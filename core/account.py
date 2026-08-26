import json
import logging
import random
import socket
import ssl
import time

from curl_cffi import requests
from fastapi import HTTPException, Request

from . import config, constants, session as session_module
from .pool import account_pool

logger = logging.getLogger(__name__)

# HIF token 刷新间隔（秒）
HIF_TOKEN_TTL = 4 * 3600

# ----------------------------------------------------------------------
# 初始化账号池
# ----------------------------------------------------------------------
def init_account_queue():
    """初始化时从配置加载账号并填入账号池。"""
    accounts = config.CONFIG.get("accounts", [])[:]
    random.shuffle(accounts)
    account_pool.load(accounts)


def choose_new_account(exclude_ids=None):
    """兼容旧接口 —— 从池中获取一个账号（不自动释放）。"""
    account, _guard = account_pool.acquire(exclude_ids)
    if account:
        # 把 guard 存到 account 上，由 release_account 负责释放
        account["_guard"] = _guard
    return account


def release_account(account):
    """兼容旧接口 —— 释放账号。如果已通过 _guard 释放则忽略。"""
    if account is None:
        return
    guard = account.get("_guard")
    if guard:
        guard.__exit__(None, None, None)
        account["_guard"] = None


# ----------------------------------------------------------------------
# 登录函数
# ----------------------------------------------------------------------
def login_deepseek_via_account(account):
    email = account.get("email", "").strip()
    mobile = account.get("mobile", "").strip()
    password = account.get("password", "").strip()
    if not password or (not email and not mobile):
        raise HTTPException(
            status_code=400,
            detail="账号缺少必要的登录信息（必须提供 email 或 mobile 以及 password）",
        )

    session = session_module.get_account_session(account)

    if email:
        payload = {
            "email": email,
            "password": password,
            "device_id": "deepseek_to_api",
            "os": "android",
        }
    else:
        payload = {
            "mobile": mobile,
            "area_code": None,
            "password": password,
            "device_id": "deepseek_to_api",
            "os": "android",
        }
    try:
        resp = session.post(
            constants.DEEPSEEK_LOGIN_URL,
            headers=constants.BASE_HEADERS,
            json=payload,
            impersonate="safari15_3",
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"[login_deepseek_via_account] 登录请求异常: {e}")
        raise HTTPException(status_code=500, detail="Account login failed: 请求异常")
    try:
        data = resp.json()
    except Exception as e:
        logger.error(f"[login_deepseek_via_account] JSON解析失败: {e}")
        raise HTTPException(
            status_code=500, detail="Account login failed: invalid JSON response"
        )
    if (
        data.get("data") is None
        or data["data"].get("biz_data") is None
        or data["data"]["biz_data"].get("user") is None
    ):
        logger.error(f"[login_deepseek_via_account] 登录响应格式错误: {data}")
        raise HTTPException(
            status_code=500, detail="Account login failed: invalid response format"
        )
    new_token = data["data"]["biz_data"]["user"].get("token")
    if not new_token:
        logger.error(f"[login_deepseek_via_account] 登录响应中缺少 token: {data}")
        raise HTTPException(
            status_code=500, detail="Account login failed: missing token"
        )
    account["token"] = new_token
    config.save_config(config.CONFIG)

    ensure_hif_tokens(account, force=True)

    # 登录成功后必须拿到 HIF 令牌，否则后续请求必然失败
    if not account.get("hif_dliq") or not account.get("hif_leim"):
        if not ensure_hif_tokens(account, force=True):
            logger.error(f"[login_deepseek_via_account] 账号 {email or mobile} 登录成功但 HIF 令牌获取失败")
            raise HTTPException(
                status_code=500, detail="Account login succeeded but HIF token fetch failed"
            )

    return new_token


# ----------------------------------------------------------------------
# HIF 令牌获取
# ----------------------------------------------------------------------
def _fetch_hif_via_socket(url, token_name, account):
    """DNS 被污染时的兜底：手动 socket 连接备用 IP + 正确 SNI 拿 token。"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname
    path = parsed.path or "/"
    backup_ip = "60.204.2.4"
    auth = ""
    if account and account.get("token"):
        auth = f"Bearer {account['token']}"
    try:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((backup_ip, 443), timeout=15)
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            req_lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
            if auth:
                req_lines.append(f"Authorization: {auth}")
            CRLF = chr(13) + chr(10)
            req = (CRLF.join(req_lines) + CRLF + CRLF).encode()
            s.sendall(req)
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        head, _, body = data.partition(bytes([13,10,13,10]))
        status = head.decode(errors="ignore").split(chr(13)+chr(10))[0]
        if "200" not in status:
            logger.warning(f"[fetch_hif_token] 兜底 {token_name} {status}")
            return None
        data_json = json.loads(body)
        value = data_json.get("data", {}).get("biz_data", {}).get("value")
        if value:
            logger.info(f"[fetch_hif_token] 兜底成功获取 {token_name}")
            return value
        logger.warning(f"[fetch_hif_token] 兜底 {token_name} 响应缺少 value: {data_json}")
    except Exception as e:
        logger.error(f"[fetch_hif_token] 兜底 {token_name} 异常: {e}")
    return None


def fetch_hif_token(session, url, token_name, account=None):
    try:
        resp = session.get(
            url,
            headers=constants.BASE_HEADERS,
            impersonate="safari15_3",
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                value = data.get("data", {}).get("biz_data", {}).get("value")
                if value:
                    logger.info(f"[fetch_hif_token] 成功获取 {token_name}")
                    return value
                else:
                    logger.warning(f"[fetch_hif_token] {token_name} 响应中缺少 value: {data}")
            else:
                logger.warning(f"[fetch_hif_token] {token_name} 业务错误 code={data.get('code')}")
        else:
            logger.warning(f"[fetch_hif_token] {token_name} HTTP {resp.status_code}")
        resp.close()
    except Exception as e:
        err = str(e)
        if "resolve host" in err or "DNSError" in type(e).__name__:
            logger.warning(f"[fetch_hif_token] {token_name} DNS 解析失败，启用 IP 兜底")
            return _fetch_hif_via_socket(url, token_name, account)
        logger.error(f"[fetch_hif_token] {token_name} 异常: {e}")
    return None


def ensure_hif_tokens(account, force=False):
    now = time.time()
    # 两个 token 都在且 4 小时内刷新过 → 直接用（省请求）
    if not force and account.get("hif_dliq") and account.get("hif_leim"):
        last = account.get("hif_refreshed_at", 0)
        if now - last < HIF_TOKEN_TTL:
            return True

    session = session_module.get_account_session(account)

    logger.info(f"[ensure_hif_tokens] 账号 {constants.get_account_identifier(account)} 开始获取 HIF 令牌")
    dliq = fetch_hif_token(session, constants.HIF_DLIQ_URL, "x-hif-dliq", account)
    leim = fetch_hif_token(session, constants.HIF_LEIM_URL, "x-hif-leim", account)

    if dliq:
        account["hif_dliq"] = dliq
    if leim:
        account["hif_leim"] = leim
    if dliq and leim:
        account["hif_refreshed_at"] = now
        account["hif_fetch_fail_count"] = 0
        config.save_config(config.CONFIG)

    if dliq and leim:
        logger.info(f"[ensure_hif_tokens] 账号 {constants.get_account_identifier(account)} HIF 令牌已就绪")
        return True
    else:
        fails = account.get("hif_fetch_fail_count", 0) + 1
        account["hif_fetch_fail_count"] = fails
        account["hif_last_fetch_fail_at"] = time.time()
        logger.warning(f"[ensure_hif_tokens] 账号 {constants.get_account_identifier(account)} HIF 令牌不完整（连续第 {fails} 次失败）")
        return False


# ----------------------------------------------------------------------
# 判断调用模式：配置模式 vs 用户自带 token
# ----------------------------------------------------------------------
def determine_mode_and_token(request: Request, allow_x_api_key: bool = False):
    auth_header = request.headers.get("Authorization", "")

    if auth_header.startswith("Bearer "):
        caller_key = auth_header.replace("Bearer ", "", 1).strip()
    elif allow_x_api_key:
        x_key = request.headers.get("x-api-key", "")
        if x_key:
            caller_key = x_key.strip()
        else:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized: missing Authorization Bearer token or x-api-key.",
            )
    else:
        raise HTTPException(
            status_code=401, detail="Unauthorized: missing Bearer token."
        )
    config_keys = config.CONFIG.get("keys", [])
    if caller_key in config_keys:
        request.state.use_config_token = True
        request.state.tried_accounts = []
        selected_account = choose_new_account()
        if not selected_account:
            raise HTTPException(
                status_code=429,
                detail="No accounts configured or all accounts are busy.",
            )
        need_login = not selected_account.get("token", "").strip()
        if need_login:
            try:
                login_deepseek_via_account(selected_account)
            except Exception as e:
                logger.error(
                    f"[determine_mode_and_token] 账号 {constants.get_account_identifier(selected_account)} 登录失败：{e}"
                )
                release_account(selected_account)
                raise HTTPException(status_code=500, detail="Account login failed.")
        else:
            ensure_hif_tokens(selected_account)

        request.state.deepseek_token = selected_account.get("token")
        request.state.account = selected_account

    else:
        request.state.use_config_token = False
        request.state.deepseek_token = caller_key


def get_auth_headers(request: Request):
    return {**constants.BASE_HEADERS, "authorization": f"Bearer {request.state.deepseek_token}"}


def get_hif_headers(request: Request):
    headers = {}
    account = getattr(request.state, "account", None)
    if account:
        if account.get("hif_dliq"):
            headers["x-hif-dliq"] = account["hif_dliq"]
        if account.get("hif_leim"):
            headers["x-hif-leim"] = account["hif_leim"]
    return headers


# ----------------------------------------------------------------------
# 账号健康检查（启动时）
# ----------------------------------------------------------------------
def account_health_check(account, timeout=30):
    """对单个账号执行健康检查：登录 → 创建会话 → PoW → 发一条 mini completion 并消费完 stream → 删除会话。

    成功返回 True，失败返回 False。
    """
    from .chat import create_session, messages_prepare, call_completion_endpoint
    from .pow import get_pow_response
    import time as _time

    acc_id = constants.get_account_identifier(account)
    logger.info(f"[health_check] 开始健康检查账号: {acc_id}")

    try:
        # 1. 登录
        if not account.get("token"):
            login_deepseek_via_account(account)
        else:
            ensure_hif_tokens(account)

        # 2. 创建会话 —— 需要伪造一个 request 对象
        class _FakeRequest:
            class state:
                deepseek_token = account.get("token")
                account = account
                use_config_token = True
        fake_req = _FakeRequest()

        session_id = create_session(fake_req)
        if not session_id:
            logger.warning(f"[health_check] {acc_id} 创建会话失败")
            return False

        # 3. 获取 PoW
        pow_resp = get_pow_response(fake_req)
        if not pow_resp:
            logger.warning(f"[health_check] {acc_id} 获取 PoW 失败")
            _delete_session_for_health(account, session_id, fake_req)
            return False

        # 4. 发送一条 mini completion
        test_prompt = messages_prepare([{"role": "user", "content": "Hello, world!"}])
        headers = {
            **get_auth_headers(fake_req),
            **get_hif_headers(fake_req),
            "x-ds-pow-response": pow_resp,
        }
        payload = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "model_type": "default",
            "prompt": test_prompt,
            "ref_file_ids": [],
            "thinking_enabled": True,
            "search_enabled": False,
            "preempt": False,
        }

        ds_session = session_module.get_account_session(account)
        resp = call_completion_endpoint(payload, headers, ds_session, max_attempts=2)
        if not resp or resp.status_code != 200:
            logger.warning(f"[health_check] {acc_id} completion 请求失败")
            _delete_session_for_health(account, session_id, fake_req)
            if resp:
                resp.close()
            return False

        # 5. 消费 stream 直到结束
        try:
            for raw_line in resp.iter_lines():
                pass  # 只需要确认能收到数据
        except Exception as e:
            logger.warning(f"[health_check] {acc_id} stream 消费异常: {e}")
        finally:
            resp.close()

        # 6. 删除会话
        _delete_session_for_health(account, session_id, fake_req)

        logger.info(f"[health_check] {acc_id} 健康检查通过")
        return True

    except Exception as e:
        logger.warning(f"[health_check] {acc_id} 健康检查异常: {e}")
        return False


def _delete_session_for_health(account, session_id, fake_req):
    try:
        headers = {
            **constants.BASE_HEADERS,
            "authorization": f"Bearer {account.get('token')}",
        }
        ds_session = session_module.get_account_session(account)
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


def run_health_checks(max_concurrent=10):
    """启动时并发执行所有账号的健康检查，丢弃不健康的账号。"""
    import threading

    all_accounts = account_pool.all_accounts()
    if not all_accounts:
        logger.warning("[health_check] 没有可用的账号")
        return

    semaphore = threading.Semaphore(max_concurrent)
    failed = []
    passed = []

    def check_one(acct):
        semaphore.acquire()
        try:
            if account_health_check(acct):
                passed.append(acct)
            else:
                failed.append(acct)
        finally:
            semaphore.release()

    threads = []
    for acct in all_accounts:
        t = threading.Thread(target=check_one, args=(acct,))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    # 用健康的账号重建池
    if passed:
        account_pool.load(passed)
        logger.info(f"[health_check] {len(passed)} 个账号通过健康检查")
    if failed:
        logger.warning(f"[health_check] {len(failed)} 个账号未通过健康检查: "
                       f"{[constants.get_account_identifier(a) for a in failed]}")

    # 如果所有账号都失败，仍然保留原始配置（降级运行）
    if not passed:
        logger.error("[health_check] 所有账号均未通过健康检查，降级使用原始配置")
        account_pool.load(all_accounts)
