# bug检测报告 修复说明（1.8.0）

- **依据**：《bug检测报告.md》（1.7.1 全量审查，22 项）
- **修复日期**：2026-11-07
- **版本**：1.7.1 → **1.8.0**

---

## 🔴 高严重度

### #1 普通 JSON 数组导致 `UnboundLocalError`（实测复现，属实）

`core/chat.py` 的列表分支只在"全部是 function-call 形态"时给 `valid_calls` 赋值，其余情况既不赋值也不 `continue`，随后 `if valid_calls:` 直接抛异常；异常被 `main._parse_tool_calls_from_text` 静默吞掉，导致同一回复里后续真实工具调用一并失效。

**修复**：循环开头初始化 `valid_calls = []`；列表分支不满足条件时显式 `continue`。

**验证**：
```
普通数组 [{"name":"张三"}] -> tool_calls=False（不再抛异常）
空数组 []                -> False
数组 + 真实 toolcall     -> True（后续真实调用正常解析）
```

### #2 代码围栏检测是死代码（属实）

`func_tag_idx` / `xml_tag_idx` 计算后从未使用，``` 里的工具调用示例仍被当真执行。

**修复**：重写为计算所有围栏区间 `_fenced_spans()`，解析 `function_calls` / `xml` / JSON 时按 span 起始位置过滤（`_filter_spans()` / `_in_fence()`），围栏内一律跳过。

**验证**：围栏内 xml / json 示例 → 不执行；围栏外 → 正常执行。

### #3 账号池 `setdefault` 合并导致改密码不生效 + `_busy` 卡死（属实）

**修复**：`pool.load()` 对配置字段（`password`/`email`/`mobile`）显式覆盖；对运行时字段（`_busy`/`_guard`/`_last_released`）复位，保留 `token`/`hif_*` 等缓存。

**验证**：改密码后立即生效；`_busy=True` 重载后恢复可 acquire。

---

## 🟡 中严重度

| # | 修复 |
|---|---|
| 4 | `create_session` 新增 `allow_switch` 参数；健康检查传 `False`（禁止换号），全程统一用 `fake_req.state.account` 取 session 与删除会话，`finally` 里释放换来的账号；换号失败不再白耗重试 |
| 5 | `/undeepseek online force` 调用改为 `download_chromium(rev, force)`，`force` 由 `_cmd_ensure_kernel` 传入；内核已存在时不再假报成功 |
| 6 | 抽出 `_iter_fragments()` 安全生成器；两个 SSE 消费函数的新格式分支加独立 `try/except` 与类型校验；单个畸形 chunk 只跳过自己，不丢弃已收正文 |
| 7 | 浏览器模式改为**账号常驻 context + `(账号, 会话ID)` 独立标签页**，带 32 个 LRU 上限；`invalidate_account` / `close()` / `replace_browser` 同步清理会话页 |
| 8 | `close()` 里 `await _pw.stop()`；`replace_browser` 改为 async 并停掉旧 `_pw`、关掉旧浏览器与旧会话页；`page_api.login_start` 失败路径与重开窗口路径、`close_all_manual` 全部补上 driver 停止 |
| 9 | `run_health_checks` 加 10 分钟缓存、**不再重建池子**（失败账号保留，靠 `hif_fetch_fail_count` 运行时降级）；检查线程改 daemon |
| 10 | 登录轮询改为真实 `deadline = time.time() + 20`；`_is_logged_in` 单次等待由 5s 降到 1s，总预算与注释/报错一致 |
| 11 | `requirements.txt` 补 `httpx`、`fastapi` |

**#6 实测**：正常 chunk + 畸形 A（`v.response` 为字符串）+ 畸形 B（`v` 为 dict）+ 正常 chunk + `[DONE]` → 非流式 `text='你好世界' ok=True`，流式 `finished=True` 且两个正文 chunk 都在。

---

## ⚪ 低严重度

| # | 修复 |
|---|---|
| 12 | 删除 `PRE_EXISTING_HINTS`、重复定义的 `_active_sessions`；`_retired_browsers` 随 #8 重构一并删除；`PROBE_CACHE_TTL` 死常量删除 |
| 13 | `manual_login.py` 去掉"将尝试复用登录态"的假承诺，改为明确提示会话级 cookie 通常已失效 |
| 14 | 配置页模型列表同步为 `SUPPORTED_MODELS` 的 8 个模型 |
| 15 | 登录 POST 与 `create_session` POST 补 `timeout=30`；`create_session` 对 200 响应做结构校验，异常结构走重试而非抛 `KeyError/TypeError` |
| 16 | `_browser_chat` 开头内核检测缺失时直接抛明确指引（不再空跑 6 次换号）；浏览器模式 `input_other` 改用 `count_tokens(history_prompt + latest_prompt)` |
| 17 | 流式队列改为有界（256）；`_consume_sse_chunks` 队列满时丢弃普通 chunk 但保留结束/错误标记；生成器 `finally` 里置 `cancelled["flag"]` 通知 worker 停止 |
| 18 | `replace_browser` 现在会 close 属于旧浏览器的所有 context 与会话页，注释与实现一致 |
| 19 | `login_check` 校验请求账号确实在插件配置里，且页面 URL 不能仍是 `/sign_in` |
| 20 | 删除 `pages/deepseek_reverse/pending_login.json` 及写入逻辑；清单完全由后端 `GET /login/pending` 提供，不再写插件包目录 |
| 21 | 删除 `core/*.pyc` 三个陈旧裸字节码 |
| 22 | `account_probe.browser_exe` 真正生效（传给 `_ensure_browser(exe_override)`）；删除 `PROBE_CACHE_TTL` 死常量 |

---

## 验证记录

- 全量 `py_compile`：`main.py` + `core/*.py` + `manual_login.py` 通过
- 配置页 JS：`node --check` 通过
- 综合回归（AstrBot 自带 Python 3.12.12 + 真实 `astrbot` 4.27.2 运行时）：
  - #1 / #2 / #3 / #5 / #6 共 9 项功能测试全过
  - #4 / #7 / #8 / #9 / #10 / #15 / #16 共 12 项结构检查全过
- **未做真实浏览器端到端测试**：避免反复自动化登录继续触发 DeepSeek 风控

## 已知限制（未改，属设计约束）

- `ds_session_id` 是会话级 cookie，Chromium 不落盘；**机器重启 / AstrBot 重启 / 重载插件后仍需重新登录一次**。ck 快照仅用于排查与导出。
- #7 的会话隔离用「同 context 多标签页」实现，标签页上限 32；超限会淘汰最早会话的标签页，该会话下次会重新喂历史。
