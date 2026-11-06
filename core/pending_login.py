# -*- coding: utf-8 -*-
"""待登录账号状态管理。

DeepSeek 对首次在新环境登录的账号会要求邮箱验证码/人机验证，
插件无法自动完成。检测到这种情况时把账号记入 plugin_data 的状态文件，
供配置页展示「请先登录」清单；用户手动登录后插件能自动清掉对应项。
"""

import json
import os
import time

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_undeepseek"
STATE_FILENAME = "pending_login.json"

# 触发人工登录的页面提示特征
MANUAL_LOGIN_HINTS = [
    "为保护账号安全，请完成验证后继续登录",
    "验证账号",
    "发送验证码",
    "请完成验证",
    "验证并登录",
]

# 状态保留时长（秒）：7 天后自动过期，避免陈旧条目一直显示
STATE_TTL = 7 * 24 * 3600


def _data_dir() -> str:
    """状态文件目录。

    优先写 plugin_data（符合插件数据规范）；同时会在 pages/ 下留一份
    **不含密码**的公开清单，供插件配置页 fetch 渲染。
    """
    try:
        from astrbot.api.star import StarTools

        return str(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:  # noqa: BLE001
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data",
            "plugin_data",
            PLUGIN_NAME,
        )


def _page_json_path() -> str:
    """配置页可读的公开清单（只含账号名，不含密码）。"""
    d = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "pages",
        "deepseek_reverse",
    )
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "pending_login.json")


def _state_path() -> str:
    d = _data_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, STATE_FILENAME)


def _load() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"accounts": {}}


def _save(data: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[PendingLogin] 状态写入失败: %s", e)

    # 公开清单：只保留账号名与时间，绝不写密码，供配置页渲染
    try:
        public = {
            "accounts": [
                {"account": k, "since": (v or {}).get("since", 0)}
                for k, v in (data.get("accounts") or {}).items()
            ]
        }
        pp = _page_json_path()
        with open(pp, "w", encoding="utf-8") as f:
            json.dump(public, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        logger.warning("[PendingLogin] 公开清单写入失败: %s", e)


def mark_needs_login(identifier: str, password: str = "", reason: str = "") -> None:
    """标记某账号需要人工登录一次。"""
    data = _load()
    accounts = data.setdefault("accounts", {})
    accounts[identifier] = {
        "password": password,
        "reason": reason,
        "since": time.time(),
    }
    _save(data)
    logger.error(
        "[Browser] 账号 %s 需要先在此电脑的任意浏览器进行一次登录"
        "（DeepSeek 要求邮箱验证/人机验证，插件无法自动完成）。"
        "%s",
        identifier,
        ("页面提示: " + reason) if reason else "",
    )


def mark_logged_in(identifier: str) -> None:
    """登录成功后清除该账号的待登录标记。"""
    data = _load()
    accounts = data.get("accounts", {})
    if identifier in accounts:
        del accounts[identifier]
        _save(data)
        logger.info("[Browser] 账号 %s 已登录，从待登录清单移除", identifier)


def list_pending() -> list[dict]:
    """返回未过期的待登录账号列表：[{account, password, reason, since}]。"""
    data = _load()
    now = time.time()
    result = []
    for identifier, info in (data.get("accounts") or {}).items():
        if not isinstance(info, dict):
            continue
        since = info.get("since", 0)
        if since and now - since > STATE_TTL:
            continue
        result.append(
            {
                "account": identifier,
                "password": info.get("password", ""),
                "reason": info.get("reason", ""),
                "since": since,
            }
        )
    return result


def clear_all() -> None:
    _save({"accounts": {}})


def detect_manual_login_hint(page_text: str) -> str:
    """页面文本命中人工验证特征时返回该片段，否则返回空串。"""
    if not page_text:
        return ""
    for hint in MANUAL_LOGIN_HINTS:
        if hint in page_text:
            return hint
    return ""
