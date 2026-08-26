# -*- coding: utf-8 -*-
"""浏览器自动化模式（抗风控）。

通过 Playwright 用真实 Chromium 访问 DeepSeek 网页端完成对话，
比直调内部 API 更像真实用户，抗风控能力最强。

副作用：网页端是纯对话，不支持 agent/工具调用。

说明：
- 使用 async API，可在异步 Provider 中直接 await。
- 登录态会持久化到 storage_state.json，复用 cookie 减少反复登录，降低风控。
- 选择器已按真实网页实测校准（2026-08-26）。
"""

import asyncio
import logging
import os

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logger = logging.getLogger("DeepSeekReverse.browser")

# 完整 Chrome 内核（Playwright 自带 node 有 UNC 长路径 bug，故手动下载解压）
CHROME_EXE = r"C:\Users\ning\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
DEEPSEEK_URL = "https://chat.deepseek.com/"

# 登录态按账号分文件持久化（避免换号时互相覆盖）
def _storage_path(identifier: str) -> str:
    import hashlib
    safe = hashlib.md5(identifier.encode("utf-8")).hexdigest()[:12]
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", f"storage_state_{safe}.json")
    )

# ---------------------------------------------------------------------------
# 修复 Playwright 在 Windows 长路径(\\?\E:\)下驱动无法启动的问题：
# 自带 node 解析 \\?\E:\ 这类 UNC 长路径前缀会报错，启动前去掉前缀。
# ---------------------------------------------------------------------------
try:
    from playwright._impl import _driver as _pd

    def _compute_driver_executable_patched():
        node, cli = _pd.compute_driver_executable()
        _pre = "\\\\?\\"
        if node.startswith(_pre):
            node = node[len(_pre):]
        if cli.startswith(_pre):
            cli = cli[len(_pre):]
        return node, cli

    _pd.compute_driver_executable = _compute_driver_executable_patched
except Exception as _e:  # noqa: BLE001
    logger.warning("[Browser] 驱动路径 patch 失败: %s", _e)

# ---------------------------------------------------------------------------
# DOM 选择器（已实测校准）
# ---------------------------------------------------------------------------
SEL_PWD_LOGIN = "text=密码登录"        # 登录页切到"密码登录"模式的可见入口
SEL_ACCOUNT = "input[type='text']"     # 手机号 / 邮箱地址 输入框
SEL_PASSWORD = "input[type='password']"  # 密码输入框
SEL_LOGIN_BTN = "登录"                 # 登录按钮文本（需 exact + 可见）
SEL_CHAT_INPUT = "textarea"            # 聊天输入框
SEL_REPLY = ".ds-markdown, .markdown, [class*='markdown']"  # 回复内容容器


class AccountBannedError(Exception):
    """账号被禁言（风控）异常，触发换号。"""


# 禁言提示的精确信号词（正常回复几乎不会出现，避免误判换号）
BAN_KEYWORD = "已被禁言"

# 全局单例（跨请求复用浏览器与登录态）
_pw = None
_browser = None
_context = None
_page = None
_current_account = None
_lock = asyncio.Lock()


async def _is_logged_in(page) -> bool:
    """已登录的判断：能见到聊天输入框即视为登录。"""
    try:
        await page.locator(SEL_CHAT_INPUT).first.wait_for(state="visible", timeout=5000)
        return True
    except Exception:
        return False


async def _do_login(page, account: str, password: str):
    """密码登录：切到密码登录模式 -> 填账号/密码 -> 点登录。"""
    logger.info("[Browser] 开始网页登录: %s", account)
    # 1. 点击可见的"密码登录"入口（切换到 手机号/邮箱 + 密码 表单）
    for i in range(await page.locator(SEL_PWD_LOGIN).count()):
        el = page.locator(SEL_PWD_LOGIN).nth(i)
        if await el.is_visible():
            await el.click(timeout=8000)
            break
    await page.wait_for_timeout(1200)

    # 2. 填账号 + 密码
    await page.locator(SEL_ACCOUNT).first.fill(account)
    await page.locator(SEL_PASSWORD).first.fill(password)

    # 3. 点可见的"登录"按钮（exact，避免点到"验证码登录"）
    login_btn = page.get_by_text(SEL_LOGIN_BTN, exact=True)
    clicked = False
    for i in range(await login_btn.count()):
        if await login_btn.nth(i).is_visible():
            await login_btn.nth(i).click(timeout=8000)
            clicked = True
            break
    if not clicked:
        raise RuntimeError("[Browser] 未找到可见的登录按钮")

    # 4. 等进入聊天页（出现输入框）
    await page.locator(SEL_CHAT_INPUT).first.wait_for(state="visible", timeout=30000)
    logger.info("[Browser] 网页登录成功")


async def _ensure_page(account: str, password: str):
    """确保浏览器已打开并处于登录状态，返回 page。"""
    global _pw, _browser, _context, _page, _current_account
    async with _lock:
        # 仅当页面存活且当前登录账号一致时才复用（否则重建以支持换号）
        if _page is not None and not _page.is_closed() and _current_account == account:
            try:
                await _page.title()  # 探活
                return _page
            except Exception:
                pass
        _current_account = account

        if _browser is None or not _browser.is_connected():
            _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(
                executable_path=CHROME_EXE,
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )

        # 有登录态直接恢复，避免反复登录（按账号分文件）
        state_path = _storage_path(account)
        if os.path.exists(state_path):
            _context = await _browser.new_context(
                storage_state=state_path,
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )
        else:
            _context = await _browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )

        _page = await _context.new_page()
        await _page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=60000)

        if not await _is_logged_in(_page):
            await _do_login(_page, account, password)
            try:
                await _context.storage_state(path=state_path)
                logger.info("[Browser] 登录态已保存到 %s", state_path)
            except Exception as _e:  # noqa: BLE001
                logger.warning("[Browser] 保存登录态失败: %s", _e)

        # 登录后检测禁言提示（如账号已被禁言则抛异常触发换号）
        try:
            _body = await _page.locator("body").inner_text()
            if BAN_KEYWORD in _body:
                raise AccountBannedError(f"账号 {account} 已被禁言")
        except AccountBannedError:
            raise
        except Exception:  # noqa: BLE001
            pass

        return _page


async def _wait_reply(page, timeout: int):
    """等待回复生成完成：轮询 markdown 内容稳定出现。"""
    import time as _t
    deadline = _t.time() + timeout
    last = ""
    while _t.time() < deadline:
        # 禁言检测：命中精确信号词立即抛异常触发换号
        try:
            _body = await page.locator("body").inner_text()
            if BAN_KEYWORD in _body:
                raise AccountBannedError("账号已被禁言")
        except AccountBannedError:
            raise
        except Exception:  # noqa: BLE001
            pass
        try:
            blocks = page.locator(SEL_REPLY)
            n = await blocks.count()
            if n:
                txt = (await blocks.nth(n - 1).inner_text()).strip()
                if txt:
                    if txt == last:
                        # 内容连续两次相同，视为生成完成
                        return txt
                    last = txt
        except Exception:
            pass
        await page.wait_for_timeout(3000)
    return last


async def chat(account: str, password: str, user_text: str, timeout: int = 180) -> str:
    """在浏览器里发一条消息并抓取完整回复文本（纯对话，不支持工具调用）。"""
    page = await _ensure_page(account, password)

    box = page.locator(SEL_CHAT_INPUT).last
    await box.click()
    await box.fill(user_text)
    await box.press("Enter")  # 深求无发送按钮，回车发送

    text = await _wait_reply(page, timeout)
    return text


async def close():
    """关闭浏览器（可选的清理入口）。"""
    global _pw, _browser, _context, _page
    async with _lock:
        try:
            if _browser is not None:
                await _browser.close()
        except Exception:  # noqa: BLE001
            pass
        _page = _context = _browser = _pw = None
