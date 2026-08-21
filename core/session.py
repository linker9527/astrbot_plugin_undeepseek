import logging

from curl_cffi import requests

from . import constants

logger = logging.getLogger(__name__)

_token_session = None  # token 模式下的全局 session


def init_account_session(account):
    """为账号初始化 curl_cffi Session，先访问主页初始化 Cookie"""
    session = requests.Session()
    try:
        resp = session.get(
            "https://chat.deepseek.com/",
            headers={k: v for k, v in constants.BASE_HEADERS.items() if k != "content-type"},
            impersonate="safari15_3",
            timeout=15,
        )
        logger.info(
            f"[init_account_session] 账号 {constants.get_account_identifier(account)} 主页初始化完成, status={resp.status_code}"
        )
        resp.close()
    except Exception as e:
        logger.warning(f"[init_account_session] 主页初始化失败: {e}")
    account["_session"] = session
    return session


def get_account_session(account):
    """获取账号关联的 Session，不存在则创建"""
    session = account.get("_session")
    if session is None:
        session = init_account_session(account)
    return session


def get_request_session(request):
    """从 request.state 获取 Session（配置模式取 account 的，token 模式用全局）"""
    if getattr(request.state, "use_config_token", False):
        account = getattr(request.state, "account", None)
        if account:
            return get_account_session(account)
    # token 模式 — 全局单例
    global _token_session
    if _token_session is None:
        _token_session = requests.Session()
        try:
            resp = _token_session.get(
                "https://chat.deepseek.com/",
                headers={k: v for k, v in constants.BASE_HEADERS.items() if k != "content-type"},
                impersonate="safari15_3",
                timeout=15,
            )
            resp.close()
        except Exception as e:
            logger.warning(f"[get_request_session] 全局 session 主页初始化失败: {e}")
    return _token_session
