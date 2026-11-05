# -*- coding: utf-8 -*-
"""浏览器自动化模式（抗风控）。

通过 Playwright 用真实 Chromium 访问 DeepSeek 网页端完成对话，
比直调内部 API 更像真实用户，抗风控能力最强。

副作用：网页端是纯对话，不支持 agent/工具调用。

说明：
- 使用 async API，可在异步 Provider 中直接 await。
- 登录态会持久化到 AstrBot 的 data/plugin_data/<插件名>/storage_state_*.json，
  复用 cookie 减少反复登录，降低风控（该文件等价于账号凭据，勿外传）。
- 选择器已按真实网页实测校准（2026-08-26）。
"""

import re
import asyncio
import os

from astrbot.api import logger

from playwright.async_api import async_playwright, TimeoutError as PWTimeout


# 浏览器可执行文件：自动探测可用浏览器，并允许通过环境变量
# UNDEEPSEEK_CHROME 覆盖（不同机器路径不同）。
# Playwright 自带 node 有 UNC 长路径 bug，故优先手动下载解压的完整 chrome。
import glob as _glob

# 插件包根目录（core/ 的上一级），提前定义供各辅助函数使用
_LEGACY_PLUGIN_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)

# 探测来源标记
SRC_ENV = "env"                    # 环境变量指定
SRC_PLUGIN = "plugin"              # 插件内 online 下载的内核
SRC_PLAYWRIGHT = "playwright"      # 系统 playwright install 装的
SRC_SYSTEM = "system"              # 系统已装的 Chrome / Edge / Brave
SRC_NONE = "none"                  # 都没找到


def _system_browser_candidates() -> list[tuple[str, str]]:
    """系统已安装的 Chromium 系浏览器，返回 [(名称, 路径)]。

    只列 Chromium 内核的浏览器：Firefox 走 Playwright 的 firefox 通道，
    不能用 chromium.launch 启动，故不纳入。
    """
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        ("Chrome", os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe")),
        ("Chrome", os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe")),
        ("Chrome", os.path.join(local, "Google", "Chrome", "Application", "chrome.exe")),
        ("Edge", os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe")),
        ("Edge", os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe")),
        ("Brave", os.path.join(pf, "BraveSoftware", "Brave-Browser", "Application", "brave.exe")),
        ("Chromium", os.path.join(pf, "Chromium", "Application", "chrome.exe")),
    ]
    seen = set()
    result = []
    for name, path in candidates:
        if not path or not os.path.isfile(path):
            continue
        real = os.path.normcase(os.path.realpath(path))
        if real in seen:
            continue
        seen.add(real)
        result.append((name, path))
    return result


def detect_browser() -> tuple[str, str, str]:
    """探测可用浏览器，返回 (路径, 来源标记, 人类可读名称)。

    优先级（前面的更可控，后面的更省事）：
      1. UNDEEPSEEK_CHROME 环境变量 —— 用户显式指定
      2. 插件内 online 内核 —— /undeepseek online 下载的
      3. 系统 Playwright 目录 —— 用户自己 playwright install chromium
      4. 系统 Chrome / Edge / Brave —— 无需下载即可用
    """
    # 1) 环境变量
    env = os.environ.get("UNDEEPSEEK_CHROME", "").strip()
    if env and os.path.isfile(env):
        return env, SRC_ENV, f"环境变量指定 ({os.path.basename(env)})"

    # 2) 插件自己的内核目录
    try:
        matches = sorted(
            _glob.glob(os.path.join(_plugin_online_dir(), "chromium-*", "chrome-win64", "chrome.exe"))
        )
        if matches:
            return matches[0], SRC_PLUGIN, "插件内置内核 (online 下载)"
    except Exception as _e:  # noqa: BLE001
        logger.debug("[Browser] 插件内核目录探测失败: %s", _e)

    # 3) 系统级 Playwright 安装目录
    for pat in (
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright", "chromium-*", "chrome-win64", "chrome.exe"),
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright", "chromium-*", "chrome-win", "chrome.exe"),
        os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright", "chromium-*", "chrome-linux", "chrome"),
    ):
        matches = sorted(_glob.glob(pat))
        if matches:
            return matches[0], SRC_PLAYWRIGHT, "系统 Playwright 内核"

    # 4) 系统已装浏览器
    for name, path in _system_browser_candidates():
        logger.info("[Browser] 未找到内置内核，使用系统浏览器: %s (%s)", name, path)
        return path, SRC_SYSTEM, f"系统 {name}"

    return "", SRC_NONE, "未找到"


def _find_chrome() -> str:
    return detect_browser()[0]


def _plugin_online_dir() -> str:
    """插件内核目录（与 core/setup.py 的 online_dir 保持一致）。"""
    try:
        from astrbot.api.star import StarTools

        base = str(StarTools.get_data_dir("astrbot_plugin_undeepseek"))
    except Exception:  # noqa: BLE001
        base = os.path.join(_LEGACY_PLUGIN_DIR, "data", "plugin_data", "astrbot_plugin_undeepseek")
    return os.path.join(base, "online")

CHROME_EXE = _find_chrome()
DEEPSEEK_URL = "https://chat.deepseek.com/"


def refresh_chrome_exe() -> str:
    """重新探测浏览器路径（下载内核后调用，使新内核立即生效）。"""
    global CHROME_EXE
    CHROME_EXE = _find_chrome()
    return CHROME_EXE

# 登录态按账号分文件持久化（避免换号时互相覆盖）
PLUGIN_NAME = "astrbot_plugin_undeepseek"


def _storage_dir() -> str:
    """登录态目录：AstrBot 的 data/plugin_data/<插件名>。

    留出回落分支便于脱离 AstrBot 单独调试：拿不到数据目录时退到
    插件目录下的 data/，避免写入插件包内部（插件更新会覆盖，且不符合
    插件数据与代码分离的约定）。
    """
    try:
        from astrbot.api.star import StarTools

        path = str(StarTools.get_data_dir(PLUGIN_NAME))
        os.makedirs(path, exist_ok=True)
        return path
    except Exception as e:  # noqa: BLE001
        fallback = os.path.join(_LEGACY_PLUGIN_DIR, "data")
        logger.warning("[Browser] 获取 plugin_data 目录失败(%s)，回落到 %s", e, fallback)
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _migrate_legacy_storage(target_dir: str) -> None:
    """把旧版存在插件根目录的 storage_state*.json 迁到新目录（移动，不留副本）。"""
    try:
        names = [
            n
            for n in os.listdir(_LEGACY_PLUGIN_DIR)
            if n.startswith("storage_state") and n.endswith(".json")
        ]
    except Exception:  # noqa: BLE001
        return
    import shutil

    for name in names:
        src = os.path.join(_LEGACY_PLUGIN_DIR, name)
        dst = os.path.join(target_dir, name)
        if os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
            logger.info("[Browser] 登录态已迁移: %s -> %s", name, target_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Browser] 登录态迁移失败 %s: %s", name, e)


def _storage_path(identifier: str) -> str:
    import hashlib

    safe = hashlib.md5(identifier.encode("utf-8")).hexdigest()[:12]
    target_dir = _storage_dir()
    _migrate_legacy_storage(target_dir)
    return os.path.join(target_dir, f"storage_state_{safe}.json")


def _restrict_permissions(path: str) -> None:
    """登录态等价于账号凭据，POSIX 上收紧到仅本人可读写。"""
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        pass

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
# 对话全程锁：所有账号/会话共用一个页面，发送与等待回复必须串行，
# 否则并发请求的消息会交错进同一聊天框、回复互相串会话
_chat_lock = asyncio.Lock()
# 强制重建标志：被顶会话/超时后置位，让下一次 _ensure_page 跳过复用、重新登录
_force_rebuild = False


async def _is_logged_in(page) -> bool:
    """已登录的判断：能见到聊天输入框即视为登录。"""
    try:
        await page.locator(SEL_CHAT_INPUT).first.wait_for(state="visible", timeout=5000)
        return True
    except Exception:
        return False


# 登录失败 / 需要人工验证的提示语。
# 注意：只检测「点击登录之后新出现」的提示——cookie 过期时页面本身就会显示
# 「登录信息已失效」，若不加区分会一进页面就误判成登录失败。
LOGIN_FAIL_HINTS = [
    "密码错误",
    "账号或密码",
    "用户名或密码",
    "登录失败",
    "操作过于频繁",
    "账号异常",
    "账号不存在",
    "请完成验证",
    "请进行验证",
]

# 这些提示在登录页一进来就存在，不能作为「本次登录失败」的依据
PRE_EXISTING_HINTS = ["登录信息已失效", "验证码登录"]


class LoginFailedError(Exception):
    """登录失败（密码错/需要验证码/风控等），换号或提示用户。"""


async def _read_login_error(page) -> str:
    """从登录页抓取**本次登录**产生的错误提示，抓不到返回空串。

    只认 LOGIN_FAIL_HINTS 里的强特征提示（密码错误、频繁、需验证等）；
    「登录信息已失效」这类进页面就有的文案不算，避免一进页面就误判。
    """
    try:
        body = await page.locator("body").inner_text()
    except Exception:  # noqa: BLE001
        return ""
    for hint in LOGIN_FAIL_HINTS:
        if hint in body:
            idx = body.find(hint)
            return body[max(0, idx - 40): idx + 80].replace("\n", " ").strip()
    return ""


async def _do_login(page, account: str, password: str):
    """密码登录：切到密码登录模式 -> 填账号/密码 -> 点登录 -> 确认进入聊天页。

    登录失败（密码错、需验证码、风控）会抛 LoginFailedError，
    而不是傻等到 30 秒超时——后者在日志里看不出真实原因。
    """
    logger.info("[Browser] 开始网页登录: %s", account)

    # 1. 若当前不在密码登录模式（全新浏览器默认是验证码登录），点入口切换
    if await page.locator(SEL_PASSWORD).count() == 0:
        for i in range(await page.locator(SEL_PWD_LOGIN).count()):
            el = page.locator(SEL_PWD_LOGIN).nth(i)
            try:
                if await el.is_visible():
                    await el.click(timeout=8000)
                    break
            except Exception:  # noqa: BLE001
                continue
        await page.wait_for_timeout(1200)

    # 2. 确认表单存在，否则说明页面结构变了或卡在别的状态
    if await page.locator(SEL_PASSWORD).count() == 0:
        err = await _read_login_error(page)
        raise LoginFailedError(
            f"登录页未出现密码输入框（页面提示: {err or '无'}），无法自动登录"
        )

    # 3. 填账号 + 密码
    try:
        await page.locator(SEL_ACCOUNT).first.fill(account, timeout=10000)
        await page.locator(SEL_PASSWORD).first.fill(password, timeout=10000)
    except Exception as e:  # noqa: BLE001
        raise LoginFailedError(f"填写账号/密码失败: {type(e).__name__}: {e}") from e

    # 4. 点可见的"登录"按钮（exact，避免点到"验证码登录"）
    login_btn = page.get_by_text(SEL_LOGIN_BTN, exact=True)
    clicked = False
    for i in range(await login_btn.count()):
        try:
            if await login_btn.nth(i).is_visible():
                await login_btn.nth(i).click(timeout=8000)
                clicked = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not clicked:
        raise LoginFailedError("未找到可见的登录按钮")

    # 5. 等进入聊天页：轮询判断，同时检测是否出现**新增的**错误提示
    #    最多等 20 秒；一旦看到明确的登录失败提示立即抛出，不空等到超时
    for step in range(20):
        if await _is_logged_in(page):
            logger.info("[Browser] 网页登录成功")
            return
        err = await _read_login_error(page)
        if err:
            raise LoginFailedError(f"登录失败，页面提示: {err}")
        await page.wait_for_timeout(1000)

    err = await _read_login_error(page)
    raise LoginFailedError(
        f"登录后 20 秒内未进入聊天页（当前 URL: {page.url}，页面提示: {err or '无'}）"
    )


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
            # 浏览器内核路径在每次启动前重新探测：可能刚通过
            # /undeepseek online 下载完，也可能是用户中途装了系统 Chrome
            exe_path = refresh_chrome_exe()
            if not exe_path:
                raise RuntimeError(
                    "未找到可用的浏览器内核。请任选一种方式：\n"
                    "  1) 发送命令 /undeepseek online 自动下载（推荐）\n"
                    "  2) 执行 playwright install chromium\n"
                    "  3) 设置环境变量 UNDEEPSEEK_CHROME 指向 chrome.exe / msedge.exe"
                )

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
                executable_path=exe_path,
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
                _restrict_permissions(state_path)
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
        # 有部分文本同样属于超时：必须上抛让上层标记重建并重试，
        # 不能把半截答案静默当成功返回（截断内容用户不可知）
        raise TimeoutError("wait timeout %ss, partial=%r" % (timeout, last[:80]))
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
    # 单页面全局共享，整个"发历史→发问题→等回复"流程必须串行执行
    async with _chat_lock:
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
