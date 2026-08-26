"""
AstrBot Provider 插件 —— DeepSeek 逆向 API
将 deepseek2api-neo 的核心逻辑封装为 AstrBot 的 LLM Provider。

配置说明（在 AstrBot 配置文件的 provider_sources 中创建 type=deepseek_reverse 的条目）：
  accounts:  DeepSeek 网页端账号列表，每项含 email/mobile + password
  model:     模型名（deepseek-v4-flash / deepseek-v4-pro / deepseek-reasoner / deepseek-chat）
  thinking_enabled: 是否开启深度思考
  search_enabled:   是否开启联网搜索
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time

from collections.abc import AsyncGenerator
from typing import Any, Literal

# 确保插件目录在 sys.path 中，使 core 包可被导入
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api.provider import Provider
from astrbot.api.star import Star
from astrbot.core.provider.register import (
    register_provider_adapter,
    provider_cls_map,
    provider_registry as _prov_reg,
)
from astrbot.core.provider.entities import LLMResponse, TokenUsage

# 防止重复注册（插件重载时旧注册可能残留）
if "deepseek_reverse" in provider_cls_map:
    del provider_cls_map["deepseek_reverse"]
    _prov_reg[:] = [pm for pm in _prov_reg if pm.type != "deepseek_reverse"]
import astrbot.core.message.components as Comp
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.agent.tool import ToolSet

# 导入 deepseek2api-neo 核心模块
from core import account, chat, config, constants, session as session_module
from core.pow import get_pow_response
from core.tokens import count_tokens

logger = logging.getLogger("deepseek_reverse")

SUPPORTED_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-flash-search",
    "deepseek-chat",
    "deepseek-chat-search",
    "deepseek-reasoner",
    "deepseek-reasoner-search",
    "deepseek-v4-pro",
    "deepseek-v4-pro-search",
]

# 账号池全局初始化标记（避免 8 个实例反复清空重建）
_ACCOUNT_POOL_INITIALIZED = False


# ----------------------------------------------------------------------#
#  辅助：伪造 Request 对象（core 模块依赖 request.state）
# ----------------------------------------------------------------------#
class _FakeState:
    def __init__(self):
        self.deepseek_token: str | None = None
        self.account: dict | None = None
        self.use_config_token = True
        self.tried_accounts: list[str] = []


class _FakeRequest:
    def __init__(self):
        self.state = _FakeState()


# ----------------------------------------------------------------------#
#  辅助：模型 → model_type 映射
# ----------------------------------------------------------------------#
def _parse_model_type(model: str) -> str:
    m = model.lower().strip()
    # 去掉 -search 后缀
    if m.endswith("-search"):
        m = m[:-7]
    if m in ("deepseek-v4-pro",):
        return "expert"
    return "default"


# 判断模型是否强制开启思考（reasoner 系列）
def _is_lock_thinking(model: str) -> bool:
    m = model.lower().strip()
    if m.endswith("-search"):
        m = m[:-7]
    return m == "deepseek-reasoner"


# ----------------------------------------------------------------------#
#  SSE 流解析（非流式版 —— 收集完整文本后返回）
# ----------------------------------------------------------------------#
def _consume_sse_full(resp) -> tuple[str, str, bool]:
    """消费 DeepSeek SSE 流，返回 (text, reasoning, ok)；ok=False 表示流中断"""
    text_parts: list[str] = []
    think_parts: list[str] = []
    current_ptype = "text"
    finished = False

    try:
        for raw_line in resp.iter_lines():
            try:
                line = raw_line.decode("utf-8")
            except Exception:
                continue
            if not line or not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                finished = True
                break
            try:
                chunk = json.loads(data_str)
            except Exception:
                continue

            # 新格式 A：response.fragments
            if "v" in chunk and isinstance(chunk["v"], dict) and "response" in chunk["v"]:
                frags = chunk["v"]["response"].get("fragments", [])
                if frags:
                    for fr in frags:
                        ft = fr.get("type", "")
                        cp = "thinking" if ft == "THINK" else "text"
                        fc = fr.get("content", "")
                        if fc:
                            (think_parts if cp == "thinking" else text_parts).append(fc)
                continue

            # 新格式 B：APPEND fragments
            if chunk.get("p") == "response/fragments" and chunk.get("o") == "APPEND":
                new_frags = chunk.get("v", [])
                if new_frags:
                    for fr in new_frags:
                        ft = fr.get("type", "")
                        cp = "thinking" if ft == "THINK" else "text"
                        fc = fr.get("content", "")
                        if fc:
                            (think_parts if cp == "thinking" else text_parts).append(fc)
                continue

            # 旧格式：路径标记
            if chunk.get("p") == "response/thinking_content":
                current_ptype = "thinking"
            elif chunk.get("p") == "response/content":
                current_ptype = "text"

            if chunk.get("p") == "response/status":
                if chunk.get("v") == "FINISHED":
                    finished = True
                    break
                continue
            if chunk.get("p") == "response/search_status":
                continue

            # v 字段
            v = chunk.get("v")
            if isinstance(v, str):
                (think_parts if current_ptype == "thinking" else text_parts).append(v)
            elif isinstance(v, list):
                if any(isinstance(it, dict) and it.get("p") == "status" and it.get("v") == "FINISHED" for it in v):
                    finished = True
                    break
    except Exception as e:
        logger.warning(f"[sse_full] 解析异常: {e}")
    finally:
        try:
            resp.close()
        except Exception:
            pass

    return "".join(text_parts), "".join(think_parts), finished


# ----------------------------------------------------------------------#
#  SSE 流解析（流式版 —— 逐块放入 queue）
# ----------------------------------------------------------------------#
def _consume_sse_chunks(resp, q) -> bool:
    """消费 SSE 流并放入队列，返回是否正常结束。"""
    current_ptype = "text"
    finished = False
    try:
        for raw_line in resp.iter_lines():
            try:
                line = raw_line.decode("utf-8")
            except Exception:
                continue
            if not line or not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                finished = True
                break
            try:
                chunk = json.loads(data_str)
            except Exception:
                continue

            if "v" in chunk and isinstance(chunk["v"], dict) and "response" in chunk["v"]:
                frags = chunk["v"]["response"].get("fragments", [])
                if frags:
                    for fr in frags:
                        ft = fr.get("type", "")
                        cp = "thinking" if ft == "THINK" else "text"
                        fc = fr.get("content", "")
                        if fc:
                            q.put((cp, fc))
                continue

            if chunk.get("p") == "response/fragments" and chunk.get("o") == "APPEND":
                new_frags = chunk.get("v", [])
                if new_frags:
                    for fr in new_frags:
                        ft = fr.get("type", "")
                        cp = "thinking" if ft == "THINK" else "text"
                        fc = fr.get("content", "")
                        if fc:
                            q.put((cp, fc))
                continue

            if chunk.get("p") == "response/thinking_content":
                current_ptype = "thinking"
            elif chunk.get("p") == "response/content":
                current_ptype = "text"

            if chunk.get("p") == "response/status":
                if chunk.get("v") == "FINISHED":
                    finished = True
                    break
                continue
            if chunk.get("p") == "response/search_status":
                continue

            v = chunk.get("v")
            if isinstance(v, str):
                q.put((current_ptype, v))
            elif isinstance(v, list):
                if any(isinstance(it, dict) and it.get("p") == "status" and it.get("v") == "FINISHED" for it in v):
                    finished = True
                    break
    except Exception as e:
        logger.warning(f"[sse_chunks] 解析异常: {e}")
        q.put(("error", str(e)))
    finally:
        try:
            resp.close()
        except Exception:
            pass
        q.put(None)
    return finished


# ----------------------------------------------------------------------#
#  自动检测账号类型：11位纯数字=电话，否则=邮箱
# ----------------------------------------------------------------------#
def _build_account(identifier: str, password: str) -> dict:
    if identifier.isdigit() and len(identifier) == 11:
        return {"mobile": identifier, "password": password}
    return {"email": identifier, "password": password}


# ----------------------------------------------------------------------#
#  HIF 失败计数：连续 N 次失败强制刷新 token
# ----------------------------------------------------------------------#
def _bump_hif_fail(acct: dict | None) -> None:
    """一次完整调用失败时计数+1，达到 5 次强制刷新 HIF token。"""
    if not acct:
        return
    count = acct.get("hif_fail_count", 0) + 1
    acct["hif_fail_count"] = count
    if count >= 5:
        logger.warning(f"[HIF] 连续 {count} 次请求失败，强制刷新 HIF token")
        try:
            ok = account.ensure_hif_tokens(acct, force=True)
        except Exception as e:
            logger.warning(f"[HIF] 强制刷新异常: {e}")
            ok = False
        if ok:
            acct["hif_fail_count"] = 0
        else:
            # 强刷失败：保留计数，让后续失败更快触发强刷/进入风控名单
            logger.warning("[HIF] 强制刷新失败，保留失败计数")


# ----------------------------------------------------------------------#
#  Provider 适配器
# ----------------------------------------------------------------------#
@register_provider_adapter(
    "deepseek_reverse",
    "DeepSeek 逆向 API（网页端账号）",
    default_config_tmpl={
        "id": "deepseek_reverse",
        "type": "deepseek_reverse",
        "enable": False,
        "account": "",
        "password": "",
        "model": "deepseek-v4-flash",
        "thinking_enabled": True,
        "search_enabled": False,
        "timeout": 120,
        "key": ["linker9527"],
    },
)
class ProviderDeepSeekReverse(Provider):
    """通过 DeepSeek 网页端账号逆向调用的 LLM Provider。"""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)

        self._initialized = False
        self._provider_config = provider_config

        model = provider_config.get("model", "deepseek-v4-flash")
        self.set_model(model)

        self.thinking_enabled = provider_config.get("thinking_enabled", True)
        self.search_enabled = provider_config.get("search_enabled", False)

        logger.info(f"[DeepSeekReverse] Provider 已创建: model={model}")

    def _ensure_initialized(self):
        """延迟初始化账号池（等 Star 类通过 _conf_schema 注入配置后）"""
        # 浏览器模式开关（在覆盖 config.CONFIG 前读取并缓存到实例）
        self._avoid_ban = bool(config.CONFIG.get("avoid_ban", False))
        if self._initialized:
            return

        global _ACCOUNT_POOL_INITIALIZED

        accounts = []
        # 优先从 config.CONFIG 读取（由 Star 类注入）
        if config.CONFIG.get("accounts"):
            accounts = list(config.CONFIG["accounts"])
            logger.info(f"[_ensure_initialized] 从 Star 注入的 config.CONFIG 获取到 {len(accounts)} 个账号")

        # 如果 Star 没注入，回退到从 provider_config 读取（兼容老方式）
        if not accounts:
            pc = self._provider_config
            logger.info(f"[_ensure_initialized] config.CONFIG 无账号，尝试从 provider_config 读取")
            accounts = list(pc.get("accounts", []))
            if not accounts:
                acct_str = pc.get("account", "").strip()
                pwd = pc.get("password", "").strip()
                if acct_str and pwd:
                    accounts = [_build_account(acct_str, pwd)]

        config.CONFIG = {
            "accounts": accounts,
            "keys": self._provider_config.get("key", ["linker9527"]),
            "avoid_ban": self._avoid_ban,
        }
        if not _ACCOUNT_POOL_INITIALIZED:
            account.init_account_queue()
            _ACCOUNT_POOL_INITIALIZED = True
        self._initialized = True
        logger.info(f"[DeepSeekReverse] 账号池初始化完成: {len(accounts)} 个账号")

    # -- 基础接口 -------------------------------------------------------
    def get_current_key(self) -> str:
        return "deepseek_reverse"

    def get_keys(self) -> list[str]:
        return ["deepseek_reverse"]

    def set_key(self, key: str) -> None:
        pass

    async def get_models(self) -> list[str]:
        return list(SUPPORTED_MODELS)

    # -- 内部辅助 -------------------------------------------------------
    def _acquire_and_login(self) -> dict:
        """选账号并确保登录，返回 account dict；自动跳过疑似风控账号"""
        logger.debug("[Provider] 开始获取账号...")
        tried_ids: list[str] = []
        acct = None
        while True:
            acct = account.choose_new_account(tried_ids)
            if not acct:
                # 区分：账号池本身为空 vs 全部账号疑似风控被跳过
                if not account.account_pool.all_accounts():
                    logger.error("[Provider] 账号池为空，没有可用的 DeepSeek 账号")
                    raise Exception("没有可用的 DeepSeek 账号，请在插件配置中添加 DeepSeek 网页端账号和密码。")
                logger.error("[Provider] 所有 DeepSeek 账号均被跳过（疑似风控）")
                raise Exception("所有 DeepSeek 账号 HIF token 连续获取失败，账号可能已被风控。请确认账号状态正常后重启插件。")
            acct_id = acct.get("email", "") or acct.get("mobile", "")
            # 连续失败 >= 3 次的账号先跳过，换下一个
            if acct.get("hif_fetch_fail_count", 0) >= 3:
                # pool.acquire 已过滤风控账号，这里兜底（防竞争态）
                logger.warning(f"[Provider] 账号 {acct_id} 疑似风控（连续失败 {acct.get('hif_fetch_fail_count')} 次），跳过")
                tried_ids.append(acct_id)
                account.release_account(acct)
                continue
            break

        logger.info(f"[Provider] 获取到账号: {acct.get('email') or acct.get('mobile')}, 是否有 token: {bool(acct.get('token', '').strip())}")
        if not acct.get("token", "").strip():
            try:
                logger.info(f"[Provider] 账号 {acct_id} 无有效 token，尝试登录...")
                account.login_deepseek_via_account(acct)
                logger.info(f"[Provider] 账号 {acct_id} 登录成功")
            except Exception as e:
                account.release_account(acct)
                logger.error(f"[Provider] 账号 {acct_id} 登录失败: {type(e).__name__}: {e}", exc_info=True)
                raise Exception(f"DeepSeek 账号登录失败: {e}")
        else:
            try:
                ok = account.ensure_hif_tokens(acct)
                if not ok and acct.get("hif_fetch_fail_count", 0) >= 3:
                    # 当前账号连续失败到阈值，释放并报错提示
                    account.release_account(acct)
                    raise Exception("HIF token 连续 3 次获取失败，账号可能已被风控。请确认账号状态正常后重启插件。")
            except Exception as e:
                logger.warning(f"[Provider] 刷新 hif token 时出错: {e}")
                if isinstance(e, Exception) and "风控" in str(e):
                    raise
        return acct

    @staticmethod
    def _make_request(acct: dict) -> _FakeRequest:
        req = _FakeRequest()
        req.state.deepseek_token = acct.get("token")
        req.state.account = acct
        req.state.use_config_token = True
        req.state.tried_accounts = []
        return req

    @staticmethod
    def _delete_session(acct: dict, session_id: str) -> None:
        try:
            headers = {
                **constants.BASE_HEADERS,
                "authorization": f"Bearer {acct.get('token')}",
            }
            ds_session = session_module.get_account_session(acct)
            resp = ds_session.post(
                constants.DEEPSEEK_DELETE_SESSION_URL,
                headers=headers,
                json={"chat_session_id": session_id},
                impersonate="safari15_3",
                timeout=5,
            )
            resp.close()
        except Exception:
            pass

    def _build_messages(
        self,
        prompt: str | None,
        contexts: list | None,
        system_prompt: str | None,
        func_tool: ToolSet | None,
        tool_choice: str,
        tool_calls_result: Any,
    ) -> list[dict]:
        """组装 OpenAI 格式的 messages 列表"""
        messages = self._ensure_message_to_dicts(contexts) if contexts else []

        if prompt is not None:
            messages.append({"role": "user", "content": prompt})

        # 工具调用结果回传
        if tool_calls_result:
            if hasattr(tool_calls_result, "to_openai_messages"):
                messages.extend(tool_calls_result.to_openai_messages())
            elif isinstance(tool_calls_result, list):
                for tcr in tool_calls_result:
                    if hasattr(tcr, "to_openai_messages"):
                        messages.extend(tcr.to_openai_messages())

        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})

        # 工具系统提示
        if func_tool:
            try:
                tools_list = func_tool.get_func_desc_openai_style()
            except Exception:
                tools_list = []
            if tools_list:
                tool_sys = chat.build_tool_system_prompt(
                    tools_list, source="openai", tool_choice=tool_choice
                )
                if tool_sys:
                    system_found = False
                    for msg in messages:
                        if msg.get("role") == "system":
                            msg["content"] = msg.get("content", "") + "\n\n" + tool_sys
                            system_found = True
                            break
                    if not system_found:
                        messages.insert(0, {"role": "system", "content": tool_sys})

        return messages

    def _build_headers(self, acct: dict, request: _FakeRequest) -> dict:
        headers = {
            **constants.BASE_HEADERS,
            "authorization": f"Bearer {request.state.deepseek_token}",
        }
        if acct.get("hif_dliq"):
            headers["x-hif-dliq"] = acct["hif_dliq"]
        if acct.get("hif_leim"):
            headers["x-hif-leim"] = acct["hif_leim"]
        return headers

    @staticmethod
    def _parse_tool_calls_from_text(text: str) -> tuple[list | None, str]:
        """从响应文本中检测并解析 tool_calls"""
        if not text:
            return None, text
        try:
            tc, cleaned = chat.detect_and_parse_tool_calls(text)
            return tc, cleaned
        except Exception:
            return None, text

    @staticmethod
    def _build_tool_call_fields(tool_calls: list) -> tuple[list, list, list]:
        """把 OpenAI 格式 tool_calls 转成 LLMResponse 需要的字段"""
        args_ls: list[dict] = []
        name_ls: list[str] = []
        id_ls: list[str] = []
        for i, tc in enumerate(tool_calls):
            func = tc.get("function", {})
            name = func.get("name", "")
            args_str = func.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except (json.JSONDecodeError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {} if args is None else args
            args_ls.append(args)
            name_ls.append(name)
            id_ls.append(tc.get("id", f"call_{i + 1:03d}"))
        return args_ls, name_ls, id_ls

    # -- 同步调用链（在线程中执行）--------------------------------------
    def _do_completion_sync(self, messages: list[dict], model: str) -> tuple[str, str, int]:
        """执行完整 DeepSeek 调用链，返回 (text, reasoning, prompt_tokens)"""
        final_prompt = chat.messages_prepare(messages)

        acct = self._acquire_and_login()
        request = self._make_request(acct)
        session_id: str | None = None
        success = False

        try:
            session_id = chat.create_session(request)
            acct = request.state.account  # create_session 可能换号
            if not session_id:
                raise Exception("创建 DeepSeek 会话失败（token 可能失效）。")

            pow_resp = get_pow_response(request)
            if not pow_resp:
                raise Exception("获取 PoW 失败（token 可能失效）。")

            headers = self._build_headers(acct, request)
            headers["x-ds-pow-response"] = pow_resp

            # 根据模型名覆盖 thinking/search
            model_lower = model.lower().strip()
            is_search_model = model_lower.endswith("-search")
            is_lock_think = _is_lock_thinking(model)

            payload = {
                "chat_session_id": session_id,
                "parent_message_id": None,
                "model_type": _parse_model_type(model),
                "prompt": final_prompt,
                "ref_file_ids": [],
                "thinking_enabled": True if is_lock_think else self.thinking_enabled,
                "search_enabled": True if is_search_model else self.search_enabled,
                "preempt": False,
            }

            ds_session = session_module.get_account_session(acct)
            deepseek_resp = chat.call_completion_endpoint(payload, headers, ds_session)
            if not deepseek_resp:
                raise Exception("DeepSeek completion 请求失败。")

            text, reasoning, sse_ok = _consume_sse_full(deepseek_resp)
            if not sse_ok:
                raise Exception("DeepSeek SSE 流中断。")
            prompt_tokens = count_tokens(final_prompt)
            success = True
            acct["hif_fail_count"] = 0
            return text, reasoning, prompt_tokens

        finally:
            if not success:
                _bump_hif_fail(acct)
            if session_id:
                self._delete_session(acct, session_id)
            account.release_account(acct)

    # -- 浏览器模式（避免封控）-----------------------------------------
    def _browser_credentials(self) -> tuple:
        """浏览器模式取账号：优先走账号池（最空闲优先 + 跳过风控号），支持换号。"""
        try:
            acct = account.choose_new_account([])
            if acct:
                identifier = acct.get("email") or acct.get("mobile") or ""
                pwd = acct.get("password", "")
                if identifier and pwd:
                    return identifier, pwd
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Browser] 从账号池取号失败，回退单值: {e}")
        acct_str = str(config.CONFIG.get("account", "") or "").strip()
        pwd = str(config.CONFIG.get("password", "") or "").strip()
        return acct_str, pwd

    @staticmethod
    def _extract_text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict)]
            return chr(10).join(p for p in parts if p)
        return ""

    @classmethod
    def _build_browser_prompt(cls, messages) -> str:
        """把 AstrBot 维护的完整上下文（用户/助手对话）注入到浏览器消息里，
        让网页端模型能看到上文，而不是只看到最后一条。"""
        lines = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            txt = cls._extract_text(m.get("content", "")).strip()
            if not txt:
                continue
            who = "用户" if role == "user" else "助手"
            lines.append(f"{who}: {txt}")
        return chr(10).join(lines)

    async def _browser_chat(self, messages) -> LLMResponse:
        from core import browser as browser_mod
        user_text = self._build_browser_prompt(messages)
        logger.info("[Browser] 浏览器模式对话开始")
        tried = set()
        for attempt in range(6):
            acct, pwd = self._browser_credentials()
            if not acct or not pwd:
                raise Exception("浏览器模式无可用账号（未配置 DeepSeek 账号）")
            if acct in tried:
                break
            tried.add(acct)
            try:
                text = await browser_mod.chat(acct, pwd, user_text)
                resp = LLMResponse("assistant")
                resp.completion_text = text or ""
                resp.usage = TokenUsage(input_other=0, input_cached=0, output=count_tokens(text or ""))
                return resp
            except browser_mod.AccountBannedError as e:
                logger.warning(f"[Browser] 账号 {acct} 被禁言，自动换号重试: {e}")
                continue
            except Exception:
                raise
        raise Exception("浏览器模式所有账号均被禁言或不可用，请检查账号状态后重试")

    async def _browser_chat_stream(self, messages):
        resp = await self._browser_chat(messages)
        yield resp

    # -- text_chat（非流式）--------------------------------------------
    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: ToolSet | None = None,
        contexts: list | None = None,
        system_prompt: str | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        extra_user_content_parts: list | None = None,
        tool_choice: Literal["auto", "required"] = "auto",
        request_max_retries: int | None = None,
        **kwargs,
    ) -> LLMResponse:
        self._ensure_initialized()
        model = model or self.get_model()
        messages = self._build_messages(
            prompt, contexts, system_prompt, func_tool, tool_choice, tool_calls_result
        )

        # 浏览器模式（避免封控）：纯对话，不支持 agent/工具调用
        if self._avoid_ban:
            return await self._browser_chat(messages)

        text, reasoning, prompt_tokens = await asyncio.to_thread(
            self._do_completion_sync, messages, model
        )

        resp = LLMResponse("assistant")

        # 检测 tool_calls
        tool_calls_detected, text = self._parse_tool_calls_from_text(text)

        resp.completion_text = text or ""
        if reasoning:
            resp.reasoning_content = reasoning

        if tool_calls_detected:
            resp.role = "tool"
            args_ls, name_ls, id_ls = self._build_tool_call_fields(tool_calls_detected)
            resp.tools_call_args = args_ls
            resp.tools_call_name = name_ls
            resp.tools_call_ids = id_ls

        output_tokens = count_tokens(text or "") + count_tokens(reasoning or "")
        resp.usage = TokenUsage(
            input_other=prompt_tokens,
            input_cached=0,
            output=output_tokens,
        )

        return resp

    # -- text_chat_stream（流式）----------------------------------------
    async def text_chat_stream(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: ToolSet | None = None,
        contexts: list | None = None,
        system_prompt: str | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        tool_choice: Literal["auto", "required"] = "auto",
        request_max_retries: int | None = None,
        **kwargs,
    ) -> AsyncGenerator[LLMResponse, None]:
        self._ensure_initialized()
        model = model or self.get_model()
        messages = self._build_messages(
            prompt, contexts, system_prompt, func_tool, tool_choice, tool_calls_result
        )

        # 浏览器模式（避免封控）：非流式，单块输出
        if self._avoid_ban:
            async for _r in self._browser_chat_stream(messages):
                yield _r
            return

        import queue as _stdq

        q: _stdq.Queue = _stdq.Queue()
        all_text: list[str] = []
        all_reasoning: list[str] = []

        def _stream_worker():
            acct = None
            sid = None
            success = False
            try:
                logger.info(f"[流式请求] 开始处理，模型={model}")
                acct = self._acquire_and_login()
                request = self._make_request(acct)
                final_prompt = chat.messages_prepare(messages)
                logger.debug(f"[流式请求] messages_prepare 完成，prompt 长度={len(final_prompt)}")

                sid = chat.create_session(request)
                acct = request.state.account  # create_session 可能换号
                if not sid:
                    logger.error("[流式请求] 创建 DeepSeek 会话失败")
                    q.put(("error", "创建 DeepSeek 会话失败"))
                    return
                logger.info(f"[流式请求] 会话创建成功: {sid[:20]}...")

                pow_resp = get_pow_response(request)
                if not pow_resp:
                    logger.error("[流式请求] 获取 PoW 失败")
                    q.put(("error", "获取 PoW 失败"))
                    return
                logger.debug(f"[流式请求] PoW 获取成功: {pow_resp[:20]}...")

                headers = self._build_headers(acct, request)
                headers["x-ds-pow-response"] = pow_resp

                # 根据模型名覆盖 thinking/search
                model_lower = model.lower().strip()
                is_search_model = model_lower.endswith("-search")
                is_lock_think = _is_lock_thinking(model)

                payload = {
                    "chat_session_id": sid,
                    "parent_message_id": None,
                    "model_type": _parse_model_type(model),
                    "prompt": final_prompt,
                    "ref_file_ids": [],
                    "thinking_enabled": True if is_lock_think else self.thinking_enabled,
                    "search_enabled": True if is_search_model else self.search_enabled,
                    "preempt": False,
                }
                logger.info(f"[流式请求] 发送 completion 请求，model={model}, thinking={payload['thinking_enabled']}, search={payload['search_enabled']}")

                ds_session = session_module.get_account_session(acct)
                deepseek_resp = chat.call_completion_endpoint(payload, headers, ds_session)
                if not deepseek_resp:
                    logger.error("[流式请求] completion 请求返回空响应")
                    q.put(("error", "completion 请求失败"))
                    return
                logger.info("[流式请求] completion 响应收到，开始消费 SSE 流")

                success = _consume_sse_chunks(deepseek_resp, q)
                if success:
                    acct["hif_fail_count"] = 0

            except Exception as e:
                logger.error(f"[流式请求] 异常: {type(e).__name__}: {e}", exc_info=True)
                q.put(("error", str(e)))
            finally:
                if not success:
                    _bump_hif_fail(acct)
                if sid and acct:
                    self._delete_session(acct, sid)
                if acct:
                    account.release_account(acct)

        thread = threading.Thread(target=_stream_worker, daemon=True)
        thread.start()

        while True:
            item = await asyncio.to_thread(q.get)
            if item is None:
                break
            if isinstance(item, tuple) and len(item) == 2 and item[0] == "error":
                raise Exception(f"DeepSeek 流式调用失败: {item[1]}")

            ptype, content = item
            chunk_resp = LLMResponse("assistant", is_chunk=True)
            if ptype == "thinking":
                chunk_resp.reasoning_content = content
                all_reasoning.append(content)
            else:
                chunk_resp.result_chain = MessageChain(chain=[Comp.Plain(content)])
                all_text.append(content)
            yield chunk_resp

        # 最终完整结果
        final_resp = LLMResponse("assistant")
        final_text = "".join(all_text)

        tool_calls_detected, final_text = self._parse_tool_calls_from_text(final_text)
        final_resp.completion_text = final_text
        final_resp.reasoning_content = "".join(all_reasoning) or None

        if tool_calls_detected:
            final_resp.role = "tool"
            args_ls, name_ls, id_ls = self._build_tool_call_fields(tool_calls_detected)
            final_resp.tools_call_args = args_ls
            final_resp.tools_call_name = name_ls
            final_resp.tools_call_ids = id_ls

        yield final_resp

    async def terminate(self):
        pass


class Main(Star):
    """Star 插件入口。读取 _conf_schema.json 配置并注入到 Provider。"""

    def __init__(self, context, config=None):
        super().__init__(context, config)
        if not config:
            return

        import core.config as ds_config
        import core.account as ds_account

        # 读取配置
        account_str = str(config.get("account", "")).strip()
        password = str(config.get("password", "")).strip()
        self.logger.info(f"[插件初始化] 读取配置: account={'已填写' if account_str else '未填写'}, password={'已填写' if password else '未填写'}")

        accounts = []
        if account_str and password:
            acct = _build_account(account_str, password)
            accounts.append(acct)
            self.logger.info(f"[插件初始化] 主账号构建成功: type={'邮箱' if '@' in account_str or not account_str.isdigit() or len(account_str) != 11 else '手机号'}, 账号={account_str}")

        # 额外账号（"添加更多"按钮添加的）
        extra = config.get("extra_accounts", []) or []
        self.logger.info(f"[插件初始化] 额外账号数: {len(extra)}")
        for item in extra:
            item = str(item).strip()
            if ":" in item:
                user, pwd = item.split(":", 1)
                acct = _build_account(user.strip(), pwd.strip())
                accounts.append(acct)
                self.logger.info(f"[插件初始化] 额外账号构建成功: {user}")

        if not accounts:
            self.logger.warning("[插件初始化] 没有配置任何账号！请在 WebUI 插件配置中填写 DeepSeek 账号和密码")

        ds_config.CONFIG = {
            "accounts": accounts,
            "keys": ["linker9527"],
            "avoid_ban": bool(config.get("avoid_ban", False)),
        }
        ds_account.init_account_queue()
        self.logger.info(f"[插件初始化] 配置已加载至 ds_config.CONFIG: {len(accounts)} 个账号，账号池已初始化")

        # 自动注册 provider source 和 provider（如果不存在）
        self._auto_register_provider()

    def _auto_register_provider(self):
        """自动在 AstrBot 配置中注册 deepseek_reverse 的 provider source 和 provider。
        如果已存在则不重复注册，避免覆盖用户已有配置。"""
        try:
            cfg = self.context._config
            if cfg is None:
                self.logger.warning("[自动注册] self.context._config 为 None，跳过自动注册")
                return

            # 1. 检查并添加 provider source
            sources = cfg.get("provider_sources", [])
            has_source = any(
                s.get("type") == "deepseek_reverse" and s.get("id") == "deepseek_reverse"
                for s in sources
            )
            if has_source:
                self.logger.info("[自动注册] provider source 已存在 (deepseek_reverse)，跳过")
            else:
                new_source = {
                    "id": "deepseek_reverse",
                    "type": "deepseek_reverse",
                    "enable": True,
                    "model": "deepseek-v4-flash",
                    "thinking_enabled": True,
                    "search_enabled": False,
                    "timeout": 120,
                    "key": ["linker9527"],
                }
                sources.append(new_source)
                cfg["provider_sources"] = sources
                cfg.save_config()
                self.logger.info(f"[自动注册] 已创建 provider source: {new_source}")

            # 2. 为每个模型创建 provider 实例
            pm = self.context.provider_manager
            providers = cfg.get("provider", [])
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()

            # 已有的运行时实例 ID 集合
            existing_ids = set()
            try:
                for p in pm.provider_insts:
                    try:
                        pid = p.provider_config.get("id", None)
                        if pid:
                            existing_ids.add(pid)
                    except Exception:
                        pass
                self.logger.info(f"[自动注册] 当前运行时实例 IDs: {existing_ids}")
            except Exception as e:
                self.logger.warning(f"[自动注册] 检查运行时实例时出错: {e}", exc_info=True)

            # 8 个模型各一个实例
            model_specs = [
                ("免费deepseek api key/deepseek-v4-flash", "deepseek-v4-flash", ["text", "tool_use"]),
                ("免费deepseek api key/deepseek-v4-flash-search", "deepseek-v4-flash-search", ["text", "tool_use"]),
                ("免费deepseek api key/deepseek-chat", "deepseek-chat", ["text", "tool_use"]),
                ("免费deepseek api key/deepseek-chat-search", "deepseek-chat-search", ["text", "tool_use"]),
                ("免费deepseek api key/deepseek-reasoner", "deepseek-reasoner", ["text", "tool_use"]),
                ("免费deepseek api key/deepseek-reasoner-search", "deepseek-reasoner-search", ["text", "tool_use"]),
                ("免费deepseek api key/deepseek-v4-pro", "deepseek-v4-pro", ["text", "tool_use"]),
                ("免费deepseek api key/deepseek-v4-pro-search", "deepseek-v4-pro-search", ["text", "tool_use"]),
            ]

            def _on_task_done(t, name):
                if t.cancelled():
                    self.logger.warning(f"[自动注册] {name} 任务被取消")
                elif t.exception():
                    self.logger.error(f"[自动注册] {name} 任务失败: {type(t.exception()).__name__}: {t.exception()}", exc_info=t.exception())
                else:
                    self.logger.info(f"[自动注册] {name} 任务成功完成")

            for inst_id, model_name, modalities in model_specs:
                if inst_id in existing_ids:
                    self.logger.info(f"[自动注册] 实例已存在: {inst_id}，跳过")
                    continue

                has_cfg = any(p.get("id") == inst_id for p in providers)

                provider_cfg = {
                    "id": inst_id,
                    "enable": True,
                    "provider_source_id": "deepseek_reverse",
                    "model": model_name,
                    "modalities": modalities,
                }
                self.logger.info(f"[自动注册] 准备创建实例: {inst_id} (model={model_name}), 配置已存在={has_cfg}")

                try:
                    if has_cfg:
                        self.logger.info(f"[自动注册] load_provider: {inst_id}")
                        task = loop.create_task(pm.load_provider(provider_cfg))
                        task.add_done_callback(lambda t, n=f"load_{inst_id}": _on_task_done(t, n))
                    else:
                        self.logger.info(f"[自动注册] create_provider: {inst_id}")
                        task = loop.create_task(pm.create_provider(provider_cfg))
                        task.add_done_callback(lambda t, n=f"create_{inst_id}": _on_task_done(t, n))
                except Exception as e:
                    self.logger.error(f"[自动注册] 创建实例 {inst_id} 失败: {type(e).__name__}: {e}", exc_info=True)
                    if not has_cfg:
                        providers.append(provider_cfg)
                        cfg["provider"] = providers
                        cfg.save_config()
                        self.logger.warning(f"[自动注册] {inst_id} 动态创建失败，已写入配置，重启后生效")

        except Exception as e:
            self.logger.error(f"[自动注册] 整体流程异常: {type(e).__name__}: {e}", exc_info=True)

    async def terminate(self):
        """插件重载时清理旧的 Provider 实例，确保下次加载使用新代码"""
        try:
            pm = self.context.provider_manager
            to_remove = []
            for p in list(pm.provider_insts):
                try:
                    pid = p.provider_config.get("id", None)
                    if pid and pid.startswith("免费deepseek api key/"):
                        to_remove.append(p)
                except Exception:
                    pass
            for p in to_remove:
                try:
                    await p.terminate()
                except Exception:
                    pass
                try:
                    pm.provider_insts.remove(p)
                except ValueError:
                    pass
            self.logger.info(f"[自动注册] 清理了 {len(to_remove)} 个旧 Provider 实例")
        except Exception as e:
            self.logger.warning(f"[自动注册] 清理旧实例时出错: {e}")
