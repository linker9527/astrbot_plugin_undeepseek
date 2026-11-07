# -*- coding: utf-8 -*-
"""配置页后端 API：手动邮箱验证流程。

提供 3 个端点供 pages/deepseek_reverse/index.html 调用：
  POST /login/start   —— 开有头浏览器，预填账号密码，用户自己过验证
  POST /login/check   —— 检测是否已登录，成功则保存 ck + 收编窗口 + 移除待登录标记
  GET  /login/status  —— 查询当前验证窗口状态
"""

import asyncio
import time

from astrbot.api import logger
from quart import request

PLUGIN_NAME = "astrbot_plugin_undeepseek"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"

DEEPSEEK_URL = "https://chat.deepseek.com/"

# 账号 -> {browser, context, page, started_at, done}
_manual_sessions: dict = {}
_manual_lock = asyncio.Lock()


class PageApi:
    def __init__(self, plugin):
        self.plugin = plugin
        self._register()

    def _register(self) -> None:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            logger.warning("[PageApi] 当前 AstrBot 版本不支持拓展页 API 注册")
            return
        routes = [
            ("/login/pending", self.login_pending, ["GET"], "待邮箱验证账号清单"),
            ("/login/start", self.login_start, ["POST"], "打开有头浏览器进行邮箱验证"),
            ("/login/check", self.login_check, ["POST"], "检测验证结果并保存登录态"),
            ("/login/status", self.login_status, ["GET"], "查询验证窗口状态"),
        ]
        for path, handler, methods, desc in routes:
            register(f"{PAGE_API_PREFIX}{path}", handler, methods, desc)
        logger.info("[PageApi] 已注册 4 个手动验证 API")

    @staticmethod
    def _ok(data=None, message="ok"):
        return {"status": "ok", "message": message, "data": data or {}}

    @staticmethod
    def _err(message):
        return {"status": "error", "message": message, "data": {}}

    def _account_password(self, account: str) -> str:
        """从插件配置里取该账号的密码。"""
        try:
            cfg = self.plugin.plugin_config or {}
            if str(cfg.get("account", "")).strip() == account:
                return str(cfg.get("password", "")).strip()
            for item in cfg.get("extra_accounts", []) or []:
                u, _, p = str(item).partition(":")
                if u.strip() == account:
                    return p.strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    @staticmethod
    async def _get_body() -> dict:
        try:
            return await request.get_json(silent=True) or {}
        except Exception:  # noqa: BLE001
            return {}

    async def login_pending(self) -> dict:
        """返回待邮箱验证的账号清单（只含账号名，绝不含密码）。"""
        from . import pending_login

        try:
            items = pending_login.list_pending()
            return self._ok(
                {
                    "accounts": [
                        {"account": x["account"], "since": x.get("since", 0)}
                        for x in items
                    ]
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[PageApi] 读取待验证清单失败: %s", e)
            return self._ok({"accounts": []})

    async def login_start(self) -> dict:
        """打开有头浏览器，切到密码登录并预填账号密码。"""
        data = await self._get_body()
        account = str(data.get("account", "")).strip()
        if not account:
            return self._err("缺少账号")
        password = self._account_password(account)
        if not password:
            return self._err(f"未在配置中找到账号 {account} 的密码")

        from playwright.async_api import async_playwright
        from . import browser as browser_mod

        exe = browser_mod.detect_browser()[0]
        if not exe:
            return self._err("未找到可用浏览器内核，请先发送 /undeepseek online")

        async with _manual_lock:
            old = _manual_sessions.pop(account, None)
            if old:
                try:
                    await old["browser"].close()
                except Exception:  # noqa: BLE001
                    pass

            try:
                pw = await async_playwright().start()
                browser = await pw.chromium.launch(
                    executable_path=exe,
                    headless=False,  # 有头：用户要手动过人机/邮箱验证
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 860}, locale="zh-CN"
                )
                page = await context.new_page()
                await page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=60000)

                # 切到密码登录模式
                if await page.locator("input[type='password']").count() == 0:
                    btn = page.get_by_text("密码登录", exact=True)
                    for i in range(await btn.count()):
                        try:
                            if await btn.nth(i).is_visible():
                                await btn.nth(i).click(timeout=8000)
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    await page.wait_for_timeout(1500)

                # 预填账号密码，用户只需过验证/点登录
                try:
                    await page.locator("input[type='text']").first.fill(account, timeout=10000)
                    await page.locator("input[type='password']").first.fill(password, timeout=10000)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[PageApi] 预填账号密码失败（可手动输入）: %s", e)

                _manual_sessions[account] = {
                    "pw": pw,
                    "browser": browser,
                    "context": context,
                    "page": page,
                    "started_at": time.time(),
                    "done": False,
                }
                logger.info("[PageApi] 已为 %s 打开有头验证窗口", account)
                return self._ok({"account": account}, "验证窗口已打开")
            except Exception as e:  # noqa: BLE001
                logger.error("[PageApi] 打开验证窗口失败: %s: %s", type(e).__name__, e)
                return self._err(f"打开验证窗口失败: {type(e).__name__}: {e}")

    async def login_check(self) -> dict:
        """检测验证窗口是否已登录；成功则保存 ck、收编窗口、移除待登录标记。"""
        data = await self._get_body()
        account = str(data.get("account", "")).strip()
        session = _manual_sessions.get(account)
        if not session:
            return self._err("没有进行中的验证窗口，请先点「进行邮箱验证」")

        from . import browser as browser_mod
        from . import pending_login

        page = session["page"]
        try:
            logged = await browser_mod._is_logged_in(page)
        except Exception as e:  # noqa: BLE001
            return self._err(f"检测失败: {type(e).__name__}: {e}")

        if not logged:
            return self._ok(
                {"account": account, "logged_in": False},
                "尚未登录成功，请在打开的浏览器里完成验证后点「重新检测」",
            )

        # 登录成功：写 ck 快照 + 把窗口及其浏览器升格为常驻浏览器 + 移除待登录标记
        context = session["context"]
        try:
            state_path = browser_mod._storage_path(account)
            await context.storage_state(path=state_path)
            browser_mod._restrict_permissions(state_path)
            logger.info("[PageApi] 账号 %s ck 已保存: %s", account, state_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("[PageApi] ck 保存失败: %s", e)

        # 关键：有头浏览器与 headless 常驻浏览器是两个独立 Playwright 实例，
        # context 不能跨实例复用。因此直接把这个「已登录的有头浏览器」升格为
        # 插件的常驻浏览器，后续所有账号都在这个进程里开 context。
        browser_mod.replace_browser(session["pw"], session["browser"])
        browser_mod.adopt_session(account, context, page)
        session["done"] = True
        # 已升格，不能再关掉这个浏览器
        _manual_sessions[account] = {**session, "browser": None, "pw": None}
        pending_login.mark_logged_in(account)

        logger.info("[PageApi] 账号 %s 验证完成，窗口已纳管，待登录清单已移除", account)
        return self._ok(
            {"account": account, "logged_in": True},
            "验证成功，登录态已保存，列表将移除该账号",
        )

    async def login_status(self) -> dict:
        """查询哪些账号有进行中的验证窗口。"""
        return self._ok(
            {
                "sessions": [
                    {
                        "account": acc,
                        "done": s.get("done", False),
                        "started_at": s.get("started_at", 0),
                    }
                    for acc, s in _manual_sessions.items()
                ]
            }
        )


async def close_all_manual() -> None:
    """插件卸载时关闭所有有头验证窗口。"""
    async with _manual_lock:
        for acc, s in list(_manual_sessions.items()):
            br = s.get("browser")
            if br is None:  # 已升格为常驻浏览器，由 browser.close() 统一清理
                continue
            try:
                await br.close()
            except Exception:  # noqa: BLE001
                pass
        _manual_sessions.clear()
