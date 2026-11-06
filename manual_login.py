# -*- coding: utf-8 -*-
"""DeepSeek 手动登录工具 —— 让用户在弹出的浏览器里完成验证，然后保存登录态。

用法（在 AstrBot 目录外运行，不依赖 AstrBot）：
    E:\\bot\\AstrBot\\backend\\python\\python.exe manual_login.py [账号]

登录态会保存到插件的 plugin_data 目录，文件名按账号哈希，与插件逻辑一致。
"""
import asyncio
import hashlib
import os
import sys

from playwright.async_api import async_playwright

PLUGIN_DATA = r"C:\Users\ning\.astrbot\data\plugin_data\astrbot_plugin_undeepseek"
KERNEL = os.path.join(
    PLUGIN_DATA, "online", "chromium-1234", "chrome-win64", "chrome.exe"
)
DEEPSEEK_URL = "https://chat.deepseek.com/"


def storage_path(identifier: str) -> str:
    safe = hashlib.md5(identifier.encode("utf-8")).hexdigest()[:12]
    return os.path.join(PLUGIN_DATA, f"storage_state_{safe}.json")


async def main():
    account = sys.argv[1] if len(sys.argv) > 1 else input("DeepSeek 账号（邮箱）: ").strip()
    if not account:
        print("未提供账号，退出")
        return 1

    exe = KERNEL if os.path.isfile(KERNEL) else None
    if not exe:
        print(f"未找到内核: {KERNEL}")
        print("请先发送 /undeepseek online 下载内核")
        return 1

    state_file = storage_path(account)
    print(f"\n账号: {account}")
    print(f"登录态将保存到: {state_file}\n")

    async with async_playwright() as p:
        # headless=False：必须显示界面，用户要手动收验证码
        browser = await p.chromium.launch(executable_path=exe, headless=False)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 860}, locale="zh-CN"
        )
        page = await ctx.new_page()

        # 已有登录态的话先加载，可能还在有效期内
        if os.path.isfile(state_file):
            print("检测到已有登录态，将尝试复用...")
        await page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=60000)

        # 关键第一步：默认是「验证码登录」模式（只有手机号输入框），
        # 必须点「密码登录」才会出现 邮箱 + 密码 表单
        try:
            if await page.locator("input[type='password']").count() == 0:
                pwd_login = page.get_by_text("密码登录", exact=True)
                for i in range(await pwd_login.count()):
                    try:
                        if await pwd_login.nth(i).is_visible():
                            await pwd_login.nth(i).click(timeout=8000)
                            print("已切换到密码登录模式")
                            break
                    except Exception:  # noqa: BLE001
                        continue
                await page.wait_for_timeout(1500)
        except Exception as e:  # noqa: BLE001
            print(f"切换密码登录模式失败（可手动点击）: {e}")

        # 预填账号，省得用户再敲一遍
        try:
            acc_input = page.locator("input[type='text']").first
            if await page.locator("input[type='text']").count() > 0:
                await acc_input.fill(account, timeout=8000)
                print(f"已预填账号: {account}")
                # 焦点移到密码框，方便直接输入密码
                await page.locator("input[type='password']").first.click(timeout=5000)
        except Exception as e:  # noqa: BLE001
            print(f"预填账号失败（手动输入即可）: {e}")

        print("=" * 60)
        print("浏览器已打开，脚本已自动：")
        print("  1) 点击「密码登录」切换到邮箱+密码表单")
        print("  2) 预填好账号")
        print("\n你只需要：")
        print("  1) 在密码框输入密码，点「登录」")
        print("  2) 若提示「请完成验证」→ 点「发送验证码」→ 查邮箱填 6 位码")
        print("  3) 登录成功（看到聊天输入框）后，脚本会自动检测并保存")
        print("=" * 60)
        print("（最多等待 10 分钟）\n")

        ok = False
        for i in range(600):
            try:
                if await page.locator("textarea").count() > 0:
                    ok = True
                    break
            except Exception:  # noqa: BLE001
                pass
            if i % 10 == 0 and i > 0:
                print(f"  等待中... {i}s（页面: {page.url}）")
            await page.wait_for_timeout(1000)

        if not ok:
            print("\n未检测到登录成功，是否仍要保存当前状态？(y/N)")
            if input().strip().lower() != "y":
                await browser.close()
                return 1

        os.makedirs(PLUGIN_DATA, exist_ok=True)
        await ctx.storage_state(path=state_file)
        try:
            os.chmod(state_file, 0o600)
        except Exception:  # noqa: BLE001
            pass

        cookies = (await ctx.cookies())
        names = sorted({c["name"] for c in cookies})
        print(f"\n✅ 登录态已保存: {state_file}")
        print(f"   cookie 数量: {len(cookies)}")
        print(f"   关键项: {', '.join(names[:8])}")
        print("\n现在可以在 AstrBot 里用浏览器模式了（无需重启，下次对话自动读取）")

        await browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
