# -*- coding: utf-8 -*-
"""浏览器内核（Chromium）下载与安装。

浏览器模式需要完整的 Chromium（不是 Playwright 默认的 headless-shell）。
本模块负责：
  - 从 Playwright 官方 CDN 下载 chrome-win64.zip
  - 流式写入并每 10 秒写一次进度日志（已下载/总量、百分比、速度）
  - 解压到 ms-playwright/chromium-<revision>/ 并校验 chrome.exe

不做断点续传：失败即整体重试，逻辑保持简单。
"""

import json
import os
import shutil
import time
import zipfile

from astrbot.api import logger

# Playwright 官方 CDN（主源 + 微软 ESRP 备用源）
# 注意：URL 路径里用的是 Chrome for Testing 的 browserVersion（如 151.0.7922.34），
# 不是 browsers.json 里的 revision（如 1234）。用 revision 会 404。
CDN_MIRRORS = [
    "https://cdn.playwright.dev/builds/cft/{version}/win64/chrome-win64.zip",
    "https://playwright.download.prss.microsoft.com/dbazure/download/playwright/builds/cft/{version}/win64/chrome-win64.zip",
    "https://storage.googleapis.com/chrome-for-testing-public/{version}/win64/chrome-win64.zip",
]

# 进度日志间隔（秒）
PROGRESS_INTERVAL = 10

# 默认值：与 playwright 1.62.0 的 browsers.json 一致。
# 运行时优先从已安装的 playwright 读取真实值，读不到才用这个兜底。
DEFAULT_CHROMIUM_REVISION = "1234"
DEFAULT_CHROMIUM_VERSION = "151.0.7922.34"

CHUNK_SIZE = 256 * 1024


def ms_playwright_dir() -> str:
    """Playwright 的浏览器安装根目录（与 playwright 自身保持一致）。"""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env and env != "0":
        return env
    return os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright")


def chromium_info() -> tuple[str, str]:
    """读取当前 Playwright 使用的 chromium 信息，返回 (revision, browserVersion)。

    revision 决定本地安装目录名（chromium-<revision>），
    browserVersion 决定 CDN 下载路径（builds/cft/<version>/...）。
    """
    try:
        import playwright as _pw

        base = os.path.dirname(os.path.abspath(_pw.__file__))
        browsers_json = os.path.join(base, "driver", "package", "browsers.json")
        with open(browsers_json, encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("browsers", []):
            if item.get("name") == "chromium":
                rev = str(item.get("revision") or "").strip()
                ver = str(item.get("browserVersion") or "").strip()
                if rev and ver:
                    return rev, ver
    except Exception as e:  # noqa: BLE001
        logger.debug("[Setup] 读取 chromium 信息失败，用默认值: %s", e)
    return DEFAULT_CHROMIUM_REVISION, DEFAULT_CHROMIUM_VERSION


def chromium_revision() -> str:
    """本地安装目录名用的 revision。"""
    return chromium_info()[0]


def chromium_version() -> str:
    """CDN 下载路径用的 Chrome for Testing 版本号。"""
    return chromium_info()[1]


def chromium_exe_path(revision: str | None = None) -> str:
    """推算解压后 chrome.exe 的绝对路径。"""
    rev = revision or chromium_revision()
    return os.path.join(
        ms_playwright_dir(), f"chromium-{rev}", "chrome-win64", "chrome.exe"
    )


def is_chromium_installed(revision: str | None = None) -> bool:
    return os.path.isfile(chromium_exe_path(revision))


def _fmt_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def _fmt_eta(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds or seconds == float("inf"):
        return "--"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _download_once(url: str, dest_file: str, timeout: int = 60) -> None:
    """单线程流式下载（Range 不可用时的回退路径）。"""
    import httpx

    start = time.time()
    last_report = 0.0
    downloaded = 0
    total = 0

    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as resp:
        resp.raise_for_status()
        try:
            total = int(resp.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            total = 0

        logger.info(
            "[浏览器内核] 开始下载(单线程): %s (总大小 %s)",
            url,
            _fmt_size(total) if total else "未知",
        )

        with open(dest_file, "wb") as f:
            for chunk in resp.iter_bytes(CHUNK_SIZE):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)

                now = time.time()
                if now - last_report < PROGRESS_INTERVAL:
                    continue
                last_report = now

                elapsed = max(now - start, 0.001)
                speed = downloaded / elapsed
                if total:
                    pct = downloaded * 100.0 / total
                    remaining = (total - downloaded) / speed if speed > 0 else 0
                    logger.info(
                        "[浏览器内核] 下载中 %.1f%% (%s / %s) 速度 %s/s 剩余约 %s",
                        pct,
                        _fmt_size(downloaded),
                        _fmt_size(total),
                        _fmt_size(speed),
                        _fmt_eta(remaining),
                    )
                else:
                    logger.info(
                        "[浏览器内核] 下载中 已下载 %s 速度 %s/s",
                        _fmt_size(downloaded),
                        _fmt_size(speed),
                    )

    elapsed = max(time.time() - start, 0.001)
    speed = downloaded / elapsed
    logger.info(
        "[浏览器内核] 下载完成 %s (平均 %s/s, 耗时 %s)",
        _fmt_size(downloaded),
        _fmt_size(speed),
        _fmt_eta(elapsed),
    )


def _probe_size(url: str, timeout: int = 30) -> tuple[int, bool]:
    """探测文件总大小与是否支持 Range，返回 (size, supports_range)。"""
    import httpx

    with httpx.Client(follow_redirects=True, timeout=timeout) as c:
        # 先用 Range 试探
        try:
            r = c.get(url, headers={"Range": "bytes=0-1023"})
            if r.status_code == 206:
                cr = r.headers.get("content-range", "")
                # content-range: bytes 0-1023/201068834
                if "/" in cr:
                    total = cr.rsplit("/", 1)[1].strip()
                    if total.isdigit() and int(total) > 0:
                        return int(total), True
        except Exception:  # noqa: BLE001
            pass

        try:
            r = c.head(url)
            size = int(r.headers.get("content-length") or 0)
            accept = (r.headers.get("accept-ranges") or "").lower()
            return size, ("bytes" in accept)
        except Exception:  # noqa: BLE001
            pass
    return 0, False


def _download_multipart(url: str, dest_file: str, workers: int = 8, timeout: int = 60) -> bool:
    """多线程分片下载。返回 True 表示成功，False 表示不可用需回退单线程。

    速度慢的单连接下（实测 250-330 KB/s）分片并发可显著提速。
    """
    import httpx
    from concurrent.futures import ThreadPoolExecutor

    size, supports = _probe_size(url)
    if not size or not supports:
        logger.info("[浏览器内核] 服务器不支持分片下载，回退单线程")
        return False

    logger.info(
        "[浏览器内核] 开始下载(多线程 %d 分片): %s (总大小 %s)",
        workers,
        url,
        _fmt_size(size),
    )

    seg_size = size // workers
    ranges = []
    for i in range(workers):
        start = i * seg_size
        end = size - 1 if i == workers - 1 else (start + seg_size - 1)
        ranges.append((start, end))

    start_time = time.time()
    done_flags = [0] * workers
    progress = {"done": 0}
    lock = __import__("threading").Lock()
    error = []

    def fetch(idx: int, start: int, end: int) -> None:
        part_path = f"{dest_file}.part{idx}"
        received = 0
        try:
            with httpx.stream(
                "GET",
                url,
                headers={"Range": f"bytes={start}-{end}"},
                follow_redirects=True,
                timeout=timeout,
            ) as resp:
                if resp.status_code != 206:
                    raise RuntimeError(f"分片 {idx} 返回 {resp.status_code}")
                with open(part_path, "wb") as f:
                    for chunk in resp.iter_bytes(CHUNK_SIZE):
                        if not chunk:
                            continue
                        f.write(chunk)
                        received += len(chunk)
                        with lock:
                            progress["done"] += len(chunk)
        except Exception as e:  # noqa: BLE001
            error.append(f"分片{idx}: {type(e).__name__}: {e}")
            raise
        finally:
            done_flags[idx] = received

    # 进度上报线程
    stop_flag = {"stop": False}

    def reporter() -> None:
        last_report = 0.0
        while not stop_flag["stop"]:
            time.sleep(1)
            now = time.time()
            if now - last_report < PROGRESS_INTERVAL:
                continue
            last_report = now
            with lock:
                done = progress["done"]
            elapsed = max(now - start_time, 0.001)
            speed = done / elapsed
            pct = done * 100.0 / size
            remaining = (size - done) / speed if speed > 0 else 0
            logger.info(
                "[浏览器内核] 下载中 %.1f%% (%s / %s) 速度 %s/s 剩余约 %s",
                pct,
                _fmt_size(done),
                _fmt_size(size),
                _fmt_size(speed),
                _fmt_eta(remaining),
            )

    rep = __import__("threading").Thread(target=reporter, daemon=True)
    rep.start()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch, i, s, e) for i, (s, e) in enumerate(ranges)]
            for f in futures:
                f.result()
    except Exception as e:  # noqa: BLE001
        stop_flag["stop"] = True
        logger.warning("[浏览器内核] 分片下载失败: %s", e)
        # 清理分片
        for i in range(workers):
            try:
                os.remove(f"{dest_file}.part{i}")
            except OSError:
                pass
        return False
    finally:
        stop_flag["stop"] = True
        # 等进度线程真正退出，避免它在合并/解压阶段再打一条过期的进度日志
        rep.join(timeout=2)

    # 合并分片
    logger.info("[浏览器内核] 正在合并分片 ...")
    try:
        with open(dest_file, "wb") as out:
            for i in range(workers):
                part = f"{dest_file}.part{i}"
                with open(part, "rb") as pf:
                    shutil.copyfileobj(pf, out)
                os.remove(part)
    except Exception as e:  # noqa: BLE001
        logger.warning("[浏览器内核] 合并分片失败: %s", e)
        return False

    merged = os.path.getsize(dest_file)
    if merged != size:
        logger.warning("[浏览器内核] 合并后大小不符 (%d != %d)", merged, size)
        return False

    elapsed = max(time.time() - start_time, 0.001)
    logger.info(
        "[浏览器内核] 下载完成 100%% (%s / %s) 平均速度 %s/s 耗时 %s",
        _fmt_size(merged),
        _fmt_size(size),
        _fmt_size(merged / elapsed),
        _fmt_eta(elapsed),
    )
    return True


def _extract(zip_path: str, dest_dir: str) -> None:
    """解压 zip 到目标目录（先解到临时目录，成功后替换）。"""
    tmp_dir = dest_dir + ".tmp"
    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)

    logger.info("[浏览器内核] 正在解压到 %s ...", dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tmp_dir)

    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
    os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
    shutil.move(tmp_dir, dest_dir)
    logger.info("[浏览器内核] 解压完成")


def download_chromium(revision: str | None = None, force: bool = False) -> dict:
    """下载并安装 Chromium。返回 {'ok': bool, 'path': str, 'message': str}。

    同步函数：调用方应放到线程里执行，避免阻塞事件循环。
    """
    rev = revision or chromium_revision()
    version = chromium_version()
    exe = chromium_exe_path(rev)

    if os.path.isfile(exe) and not force:
        logger.info("[浏览器内核] 已安装，无需下载: %s", exe)
        return {"ok": True, "path": exe, "message": "内核已安装，无需重复下载"}

    target_dir = os.path.dirname(os.path.dirname(exe))
    os.makedirs(os.path.dirname(target_dir), exist_ok=True)
    zip_path = os.path.join(os.path.dirname(target_dir), f"chromium-{rev}.zip")

    logger.info("[浏览器内核] 目标路径: %s", exe)
    logger.info("[浏览器内核] Chrome for Testing 版本: %s (revision %s)", version, rev)

    last_err = None
    for idx, tmpl in enumerate(CDN_MIRRORS, 1):
        url = tmpl.format(version=version)
        logger.info("[浏览器内核] 尝试下载源 %d/%d: %s", idx, len(CDN_MIRRORS), url)
        try:
            # 优先多线程分片（速度明显更快），不可用则回退单线程
            if not _download_multipart(url, zip_path):
                _download_once(url, zip_path)
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("[浏览器内核] 下载源 %d 失败: %s: %s", idx, type(e).__name__, e)
            continue

        try:
            _extract(zip_path, target_dir)
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("[浏览器内核] 解压失败: %s: %s", type(e).__name__, e)
            continue
        finally:
            try:
                os.remove(zip_path)
            except OSError:
                pass

        if os.path.isfile(exe):
            logger.info("[浏览器内核] 安装成功: %s", exe)
            return {"ok": True, "path": exe, "message": f"内核安装成功: {exe}"}

        last_err = RuntimeError("解压后未找到 chrome.exe")
        logger.warning("[浏览器内核] %s", last_err)

    msg = f"内核下载失败（已尝试 {len(CDN_MIRRORS)} 个下载源）: {type(last_err).__name__ if last_err else '未知'}: {last_err}"
    logger.error("[浏览器内核] %s", msg)
    return {"ok": False, "path": "", "message": msg}
