import base64
import ctypes
import json
import logging
import struct
import time

from wasmtime import Linker, Module, Store

from . import constants, session as session_module
from .account import choose_new_account, login_deepseek_via_account, release_account
from .constants import get_account_identifier

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 使用 WASM 模块计算 PoW 答案的辅助函数
# ----------------------------------------------------------------------
def compute_pow_answer(
    algorithm: str,
    challenge_str: str,
    salt: str,
    difficulty: int,
    expire_at: int,
    signature: str,
    target_path: str,
    wasm_path: str,
) -> int:
    """
    使用 WASM 模块计算 DeepSeekHash 答案（answer）。
    根据 JS 逻辑：
      - 拼接前缀： "{salt}_{expire_at}_"
      - 将 challenge 与前缀写入 wasm 内存后调用 wasm_solve 进行求解，
      - 从 wasm 内存中读取状态与求解结果，
      - 若状态非 0，则返回整数形式的答案，否则返回 None。
    """
    if algorithm != "DeepSeekHashV1":
        raise ValueError(f"不支持的算法：{algorithm}")
    prefix = f"{salt}_{expire_at}_"
    # --- 加载 wasm 模块 ---
    store = Store()
    linker = Linker(store.engine)
    try:
        with open(wasm_path, "rb") as f:
            wasm_bytes = f.read()
    except Exception as e:
        raise RuntimeError(f"加载 wasm 文件失败: {wasm_path}, 错误: {e}")
    module = Module(store.engine, wasm_bytes)
    instance = linker.instantiate(store, module)
    exports = instance.exports(store)
    try:
        memory = exports["memory"]
        add_to_stack = exports["__wbindgen_add_to_stack_pointer"]
        alloc = exports["__wbindgen_export_0"]
        wasm_solve = exports["wasm_solve"]
    except KeyError as e:
        raise RuntimeError(f"缺少 wasm 导出函数: {e}")

    def write_memory(offset: int, data: bytes):
        size = len(data)
        base_addr = ctypes.cast(memory.data_ptr(store), ctypes.c_void_p).value
        ctypes.memmove(base_addr + offset, data, size)

    def read_memory(offset: int, size: int) -> bytes:
        base_addr = ctypes.cast(memory.data_ptr(store), ctypes.c_void_p).value
        return ctypes.string_at(base_addr + offset, size)

    def encode_string(text: str):
        data = text.encode("utf-8")
        length = len(data)
        ptr_val = alloc(store, length, 1)
        ptr = int(ptr_val.value) if hasattr(ptr_val, "value") else int(ptr_val)
        write_memory(ptr, data)
        return ptr, length

    # 1. 申请 16 字节栈空间
    retptr = add_to_stack(store, -16)
    # 2. 编码 challenge 与 prefix 到 wasm 内存中
    ptr_challenge, len_challenge = encode_string(challenge_str)
    ptr_prefix, len_prefix = encode_string(prefix)
    # 3. 调用 wasm_solve（注意：difficulty 以 float 形式传入）
    wasm_solve(
        store,
        retptr,
        ptr_challenge,
        len_challenge,
        ptr_prefix,
        len_prefix,
        float(difficulty),
    )
    # 4. 从 retptr 处读取 4 字节状态和 8 字节求解结果
    status_bytes = read_memory(retptr, 4)
    if len(status_bytes) != 4:
        add_to_stack(store, 16)
        raise RuntimeError("读取状态字节失败")
    status = struct.unpack("<i", status_bytes)[0]
    value_bytes = read_memory(retptr + 8, 8)
    if len(value_bytes) != 8:
        add_to_stack(store, 16)
        raise RuntimeError("读取结果字节失败")
    value = struct.unpack("<d", value_bytes)[0]
    # 5. 恢复栈指针
    add_to_stack(store, 16)
    if status == 0:
        return None
    return int(value)


# ----------------------------------------------------------------------
# 获取 PoW 响应，融合计算 answer 逻辑
# ----------------------------------------------------------------------
def get_pow_response(request, max_attempts=6, target_path="/api/v0/chat/completion"):
    attempts = 0
    RETRY_DELAYS = [1, 2, 4, 8, 16, 32]
    while attempts < max_attempts:
        headers = {
            **constants.BASE_HEADERS,
            "authorization": f"Bearer {request.state.deepseek_token}",
        }
        ds_session = session_module.get_request_session(request)
        try:
            resp = ds_session.post(
                constants.DEEPSEEK_CREATE_POW_URL,
                headers=headers,
                json={"target_path": target_path},
                timeout=30,
                impersonate="safari15_3",
            )
        except Exception as e:
            wait = RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)]
            logger.error(f"[get_pow_response] 请求异常 (尝试 {attempts + 1}/{max_attempts}): {e}, 等待 {wait}s")
            time.sleep(wait)
            attempts += 1
            continue
        try:
            data = resp.json()
        except Exception as e:
            body_preview = getattr(resp, "text", "")[:300]
            logger.error(
                f"[get_pow_response] JSON解析异常: {e}, status={resp.status_code}, target={target_path}, body={body_preview!r}"
            )
            data = {}
        if resp.status_code == 200 and data.get("code") == 0:
            challenge = data["data"]["biz_data"]["challenge"]
            difficulty = challenge.get("difficulty", 144000)
            expire_at = challenge.get("expire_at", 1680000000)
            try:
                answer = compute_pow_answer(
                    challenge["algorithm"],
                    challenge["challenge"],
                    challenge["salt"],
                    difficulty,
                    expire_at,
                    challenge["signature"],
                    challenge["target_path"],
                    constants.WASM_PATH,
                )
            except Exception as e:
                logger.error(f"[get_pow_response] PoW 答案计算异常: {e}")
                answer = None
            if answer is None:
                logger.warning("[get_pow_response] PoW 答案计算失败，重试中...")
                resp.close()
                attempts += 1
                continue
            pow_dict = {
                "algorithm": challenge["algorithm"],
                "challenge": challenge["challenge"],
                "salt": challenge["salt"],
                "answer": answer,
                "signature": challenge["signature"],
                "target_path": challenge["target_path"],
            }
            pow_str = json.dumps(pow_dict, separators=(",", ":"), ensure_ascii=False)
            encoded = base64.b64encode(pow_str.encode("utf-8")).decode("utf-8").rstrip()
            resp.close()
            return encoded
        else:
            code = data.get("code")
            logger.warning(
                f"[get_pow_response] 获取 PoW 失败, target={target_path}, status={resp.status_code}, code={code}, msg={data.get('msg')}"
            )
            resp.close()
            if getattr(request.state, "use_config_token", False):
                current_id = get_account_identifier(request.state.account)
                if not hasattr(request.state, "tried_accounts"):
                    request.state.tried_accounts = []
                if current_id not in request.state.tried_accounts:
                    request.state.tried_accounts.append(current_id)
                release_account(request.state.account)
                new_account = choose_new_account(request.state.tried_accounts)
                if new_account is None:
                    break
                try:
                    login_deepseek_via_account(new_account)
                except Exception as e:
                    logger.error(
                        f"[get_pow_response] 账号 {get_account_identifier(new_account)} 登录失败：{e}"
                    )
                    release_account(new_account)
                    attempts += 1
                    continue
                request.state.account = new_account
                request.state.deepseek_token = new_account.get("token")
            else:
                attempts += 1
                continue
            attempts += 1
    return None
