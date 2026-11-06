# -*- coding: utf-8 -*-
"""启动时（插件重载）账号登录可用性探测。

对每个配置的账号做一次全新登录尝试，判定三类结果：
  ok      —— 可以直接登录（清除待人工登录标记）
  manual  —— DeepSeek 要求邮箱验证码/人机验证（记入待登录清单）
  fail    —— 其它失败（密码错、网络问题等，单独记日志）

探测用独立 headless 浏览器、全新 context，串行执行（不并发，避免触发风控）。
本模块由后台 daemon 线程调用，不阻塞 AstrBot 事件循环。
"""

import asyncio
import time

from astrbot.api import logger

from . import pending_login

DEEPSEEK_URL = "https://chat.deepseek.com/"

# 探测结果缓存（秒）：短时间内重复重载不再全量探测，减少风控风险
PROBE_CACHE_TTL = 10 * 60


def _probe_sync(accounts: list[dict], browser_exe: str) -> dict:
    """同步入口，在后台线程里调用。"""
    return asyncio.run(_probe_all_async(accounts, browser_exe))


async def _probe_one(account: str, password: str, browser) -> str:
    """探测单个账号，返回 ok / manual / fail。"""
    from . import browser as browser_mod

    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 800}, locale="zh-CN"
    )
    try:
        page = await ctx.new_page()
        await page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=60000)

        # 已有登录态能直接进聊天页 -> 直接判为可用
        if await browser_mod._is_logged_in(page):
            return "ok"

        try:
            await browser_mod._do_login(page, account, password)
            return "ok"
        except browser_mod.ManualLoginRequiredError as e:
            logger.error(
                "[Browser] 账号 %s 需要先在此电脑的任意浏览器进行一次登录"
                "（DeepSeek 要求邮箱验证/人机验证，页面提示: %s）",
                account,
                e,
            )
            return "manual"
        except browser_mod.LoginFailedError as e:
            logger.warning("[Browser] 账号 %s 登录探测失败: %s", account, e)
            return "fail"
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[Browser] 账号 %s 登录探测异常: %s: %s",
                account,
                type(e).__name__,
                e,
            )
            return "fail"
    finally:
        try:
            await ctx.close()
        except Exception:  # noqa: BLE001
            pass


async def _probe_all_async(accounts: list[dict], browser_exe: str) -> dict:
    from playwright.async_api import async_playwright

    result = {"ok": [], "manual": [], "fail": [], "skipped": False}
    if not accounts:
        return result

    logger.info(
        "[Browser] 启动期账号登录探测开始，共 %d 个账号（串行，不并发）",
        len(accounts),
    )
    t0 = time.time()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=browser_exe,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            for item in accounts:
                identifier = item.get("email") or item.get("mobile") or ""
                pwd = item.get("password", "")
                if not identifier or not pwd:
                    continue
                try:
                    verdict = await _probe_one(identifier, pwd, browser)
                except Exception as e:  # noqa: BLE001
                    verdict = "fail"
                    logger.warning(
                        "[Browser] 账号 %s 探测出错: %s: %s",
                        identifier,
                        type(e).__name__,
                        e,
                    )
                result[verdict].append(identifier)

                if verdict == "ok":
                    pending_login.mark_logged_in(identifier)
                elif verdict == "manual":
                    pending_login.mark_needs_login(
                        identifier, pwd, "启动期探测：需要邮箱验证/人机验证"
                    )
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass

    logger.info(
        "[Browser] 启动期账号登录探测完成（耗时 %.1fs）："
        "可用 %d / 需人工登录 %d / 其它失败 %d",
        time.time() - t0,
        len(result["ok"]),
        len(result["manual"]),
        len(result["fail"]),
    )
    if result["manual"]:
        logger.error(
            "[Browser] 以下账号已被标记，浏览器模式将自动跳过：%s。"
            "请在此电脑的任意浏览器里完成一次登录后刷新列表。",
            ", ".join(result["manual"]),
        )
    return result


def probe_all_accounts(accounts: list[dict], browser_exe: str = "") -> dict:
    """对外入口：探测全部账号并更新待登录清单。

    返回 {'ok': [...], 'manual': [...], 'fail': [...], 'skipped': bool}。
    """
    from . import browser as browser_mod

    exe = browser_exe or browser_mod.detect_browser()[0]
    if not exe:
        logger.warning("[Browser] 无可用浏览器内核，跳过启动期账号探测")
        return {"ok": [], "manual": [], "fail": [], "skipped": True}
    try:
        return _probe_sync(accounts, exe)
    except Exception as e:  # noqa: BLE001
        logger.warning("[Browser] 启动期账号探测整体失败: %s: %s", type(e).__name__, e)
        return {"ok": [], "manual": [], "fail": [], "skipped": True}
