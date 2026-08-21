DEEPSEEK_HOST = "chat.deepseek.com"
DEEPSEEK_LOGIN_URL = f"https://{DEEPSEEK_HOST}/api/v0/users/login"
DEEPSEEK_CREATE_SESSION_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat_session/create"
DEEPSEEK_CREATE_POW_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat/create_pow_challenge"
DEEPSEEK_COMPLETION_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat/completion"
DEEPSEEK_FILE_UPLOAD_URL = f"https://{DEEPSEEK_HOST}/api/v0/file/upload_file"
DEEPSEEK_FILE_FETCH_URL = f"https://{DEEPSEEK_HOST}/api/v0/file/fetch_files"
DEEPSEEK_STOP_STREAM_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat/stop_stream"
DEEPSEEK_DELETE_SESSION_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat_session/delete"
HIF_DLIQ_URL = "https://hif-dliq.deepseek.com/query"
HIF_LEIM_URL = "https://hif-leim.deepseek.com/query"
BASE_HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "cache-control": "no-cache",
    "content-type": "application/json",
    "pragma": "no-cache",
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "x-app-version": "20241129.1",
    "x-client-locale": "zh_CN",
    "x-client-platform": "web",
    "x-client-timezone-offset": "28800",
    "x-client-version": "1.8.0",
}
import os as _os
# 插件根目录（core/ 的上一级）
_PLUGIN_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
WASM_PATH = _os.path.join(_PLUGIN_DIR, "sha3_wasm_bg.7b9ca65ddd.wasm")
KEEP_ALIVE_TIMEOUT = 1.7
PROMPT_UPLOAD_THRESHOLD = 24000
PROMPT_UPLOAD_POLL_RETRIES = 20
PROMPT_UPLOAD_POLL_INTERVAL = 0.5


def get_account_identifier(account):
    """返回账号的唯一标识，优先使用 email，否则使用 mobile"""
    return account.get("email", "").strip() or account.get("mobile", "").strip()
