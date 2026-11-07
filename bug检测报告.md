# undeepseek 1.7.1 Bug 检测报告

- **检测对象**：`C:\Users\ning\Desktop\马造2\undeepseek`（AstrBot 插件 `astrbot_plugin_undeepseek`，metadata 版本 1.7.1，git `289b7b5`）
- **检测日期**：2026-11-07
- **检测方式**：全量代码审查（`main.py` + `core/` 14 个模块 + `pages/` 配置页 + `manual_login.py`）＋ 纯函数级实测复现（Python 3.10，mock 掉 `astrbot.*` / `curl_cffi` / `wasmtime` 后直接调用真实函数）
- **说明**：只检测、不改代码。文中“实测”均给出可复现的输入与输出。
- **严重度**：🔴 高（功能损坏/数据错乱）｜🟡 中（特定条件下出错/资源泄漏）｜⚪ 低（代码卫生/健壮性）

---

## 概览

| # | 严重度 | 位置 | 问题 | 证据 |
|---|--------|------|------|------|
| 1 | 🔴 | `core/chat.py:951-975` | 非工具调用的 JSON 数组触发 `UnboundLocalError`，同一响应里后续的真实工具调用被静默吞掉 | ✅ 实测 |
| 2 | 🔴 | `core/chat.py:913-919` | 代码围栏检测是死代码，代码块里的 `<tool_call>` / `<function_calls>` 示例仍会被当真执行 | ✅ 实测 |
| 3 | 🔴 | `core/pool.py:38-42` | `load()` 用 `setdefault` 合并账号：配置改密码不生效；`_busy=True` 的账号重载后永久不可用 | ✅ 实测 |
| 4 | 🟡 | `core/account.py:326-409` | 健康检查换号后旧 session + 新 token 混用，且换来的账号永不释放（池位泄漏） | 代码审查 |
| 5 | 🟡 | `main.py:1462` | `/undeepseek online force` 未把 `force=True` 传下去，内核已存在时假报“下载完成” | ✅ 实测 |
| 6 | 🟡 | `main.py:103-184 / 190-265` | 单个畸形 SSE chunk 会让整条流解析抛异常，已收到的正文全部丢弃 | ✅ 实测 |
| 7 | 🟡 | `core/browser.py:640-679` | 浏览器模式按“账号”复用页面，不同 AstrBot 会话共享同一页面，上下文互相污染 | 代码审查 |
| 8 | 🟡 | `core/browser.py:717-742`、`core/page_api.py:118-162` | Playwright driver（`_pw`）从不 `stop()`，多处泄漏 node 子进程 | 代码审查 |
| 9 | 🟡 | `core/account.py:431-474` | 每次插件加载都全量健康检查（真实登录+completion），失败账号被永久移出池子 | 代码审查 |
| 10 | 🟡 | `core/browser.py:385-420` | 登录等待注释/报错写“20 秒”，实际最坏约 120 秒 | 代码审查 |
| 11 | 🟡 | `requirements.txt` | 缺 `httpx`（`setup.py` 下载内核依赖）与 `fastapi`（`core/account.py` 依赖） | 依赖核对 |
| 12 | ⚪ | 多处 | 死代码 / 未使用变量 / 重复定义（12 项） | grep 确认 |
| 13 | ⚪ | `manual_login.py` | 打印“检测到已有登录态，将尝试复用”但从不加载；路径硬编码 | 代码审查 |
| 14 | ⚪ | `pages/deepseek_reverse/index.html:96-100` | 配置页展示的模型与 `SUPPORTED_MODELS` 不一致 | 对比确认 |
| 15 | ⚪ | `core/chat.py:1051`、`core/account.py:79` | 关键 POST 无 timeout；`create_session` 对 200 响应直接下标，结构异常抛 TypeError | 代码审查 |
| 16 | ⚪ | `main.py:792`、`main.py:825` | 内核检测返回值被忽略；浏览器模式 input token 记为 0 | 代码审查 |
| 17 | ⚪ | `main.py:993-1062` | 客户端断开后流式 worker 仍继续跑，队列无界增长 | 代码审查 |
| 18 | ⚪ | `core/browser.py:682-695` | `replace_browser` 后旧浏览器 context 仍留在 `_account_sessions`，注释与实现不符 | 代码审查 |
| 19 | ⚪ | `core/page_api.py:164-211` | `login_check` 不校验登录账号与请求账号是否一致 | 代码审查 |
| 20 | ⚪ | `core/pending_login.py:50-58` | 公开清单写进插件包 `pages/` 目录（插件更新会覆盖，且可能只读） | 代码审查 |
| 21 | ⚪ | `core/config.pyc`、`core/models.pyc` | 陈旧字节码随包分发 | 文件核对 |
| 22 | ⚪ | `core/account_probe.py:23,144-159` | `PROBE_CACHE_TTL` 死常量；`browser_exe` 参数只用于判空，实际不生效 | 代码审查 |

---

## 🔴 高严重度

### 1. `detect_and_parse_tool_calls` 对普通 JSON 数组抛 `UnboundLocalError`，后续真实工具调用被吞掉

**位置**：`core/chat.py:951-975`；异常被 `main.py:526-535`（`_parse_tool_calls_from_text`）的 `except Exception: return None, text` 静默吞掉。

**问题**：`valid_calls` 只在“列表元素全部是 OpenAI function-call 形态”时赋值；否则列表分支不赋值也不 `continue`，直接走到 `if valid_calls:`，抛 `UnboundLocalError`。

```python
if isinstance(parsed, list):
    if parsed and all(...):
        valid_calls = _normalize_tool_calls(parsed)   # 只有这里赋值
elif not isinstance(parsed, dict):
    continue
...
if valid_calls:   # ← 列表分支未赋值时 UnboundLocalError
```

**实测**（直接调用真实函数）：

```
empty list -> EXC UnboundLocalError local variable 'valid_calls' referenced before assignment
plain object list -> EXC UnboundLocalError ...
mixed list -> EXC UnboundLocalError ...
array then real tool call -> EXC UnboundLocalError ...
real tool call alone -> tool_calls= True
```

**影响**：模型回复里只要出现一个普通 JSON 数组（数据列表、配置示例、`[{"name":...}]` 之类），工具调用检测就整体抛异常；如果同一回复后面还有真实的 `{"tool_calls":[...]}`，也不会被执行。用户侧表现为“工具偶尔不触发”，且日志里没有任何报错（异常被吞）。

**方向**：列表分支补 `else: continue`，或在循环开头初始化 `valid_calls = []`；同时建议 `main._parse_tool_calls_from_text` 至少记一条 debug 日志，不要完全静默。

---

### 2. 代码围栏检测是死代码，代码块里的工具调用示例会被当真执行

**位置**：`core/chat.py:913-919`（计算 `func_tag_idx` / `xml_tag_idx`）与 `921/933`（无条件调用解析）。

**问题**：`func_tag_idx`、`xml_tag_idx` 计算并做了围栏判断后就再也没有被使用；`_parse_function_calls_block(original_content)` 和 `_parse_xml_tool_calls(original_content)` 依旧无条件执行。`is_inside_code_fence()` 也是只被这段死逻辑调用。

**实测**：

```
xml tool_call in fence -> tool_calls= True  cleaned= 'Example:\n```\n\n```'
function_calls in fence -> tool_calls= True ...
xml tool_call not in fence -> tool_calls= True ...
```

**影响**：模型在 ``` 代码块里展示工具调用示例（教学、调试、解释格式）时，插件会把它当成真实调用执行。注册了工具的场景下可能触发意料之外的函数调用。

**方向**：把围栏判断接回解析入口（围栏内跳过 `function_calls` / `xml` 解析），或删掉这段死逻辑，避免误导后续维护者。

---

### 3. 账号池 `load()` 合并策略：改密码不生效 + `_busy` 账号重载后永久失效

**位置**：`core/pool.py:38-42`（`old.setdefault(k, v)`）、`core/pool.py:19-46`（整表重建）。调用点：`main.py:1169`（每次 `Main.__init__` 都调 `init_account_queue`）、`core/account.py:465/474`（健康检查后重建）。

**问题**：
1. 已存在账号用 `setdefault` 合并，配置里更新的 `password` 不会覆盖旧值 → 改了密码要重启整个 AstrBot 才生效。
2. 整表重建时复用旧对象，`_busy` 标志一起保留 → 只要账号曾经处于 `_busy`（健康检查换号泄漏、重载时正好有请求在跑、进程异常等），重载后 `acquire()` 永远跳过它。

**实测**：

```
password after reload: old          # 配置已是 new
busy after reload: True             # 重载没有重置
acquire returns: None               # 池子被“卡死”的账号占住
```

**影响**：单账号用户改密码后一直登录失败；池子容量随重载次数悄悄缩水，最终报“无可用账号”。

**方向**：合并时对 `password` / `email` / `mobile` 等配置字段做显式覆盖，对运行时字段（`token` / `hif_*` / `_busy` / `_last_released`）才保留；`load()` 时把 `_busy` 重置为 `False`（或只保留真正持有 guard 的忙碌状态）。

---

## 🟡 中严重度

### 4. 健康检查换号后身份混用 + 池位泄漏

**位置**：`core/account.py:326-409`（`account_health_check`），换号逻辑在 `core/chat.py:1085-1107`。

**问题**：健康检查用 `fake_req` 调 `create_session`。如果本次 create-session 失败，`create_session` 会释放旧账号、从池里 `choose_new_account` 拿一个新账号（`_busy=True`）并写回 `request.state.account` / `deepseek_token`。而 `account_health_check`：
- 之后用**原账号**的 `session_module.get_account_session(account)` 发 completion，却用 `fake_req` 里**新账号**的 token / HIF 头 → 身份混用；
- 全程没有 `release_account(fake_req.state.account)` → 新账号永久 `_busy`；
- 删除会话时也用原账号 token，删不掉新账号的会话。

**影响**：一次 create-session 抖动就烧掉一个池账号；与 #3 叠加后重载也不会恢复。

**方向**：健康检查前把账号从池里 acquire（或干脆不让我 `create_session` 换号）；无论是否换号，结束时释放 `fake_req.state.account`；会话删除统一用 `fake_req.state.account`。

---

### 5. `/undeepseek online force` 并不强制下载

**位置**：`main.py:1462` `result = await asyncio.to_thread(setup_mod.download_chromium, rev)`；`core/setup.py:385-396` 的 `force` 分支。

**问题**：`download_chromium(revision=None, force=False)` 只有在 `force=True` 时才会跳过“已安装”早退；而调用点没传 `force`。于是 `online force` 走到下载步骤时，函数直接返回 `{"ok": True, "message": "内核已安装，无需重复下载"}`，上层照旧提示“✅ 浏览器内核下载完成”。

**实测**：

```
download_chromium(rev) with kernel present: 内核已安装，无需重复下载
download_chromium(rev, force=True): 内核安装成功: ...
```

**影响**：用户以为强制重装了内核，实际什么都没做；与 README 第 97 行“强制下载插件内置内核”的说明不符。

**方向**：调用改成 `download_chromium(rev, force=force)`（`force` 由 `_cmd_ensure_kernel` 传入）。

---

### 6. 单个畸形 SSE chunk 会丢弃整条回复

**位置**：`main.py:128-149`（`_consume_sse_full` 新格式 A/B 分支）、`main.py:211-231`（`_consume_sse_chunks` 同分支）。

**问题**：新格式分支对 `chunk["v"]` 的结构做了假设，但异常处理是外层大 `try`：一旦某个 chunk 的 `v.response` 不是 dict、或 `response/fragments` 的 `v` 不是 list，`fr.get()` 就抛 `AttributeError`，直接终止整个流的解析。

**实测**：

```
case2 (malformed A: v.response 是字符串): text='' think='' ok=False closed=True
case3 (malformed B: v 是 dict 不是 list): text='' think='' ok=False
case5 (chunks): ok=False items=[('error', "'str' object has no attribute 'get'"), None]
```

**影响**：DeepSeek 只要下发一个结构变化的 chunk，整条已生成的回答全部作废（非流式直接报“SSE 流中断”，流式直接失败），而不是跳过该 chunk 继续解析。

**方向**：把新格式分支的解析放进独立的 `try/except`，结构不符时记 debug 并 `continue`；或对 `frags` / `v` 做 `isinstance(..., (list, dict))` 校验。

---

### 7. 浏览器模式跨会话上下文污染

**位置**：`core/browser.py:272-274`（`_account_sessions` 按账号缓存）、`640-679`（`chat()`）、`657-659`（`is_first` 只按 `session_id` 判断）。

**问题**：页面按**账号**复用，`session_id` 只用来决定“是否喂历史”。同一个账号被多个 AstrBot 会话（多用户/多会话）使用时，所有会话共享同一个网页对话：
- 会话 B 的第一条消息会把 B 的历史喂进已经装着会话 A 内容的页面；
- 之后 A 再发消息，页面里已经夹着 B 的上下文。

`_chat_lock` 只能防止两条消息同时操作页面，防不住这种跨会话的历史串味。`_active_sessions` 只增不减（仅 `close()` 时清空），长期运行会无界增长。

**影响**：多用户/多会话部署下回复串上下文，严重时泄露其他会话内容。

**方向**：按 `(account, session_id)` 维度缓存页面（带 LRU 上限），或在切换 `session_id` 时清空/重建页面；`_active_sessions` 增加过期清理。

---

### 8. Playwright driver 从不 `stop()`，多处泄漏 node 子进程

**位置**：`core/browser.py:717-742`（`close()` 只 `_browser.close()`，`_pw` 置 None 但没 `stop()`）、`423-459`（`_ensure_browser` 重连时旧 `_pw` 直接覆盖）、`682-695`（`replace_browser` 旧 `_pw` 不 stop）、`core/page_api.py:118-162`（`login_start` 失败时 `pw` 不 stop；重开窗口时旧 `pw` 不 stop）。

**问题**：`async_playwright().start()` 拉起的 driver（node.exe）在浏览器关闭后仍然存在；全仓库 grep 确认没有任何 `_pw.stop()` / `pw.stop()` 调用。`修复报告.md` 声称“释放 headless Chromium 与 Playwright 驱动进程”，实际只释放了 Chromium。

**影响**：每次插件重载/停用/验证窗口失败都会累积一个 node 子进程，长时间运行后进程数与内存上涨。

**方向**：`close()` 里 `await _pw.stop()`；`replace_browser` 对旧 `_pw` 调 `stop()`；`page_api` 的失败/替换路径同样处理。

---

### 9. 每次插件加载都全量健康检查，且失败账号被永久移出池子

**位置**：`main.py:1177-1180`（每次 `Main.__init__` 都起 `run_health_checks`）、`core/account.py:431-474`。

**问题**：
- 健康检查对每个账号做真实登录 + 创建会话 + PoW + completion，**每次重载都做一遍**，容易触发风控/限流；
- 检查完全通过后 `account_pool.load(passed)` 用“通过者”重建池子，失败账号被直接丢弃（直到下次重载才会按配置重建）；
- 与 #3 组合：一次网络抖动就可能让一个原本正常的账号在本次进程生命周期内彻底不可用。

**方向**：加时间窗/缓存（`account_probe.PROBE_CACHE_TTL` 已有类似常量但没用），只在首次加载或配置变更时检查；失败账号保留在池子里、用 `hif_fetch_fail_count` 做运行时降级，而不是直接移出。

---

### 10. 登录等待的实际时长与注释/报错不符

**位置**：`core/browser.py:385-420`。

**问题**：循环 `for step in range(20)`，每轮先调 `_is_logged_in()`，而它内部 `wait_for(..., timeout=5000)`；每轮再 `wait_for_timeout(1000)`，外加一次 `body.inner_text()`。最坏情况约 `20 × (5s + 1s + inner_text)` ≈ 120 秒以上，但注释写“最多等 20 秒”，超时报错也写“登录后 20 秒内未进入聊天页”。

**影响**：登录卡住时用户/日志都以为 20 秒就超时，实际要等两分钟，排障时间被误导。

**方向**：把总预算写成 deadline（比如 `time.time() + 20`）而不是固定轮数，报错文案与实际一致。

---

### 11. `requirements.txt` 缺依赖声明

**位置**：`requirements.txt`。

**问题**：`core/setup.py` 的 `_download_once` / `_probe_size` / `_download_multipart` 都 `import httpx`；`core/account.py` `from fastapi import HTTPException, Request`。两者都不在 `requirements.txt` 里。当前能跑是因为 AstrBot 自带 fastapi / httpx（Quart 依赖），但插件单独安装或环境精简时会 `ImportError`。

**方向**：补上 `httpx`、`fastapi`（或把 `account.py` 里的 `HTTPException` 换成自定义异常，减少对 fastapi 的依赖）。

---

## ⚪ 低严重度 / 代码卫生

12. **死代码 / 未使用**（grep 确认无调用点）：
    - `core/browser.py:309` `PRE_EXISTING_HINTS`；
    - `core/chat.py:117` `ToolCallStreamDetector` 类；`core/chat.py:836` `strip_partial_tool_call_text`；`core/chat.py:648` `tool_call_to_anthropic_block`；
    - `core/account.py:259` `determine_mode_and_token`；
    - `core/tokens.py:61` `count_tokens_for_messages`；`core/pool.py:100` `available_count`；`core/pending_login.py:176` `clear_all`；
    - `core/account_probe.py:23` `PROBE_CACHE_TTL`；
    - `core/constants.py:6-8` `DEEPSEEK_FILE_UPLOAD_URL` / `DEEPSEEK_FILE_FETCH_URL` / `DEEPSEEK_STOP_STREAM_URL`，`35-38` `KEEP_ALIVE_TIMEOUT` / `PROMPT_UPLOAD_THRESHOLD` / `PROMPT_UPLOAD_POLL_RETRIES` / `PROMPT_UPLOAD_POLL_INTERVAL`；
    - `core/chat.py:913-919` `func_tag_idx` / `xml_tag_idx` 计算后未使用（同时是让 #2 失效的直接原因）；
    - `core/browser.py:281` 与 `610` 重复定义 `_active_sessions`（后者覆盖前者，纯属冗余）。

13. **`manual_login.py`**：第 53-55 行打印“检测到已有登录态，将尝试复用...”，但 `new_context()` 根本没传 `storage_state`，所谓“复用”不存在；`PLUGIN_DATA` 与 `chromium-1234` 硬编码，换机器/换版本即失效。

14. **配置页模型列表过期**：`pages/deepseek_reverse/index.html:96-100` 展示 `deepseek-v4-thinking`、`deepseek-r1`，而 `main.py:48-57` 的 `SUPPORTED_MODELS` 是 `deepseek-v4-flash/-search`、`deepseek-chat/-search`、`deepseek-reasoner/-search`、`deepseek-v4-pro/-search`。页面会误导用户去选不存在的模型。

15. **关键请求缺 timeout / 结构未校验**：`core/chat.py:1051`（create session POST）、`core/account.py:79`（登录 POST）都没传 timeout；`core/chat.py:1070-1075` 对 200 响应直接 `data["data"]["biz_data"]["id"]`，DeepSeek 返回结构变化时会抛 `KeyError/TypeError` 而不是走重试。

16. **浏览器模式细节**：`main.py:792` 调 `_diagnose_browser_kernel()` 但丢弃返回值（只在异常分支才用）；`main.py:825` 浏览器模式 `TokenUsage(input_other=0, ...)`，输入 token 恒为 0，用量统计不准。

17. **流式取消后的资源**：`main.py:993-1062` 的 worker 线程在客户端断连后仍继续跑，`q` 无界增长；账号也会一直被占用到完成。

18. **`replace_browser` 注释与实现不符**：`core/browser.py:691-694` 注释说“旧 headless 浏览器已无账号窗口在用”，但 `_account_sessions` 里可能仍有属于旧浏览器的 context；这些 context 既不 close 也不迁移，只能等 `close()` 统一回收。

19. **`login_check` 不校验账号一致性**：`core/page_api.py:164-211` 只按请求里的 `account` 取 session，不校验当前登录的是不是该账号；理论上可以把他人的验证窗口升格为指定账号的登录态。

20. **公开清单写进插件包**：`core/pending_login.py:50-58` 把 `pending_login.json` 写进 `pages/deepseek_reverse/`，插件更新会覆盖；某些部署下插件目录只读，写入会失败（虽有 try/except）。

21. **陈旧字节码**：`core/config.pyc`、`core/models.pyc` 是 Python 3.12 之前风格的裸 `.pyc`，与同名 `.py` 并存，容易在排查时误判加载的是哪份代码。

22. **`account_probe` 参数形同虚设**：`probe_all_accounts(accounts, browser_exe="")` 的 `browser_exe` 只用于“有没有内核”的判断，实际浏览器永远由 `_ensure_browser()` 自己探测，传了也不生效。

---

## 已核对、确认不是问题的项

- `core/browser.py:219` 与 `445` 的 UNC 去前缀正则 `^\\\\\?\\\\?([A-Za-z]:)`：实测能把 `\\?\E:\bot\...` 正确还原成 `E:\bot\...`，`浏览器模式bug修复报告.md` 的修复有效。
- `storage_state_*.json` 只在登录后写出、从不在 `new_context()` 加载：与 `README.md:332`“实测 storage_state 无法让浏览器重启后存活”一致，属于已说明的设计限制，不算 bug（但 `manual_login.py` 的提示语与之矛盾，见 #13）。
- `修复报告.md` 中 #1-#19 的旧问题在当前 1.7.1 代码里均已落实（工具调用门控、`_chat_lock`、SSE 白名单、流式截断标记、短语只匹配开头、`split_browser_messages`、401/403 早退、chunked 兜底等）。
- 账号池的 `_guard` 机制会清空 `_guard`，`create_session` / `get_pow_response` 在换号失败时不会造成重复释放（这一点比旧报告 #19 描述的更稳）。
- 全量 `py_compile`（Python 3.10）通过；`git status` 干净，`storage_state*.json` 已被 `.gitignore` 忽略、未入库。

---

## 建议处理顺序

1. **#1 / #2**（工具调用误判与漏判）——改动小、影响直接，优先。
2. **#3 / #4**（账号池生命周期）——会持续吃掉池子容量，叠加后很难自查。
3. **#5 / #6**（force 命令、SSE 健壮性）——单点修复即可。
4. **#7 / #8 / #9**（浏览器模式资源与隔离）——多用户/长期运行场景。
5. 其余 ⚪ 项随版本迭代清理。
