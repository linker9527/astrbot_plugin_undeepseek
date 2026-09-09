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

import re
import asyncio
import logging
import os

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logger = logging.getLogger("DeepSeekReverse.browser")

# 浏览器可执行文件：自动探测 Playwright 安装的 Chromium，并允许通过
# 环境变量 UNDEEPSEEK_CHROME 覆盖（不同机器路径不同）。
# Playwright 自带 node 有 UNC 长路径 bug，故优先手动下载解压的完整 chrome。
import glob as _glob

def _find_chrome() -> str:
    # 1) 环境变量显式指定
    env = os.environ.get("UNDEEPSEEK_CHROME", "").strip()
    if env and os.path.exists(env):
        return env
    # 2) 常见 Playwright chromium 安装目录下探测 chrome.exe
    patterns = [
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright", "chromium-*", "chrome-win64", "chrome.exe"),
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright", "chromium-*", "chrome-win", "chrome.exe"),
        os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright", "chromium-*", "chrome-linux", "chrome"),
    ]
    for pat in patterns:
        matches = sorted(_glob.glob(pat))
        if matches:
            return matches[0]
    return ""

CHROME_EXE = _find_chrome()
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
    from playwright._impl import _transport as _tr

    _orig_compute_driver = _pd.compute_driver_executable  # 保存原始函数，避免递归
    _orig_get_driver_env = _pd.get_driver_env

    def _clean_unc(v: str) -> str:
        """去掉路径里的 \\?\\<盘符>:\\ UNC 长路径前缀，还原成 <盘符>:\\。"""
        import re as _re
        # 通用正则：把 \\\\?\\\\C:\\ 还原成 C:\\（不硬编码 E: 盘）
        return _re.sub(r"^\\\\\?\\\\?([A-Za-z]:)", r"\1", v)

    def _compute_driver_executable_patched():
        node, cli = _orig_compute_driver()
        return _clean_unc(node), _clean_unc(cli)

    def _get_driver_env_patched() -> dict:
        env = _orig_get_driver_env()
        # 清理 env 里所有变量中的 UNC 前缀（PATH / PYTHONPATH 等）
        for _pk in list(env.keys()):
            if "\\?\\" in env[_pk]:
                env[_pk] = _clean_unc(env[_pk])
        return env

    # playwright._transport 是 `from ._driver import compute_driver_executable`，
    # 直接把函数绑定到自身命名空间，所以只改 _driver 不生效，必须连 _transport 一起改。
    _pd.compute_driver_executable = _compute_driver_executable_patched
    _pd.get_driver_env = _get_driver_env_patched
    if hasattr(_tr, "compute_driver_executable"):
        _tr.compute_driver_executable = _compute_driver_executable_patched
    if hasattr(_tr, "get_driver_env"):
        _tr.get_driver_env = _get_driver_env_patched
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


# 禁言提示的精确信号词（匹配完整提示句，越长越不容易误命中正常提示）
BAN_KEYWORD = "由于违法用户使用规范，你的账号已被禁言至"

# 全局单例（跨请求复用浏览器与登录态）
_pw = None
_browser = None
_context = None
_page = None
_current_account = None
_lock = asyncio.Lock()
# 强制重建标志：被顶会话/超时后置位，让下一次 _ensure_page 跳过复用、重新登录
_force_rebuild = False


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
    global _pw, _browser, _context, _page, _current_account, _force_rebuild
    async with _lock:
        # 仅当页面存活且当前登录账号一致且未标记强制重建时才复用
        if (not _force_rebuild and _page is not None and not _page.is_closed()
                and _current_account == account):
            try:
                await _page.title()  # 探活
                return _page
            except Exception:
                pass
        _force_rebuild = False  # 消费掉重建标记
        _current_account = account

        # Bug4: 换号/重建页面会丢失网页端上下文，清空活跃会话集合，
        # 让后续同 session 请求重新喂入历史，避免上下文断裂。
        _active_sessions.clear()

        # Bug5: 关闭上一个账号的旧 context（换号重建时避免 context/页面泄漏）
        if _context is not None:
            try:
                await _context.close()
            except Exception:  # noqa: BLE001
                pass
            _context = None
            _page = None

        if _browser is None or not _browser.is_connected():
            # Playwright 子进程会继承带 \\?\ 前缀的 PATH（AstrBot 嵌入 E 盘所致），
            # node 遇到该 UNC 前缀会报 EISDIR 启动失败。这里给子进程一份干净的
            # PATH（把 \\?\E:\ 还原成 E:\），并在重启前记录一条日志便于排查。
            _launch_env = dict(os.environ)
            _changed = False
            for _pkey in list(_launch_env.keys()):
                if _pkey.upper() == "PATH":
                    _pv = _launch_env[_pkey]
                    _clean = re.sub(r"^\\\\\?\\\\?([A-Za-z]:)", r"\1", _pv)
                    if _clean != _pv:
                        _launch_env[_pkey] = _clean
                        _changed = True
            if not _changed:
                _launch_env = None
            _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(
                executable_path=CHROME_EXE,
                headless=True,
                env=_launch_env,
                # 隐藏 Playwright 自动化痕迹（如 navigator.webdriver），
                # 避免 DeepSeek 网页端把本插件识别为机器人进而风控/封号。
                # 这是浏览器自动化插件的常规反检测配置，不影响任何安全边界。
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
        # 注意：只在可见的提示框/toast 里检测，不扫整个 body（避免误命中帮助文本等）
        try:
            await _page.wait_for_timeout(2000)  # 等 2 秒让页面完全加载
            # 只检查 toast / 消息提示框 / dialog 类元素
            ban_els = _page.locator(
                ".ant-message, .ant-notification, .ant-modal-body, "
                ".toast, .alert, [role='alert'], [class*='toast'], [class*='message']"
            )
            n = await ban_els.count()
            for i in range(n):
                try:
                    t = await ban_els.nth(i).inner_text()
                    if BAN_KEYWORD in t:
                        logger.warning(f"[Browser] 检测到禁言提示，触发换号: {t[:80]}")
                        raise AccountBannedError(t[:200])
                except AccountBannedError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
        except AccountBannedError:
            raise
        except Exception:  # noqa: BLE001
            pass

        return _page


async def _wait_reply(page, timeout: int, base_count: int = 0):
    import time as _t
    deadline = _t.time() + timeout
    last = ""
    stable = 0
    _bl = 0
    _bls = 0
    try:
        await page.wait_for_timeout(1500)
        _bl = len(await page.locator("body").inner_text())
    except Exception:
        pass
    _lastl = _bl
    while _t.time() < deadline:
        try:
            ban_els = page.locator(".ant-message, .ant-notification, .toast, .alert, [role='alert']")
            bn = await ban_els.count()
            for bi in range(bn):
                bt = await ban_els.nth(bi).inner_text()
                if BAN_KEYWORD in bt:
                    logger.warning("[Browser] ban: %s", bt[:80])
                    raise AccountBannedError(bt[:200])
        except AccountBannedError:
            raise
        except Exception:
            pass
        text = None
        try:
            blocks = page.locator(SEL_REPLY)
            n = await blocks.count()
            if n > base_count:
                text = (await blocks.nth(n - 1).inner_text()).strip()
        except Exception:
            text = None
        if not text:
            try:
                cur = await page.locator("body").inner_text()
                cl = len(cur)
                if cl == _lastl:
                    _bls += 1
                    if _bls >= 6 and cl > _bl:
                        text = cur[_bl:].strip()
                else:
                    _lastl = cl
                    _bls = 0
            except Exception:
                pass
        if text:
            if text == last:
                stable += 1
                if stable >= 6:
                    return text
            else:
                last = text
                stable = 0
        await page.wait_for_timeout(800)
    if last:
        return last
    raise TimeoutError("wait timeout %ss" % timeout)



_active_sessions: set = set()


async def _send(page, text: str) -> int:
    """向输入框发送一条消息，返回发送前已有的回复块数（作为新回复的基准）。"""
    base = 0
    try:
        base = await page.locator(SEL_REPLY).count()
    except Exception:
        pass
    box = page.locator(SEL_CHAT_INPUT).last
    await box.click()
    await box.fill(text)
    try:
        await box.evaluate(
            """(el) => {
                const proto = el.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, el.value);
                el.dispatchEvent(new Event('input', {bubbles: true}));
            }"""
        )
    except Exception:
        pass
    await box.press("Enter")
    return base


async def chat(
    account: str,
    password: str,
    history_prompt: str,
    latest_prompt: str,
    session_id=None,
    timeout: int = 300,
) -> str:
    """在浏览器里对话。

    会话(session_id)首次：先喂完整历史（丢弃其回复），再发最新问题并返回答案。
    会话后续：复用同一页面，只发最新消息（网页端自己记住历史），上下文连续、不反复新建。
    """
    global _force_rebuild
    page = await _ensure_page(account, password)

    is_first = bool(session_id) and session_id not in _active_sessions
    if is_first:
        _active_sessions.add(session_id)
        # 首次：先发历史作为上下文（不返回它的回复），再发最新问题
        if history_prompt.strip():
            logger.info(f"[Browser] 会话 {session_id} 首次，喂入历史 {len(history_prompt)} 字")
            _base = await _send(page, history_prompt)
            try:
                await _wait_reply(page, 30, _base)
            except TimeoutError:
                pass
        else:
            logger.info(f"[Browser] 会话 {session_id} 首次，无历史，直接发问题")

    # 发最新问题并返回回复
    base = await _send(page, latest_prompt)
    try:
        text = await _wait_reply(page, timeout, base)
        return text
    except TimeoutError:
        # 会话被顶/超时：标记强制重建，让下次 _ensure_page 重新登录
        _force_rebuild = True
        raise


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
