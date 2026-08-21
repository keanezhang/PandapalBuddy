# ResponsesAPIClient 功能改动 pytest 测试设计（风险驱动）

> 用途：本次 `pandaren/llm/responses_client.py` 功能性改动（instructions 变化检测 + no_increment
> 冷启动 + 流式事件分发重构 + failed→error 映射 + system 消息保留）的测试设计。
> 本文档为 **test-coder 的输入**：每条用例含 Given/When/Then、等价类代表值、Mock/Fake 决策、
> Oracle、覆盖准则，可直接落成 pytest 代码，无需二次分析。
>
> 设计依据：源码白盒分析（`responses_client.py`，行号见 §1）；类型契约 `pandaren/llm/types.py`
> （LLMStreamChunk 6 字段 / ToolCallDelta 4 字段）；项目测试约定 `pyproject.toml`
> （pytest + pytest-asyncio，`asyncio_mode = "auto"`，`testpaths` 含 `pandaren`）。
>
> 隔离策略（用户指定）：LLM 网络调用用 **httpx.MockTransport** 替换 `client._http_client` 的
> transport；纯函数层零 mock。无真实网络、无外部依赖、CI 可独立运行。

---

## 0. 设计元信息

| 项 | 值 |
|----|----|
| 测试框架 | pytest + pytest-asyncio（`asyncio_mode=auto`，async 用例无需装饰器） |
| 测试层级 | unit（纯函数/事件分发 13 条）+ component(fake)（call/stream 13 条，MockTransport 假网络）；**无 integration / e2e** |
| 用例总数 | 26（T1-T13 unit，C1-C9 与 S1-S5 component(fake)） |
| Oracle 策略 | hash 类 → 参考实现（hashlib 标准库，非被测实现）；流式 chunk 序列 / 请求体 / 状态 → golden value（规格白纸黑字可独立推导）；确定性 → property 标注 |
| 确定性控制 | 本被测对象无时间/随机/浮点/集合顺序不确定源；SSE 事件顺序由测试显式构造的文本决定；MockTransport 同步返回无真实网络时序 |

**层级声明（为什么没有 integration/e2e）**：本次改动全部在 client 内部逻辑（状态机 + 事件分发 +
请求体构建），网络层用 MockTransport 假传输即可完整覆盖。真实 provider 行为（各家的 SSE 字段差异、
过期错误文案）属集成验证，不在功能改动测试范围内——见 §8 推断清单。

**本次改动与既有行为边界**：`tools_changed` / `messages_shortened` / `_detect_increment` /
`_build_full_request` / `_build_incremental_request` / SSE 解析 / 错误分类均为既有逻辑，本次设计
只在它们与新增逻辑**交互**的用例中涉及（如 C3/C4/C5 验证新增检测与既有检测的并列关系），不单独展开。

---

## 1. 被测对象全景与本次改动对照（白盒，行号引用 responses_client.py）

| 改动 | 实现位置 | 关键语义 |
|------|---------|---------|
| A. `_compute_instructions_hash(messages)` | L967-992 | 遍历全部消息，收集 `role=="system"` 且 `content` 为 **str** 的 content，`"\n".join` 后 `sha256(...).hexdigest()[:16]`；无 system → `None`；非 dict 元素跳过 |
| A. 实例字段 `_instructions_hash` | L195 | 构造器初始 `None` |
| B. `instructions_changed` 检测 | call L351-355 / stream L425-427 | `_instructions_hash is not None` 且 `new != old` → `_invalidate("instructions_changed")`。注意：旧值为 None（从未成功建过 hash）时**不检测**；new 为 None 但旧值非 None（system 被删）→ 触发 |
| B. `no_increment` 检测 | call L360-362 / stream L432-434 | `elif`（排在 messages_shortened 之后）：`_previous_response_id is not None` 且 `len(messages) == _last_messages_len` → `_invalidate("no_increment")` |
| B. `invalidate` | L912-923 | `previous_response_id=None`、`last_messages_len=0`；**保留** `_tools_hash` / `_instructions_hash`（下次计算覆盖） |
| C. 成功后更新 instructions_hash | `_update_state_after_success` L948 | `self._instructions_hash = _compute_instructions_hash(messages)` |
| D. `_dispatch_stream_event` | L1304-1412 | 见 §1.1 事件→chunk 映射表；`stream_state` 四键（L469-474）主/降级循环共享，fallback 时被新响应覆盖不显式重置 |
| D. 兜底 flush | L547-553 / L616-623 | 需**同时**满足 `pending_usage is not None and not usage_flushed and not usage_yielded`，flush 后置 `usage_yielded=True` |
| D. 流结束状态更新 | L632-644 | `if stream_state["response_id"]:` 才更新 `previous_response_id / last_messages_len / tools_hash / instructions_hash` |
| E. `_status_to_finish_reason` | L1213-1227 | `failed→"error"`（completed→stop, incomplete→length, cancelled→stop, 未知→stop 不变） |
| F. `_convert_messages_to_input` system 分支 | L805-809 | `role=="system"` → 原样保留 `{"role":"system","content":...}`（content 缺失默认 `""`） |

### 1.1 `_dispatch_stream_event` 事件→chunk 映射表（Oracle 基准，T8-T13 逐分支覆盖）

| 事件 type | state 副作用 | 产出 chunk（全部非空条件） |
|-----------|-------------|---------------------------|
| `response.created` | `response_id = resp.id` | 无 |
| `response.completed` | `response_id`（若有）、`pending_usage`、`usage_flushed=True` | `LLMStreamChunk(finish_reason=_status_to_finish_reason(status), usage=pending_usage)` |
| `response.output_text.delta` | 无 | `delta` 非空 → `LLMStreamChunk(delta_content=delta)` |
| `response.reasoning_text.delta` | 无 | `delta` 非空 → `LLMStreamChunk(delta_reasoning_content=delta)` |
| `response.refusal.delta` | 无 | `delta` 非空 → `LLMStreamChunk(refusal_delta=delta)` |
| `response.function_call_arguments.delta` | 无 | `delta` 非空 → `tool_call_delta(index=output_index, id=item_id, name="", arguments_delta=delta)` |
| `response.function_call_arguments.done` | 无 | 无（pass） |
| `response.output_item.added` | 无 | item.type==`function_call` 且 name 非空 → `tool_call_delta(index=output_index, id=call_id or id, name=name, arguments_delta="")` |
| `output_text.done` / `output_item.done` / `content_part.done` | 无 | 无（pass） |

---

## 2. 不变式清单

| 编号 | 不变式 | 验证位置 |
|------|--------|---------|
| inv-A | `_compute_instructions_hash` 确定性：同输入 → 同输出 | T2 [property] |
| inv-B | 无 system 消息 → hash 为 `None`；hash 只覆盖 **str** content 的 system 消息 | T1 / T4 |
| inv-C | hash 拼接为 `"\n".join` 且**顺序敏感**（多条 system 顺序交换 → 不同 hash） | T3 |
| inv-D | `previous_response_id is None` ⟺ 必发全量请求（body 无 `previous_response_id`、input 为完整转换） | C1/C3/C4/C5/C6 |
| inv-E | 正常增量（消息增长 + tools/instructions 未变）→ body 含 `previous_response_id` + 仅增量 input | C2 |
| inv-F | `invalidate` 后 `previous_response_id=None`、`last_messages_len=0`，`_tools_hash`/`_instructions_hash` **保留**（供下次计算覆盖，不留作陈旧比较） | C8 |
| inv-G | 单个流中 usage chunk **至多产出 1 次**（completed 已 flush 后兜底不再 yield） | S2/S3 |
| inv-H | 流结束：`response_id` 非 None 才更新四状态；否则全部保持 | S1/S5 |
| inv-I | 所有 `role=="system"` 消息在 input 转换中原样保留（`{role:"system", content}`） | T5/T6 |
| inv-J | 成功（非流式）后 `_instructions_hash` 更新为本次 messages 的 hash（下次调用可检测变化） | C7 |

---

## 3. 风险清单（按 P0→P3 排序，S×L 定级）

| 编号 | 风险 | S×L | 优先级 | 关联用例 |
|------|------|-----|--------|----------|
| Risk-1 | instructions 改写（system 压缩/改写）未触发冷启动 → 增量续接读到不一致上下文（上下文污染） | 高×高 | **P0** | C3/C4/S1 |
| Risk-2 | no_increment 漏判：重发相同消息仍走增量 → 空增量请求或重复输出 | 高×中 | **P0** | C5 |
| Risk-3 | system 消息仍被静默丢弃 → 增量续接上下文不一致（本次改动 F 的修复目标回归） | 高×高 | **P0** | T5/T6 |
| Risk-4 | 冷启动后仍残留 previous_response_id → 请求带过期 id 被拒 / 上下文污染 | 高×高 | **P0** | C1/C3/C4/C5/C8 |
| Risk-5 | failed status 误映射为 stop → 上游把「请求本身失败」当「模型正常停止」（语义失真） | 高×中 | **P0** | T7 |
| Risk-6 | usage 重复 flush → 上层重复计费 / 重复展示 | 中×中 | **P1** | S2 |
| Risk-7 | fallback 降级后 `_instructions_hash` 未更新 → 后续 system 变化漏检 | 高×中 | **P0** | C9/S4 |
| Risk-8 | 流式无 response_id（异常流）时仍用旧 id 续接 → 上下文错乱 | 中×低 | **P1** | S5 |
| Risk-9 | 事件→chunk 映射重构回归（delta 空值产出、tool_call name/id 字段错位、done 事件误产出） | 中×高 | **P1** | T8-T13/S1 |
| Risk-10 | 首次调用（hash/len 初始态 None/0）误触发新增检测 → 不必要的冷启动（行为正确但路径退化） | 低×中 | **P2** | C6 |
| Risk-11 | 非 str content 的 system（list 结构）不参与 hash 检测 → 该类 system 改写不被发现 | 低×中 | **P2** | T4（锁定现状，设计声明） |
| Risk-12 | 多条 system 顺序交换触发冷启动（hash 顺序敏感）——保守冷启动，可接受 | 低×低 | **P3** | T3（锁定现状） |
| 非功能风险（只标注不展开） | 流式响应延迟、并发流、日志注入安全 | — | — | 需专项流程，功能设计不假装覆盖 |

---

## 4. 测试双与确定性控制（Phase 2 决策）

| 依赖 | 决策 | 理由 |
|------|------|------|
| 纯函数 / 事件分发（T1-T13） | **零 mock** | 无 I/O、无协作对象（`_dispatch_stream_event` 仅调用同对象 `_build_usage_info`/`_status_to_finish_reason`） |
| `_http_client` 网络层（C1-C9/S1-S5） | **httpx.MockTransport**（用户指定）：`client._http_client = httpx.AsyncClient(transport=MockTransport(handler))` | 假传输替代真实网络，同步可控返回；无真实 I/O → component(fake) 层级 |
| `_send_request` / `_http_client.stream` | 不单独 monkeypatch | MockTransport 已覆盖；需要按请求体分流响应时，在 handler 内读 `request.content`（JSON）分支 |
| 时间 / 随机 / 浮点 / 集合顺序 | 无（本对象不涉及） | hash 字节序确定；SSE 事件顺序由测试构造文本显式控制 |

**MockTransport handler 分流约定**（C9/S4 用）：读取 `request.content` 解析 JSON，含
`previous_response_id` 键 → 返回 400 expired；否则 → 返回成功响应。其余用例 handler 直接返回
预置响应。所有测试结束 `await client.aclose()`。

---

## 5. 覆盖矩阵（用例 × 焦点/风险）

焦点编号：F1=冷启动必全量（inv-D） F2=正常增量（inv-E） F3=流式事件映射（inv-G/inv-H + Risk-9）
F4=fallback 降级（Risk-7） F5=usage 不重复 flush（inv-G/Risk-6） F6=system 保留 + 无 system hash None
（inv-B/inv-I/Risk-3） F7=failed→error（Risk-5）

| 用例 | F1 | F2 | F3 | F4 | F5 | F6 | F7 | 补充风险/不变式 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|----------------|
| T1 无 system→None | | | | | | ✅ | | inv-B |
| T2 单条 system 确定性 | | | | | | ✅ | | inv-A [property] |
| T3 多条拼接+顺序敏感 | | | | | | ✅ | | inv-C / Risk-12 |
| T4 仅 str 参与 | | | | | | ✅ | | Risk-11 |
| T5 system 原样保留 | | | | | | ✅ | | inv-I / Risk-3 |
| T6 混合消息转换 | | | | | | ✅ | | inv-I / Risk-3 |
| T7 failed→error | | | | | | | ✅ | Risk-5 |
| T8 created 收集 id | | | ✅ | | | | | inv-H |
| T9 completed 终止 chunk | | | ✅ | | ✅ | | | inv-G / Risk-6 |
| T10 delta 文本类 | | | ✅ | | | | | Risk-9 |
| T11 arguments.delta | | | ✅ | | | | | Risk-9 |
| T12 output_item.added | | | ✅ | | | | | Risk-9 |
| T13 done 系列 pass | | | ✅ | | | | | Risk-9 |
| C1 首次调用全量 | ✅ | | | | | | | inv-D / Risk-4 |
| C2 正常增量 | | ✅ | | | | | | inv-E |
| C3 instructions 改写 | ✅ | | | | | | | Risk-1 / Risk-4 |
| C4 system 删除 | ✅ | | | | | ✅ | | Risk-1 / Risk-4 |
| C5 no_increment | ✅ | | | | | | | Risk-2 / Risk-4 |
| C6 首次不误触发 | ✅ | | | | | ✅ | | Risk-10 / inv-B |
| C7 成功后四状态更新 | | | | | | ✅ | | inv-J |
| C8 invalidate 保留 hash | | | | | | ✅ | | inv-F / Risk-4 |
| C9 call fallback | ✅ | | | ✅ | | ✅ | | Risk-7 / Risk-4 |
| S1 流式完整序列 | ✅ | | ✅ | | ✅ | ✅ | | inv-H / Risk-1 / Risk-9 |
| S2 usage 不重复 flush | | | | | ✅ | | | inv-G / Risk-6 |
| S3 无 completed 无 usage | | | | | ✅ | | | inv-G |
| S4 流式 fallback | ✅ | | ✅ | ✅ | | ✅ | | Risk-7 / Risk-9 |
| S5 流无 response_id | | | | | | | | inv-H / Risk-8 |

覆盖准则汇总：`_dispatch_stream_event` 的 if/elif 链 → T8-T13 全分支；call/stream 检测链
（tools_changed / instructions_changed / messages_shortened / no_increment / 路径选择）→
C1-C6 全分支；降级分支（`_is_response_id_expired_error` True/False 两条路径）→ C9/S4 + C2（False 路径）。

---

## 6. 用例详情

### Group 1 — 纯函数（unit，零 mock）：`_compute_instructions_hash`

#### T1：无 system 消息 → 返回 None

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-B 无 system → None [P0]（Risk-3 反向：不参与检测） |
| 测试层级 | unit |
| 覆盖准则 | branch：`system_parts` 为空 → 返回 None 分支 |
| Oracle | golden value（`None` 白纸黑字） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：消息列表无 system 元素 → 代表值 = `[{"role":"user","content":"你好"},{"role":"assistant","content":"hi"}]`

**Given**：无前置。

**When**：`ResponsesAPIClient._compute_instructions_hash(messages)`（直接调用静态方法）。

**Then**：
- 返回值 = `None`
- 副作用：无副作用，仅验证返回值

#### T2：单条 str system → 确定性 hash，与参考实现一致

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-A 确定性 [property] + Risk-1 变化检测的数据基础 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：str content 收集分支 |
| Oracle | 参考实现：`hashlib.sha256("你是助手".encode("utf-8")).hexdigest()[:16]`（标准库独立计算，非被测实现）+ 确定性 property |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：单条 system、content 为 str → 代表值 = `[{"role":"system","content":"你是助手"}]`

**Given**：无前置。

**When**：`h1 = ResponsesAPIClient._compute_instructions_hash(messages)`，再调一次 `h2`。

**Then**：
- `h1` == `hashlib.sha256("你是助手".encode("utf-8")).hexdigest()[:16]`（参考实现一致）
- 格式：`len(h1) == 16` 且全为 `[0-9a-f]` 十六进制字符
- 确定性（[property]）：`h1 == h2`
- 副作用：无副作用

#### T3：多条 system → `"\n".join` 拼接且顺序敏感

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-C 拼接规则 + Risk-12 顺序敏感（保守冷启动）[P3] |
| 测试层级 | unit |
| 覆盖准则 | branch：多元素收集 + join 分支 |
| Oracle | 参考实现：`hashlib.sha256("你是助手\n第二条".encode("utf-8")).hexdigest()[:16]` |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：多条 system 消息（str content）→ 代表值 = 顺序 A `[sys("你是助手"), sys("第二条")]` 与顺序 B `[sys("第二条"), sys("你是助手")]`

**Given**：无前置。

**When**：分别对顺序 A 与顺序 B 调用。

**Then**：
- 顺序 A 的返回值 == 参考实现 `sha256("你是助手\n第二条")[:16]`（证明用 `\n` 连接而非拼接其他分隔符）
- 顺序 A ≠ 顺序 B 的返回值（证明顺序敏感——两条 system 交换顺序会冷启动，属保守行为锁定）
- 副作用：无副作用

#### T4：仅 str content 参与（list/None content 跳过、非 dict 元素跳过）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-B hash 范围 + Risk-11 非 str system 不参与检测（锁定现状）[P2] |
| 测试层级 | unit |
| 覆盖准则 | branch：`isinstance(content, str)` 跳过分支 + `isinstance(msg, dict)` 跳过分支 |
| Oracle | golden value：`None`；参考实现（单条 str 参与时） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：system 的 content 形态 → 代表值 =
- `[{"role":"system","content":[{"type":"text","text":"结构化"}]}]`（list content）
- `[{"role":"system","content":None}]`（None content）
- `[{"role":"system","content":"有效"}, "not-a-dict"]`（混入非 dict 元素）

**Given**：无前置。

**When**：依次调用三种输入。

**Then**：
- list content / None content 的输入 → 返回 `None`（该 system 被 hash 跳过）
- 混合输入 → 返回值 == 参考实现 `sha256("有效")[:16]`（非 dict 元素被跳过，仅 str content 参与）
- 副作用：无副作用

### Group 1b — 纯函数（unit，零 mock）：`_convert_messages_to_input` / `_status_to_finish_reason`

#### T5：system 消息原样保留为 `{role:"system", content}`

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-I + Risk-3 system 静默丢弃（改动 F 修复目标回归）[P0] |
| 测试层级 | unit |
| 覆盖准则 | branch：`role == "system"` 新分支 |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：system 消息的 content 形态 → 代表值 =
- `{"role":"system","content":"系统指令"}`（有 content）
- `{"role":"system"}`（无 content 键 → 默认 `""`）

**Given**：无前置。

**When**：`ResponsesAPIClient._convert_messages_to_input([system_msg])`。

**Then**：
- 有 content：返回 `[{"role":"system","content":"系统指令"}]`（原样保留，无任何改写）
- 无 content：返回 `[{"role":"system","content":""}]`（默认空串，不丢消息）
- 副作用：无副作用

#### T6：混合消息序列中 system 穿插保留，其余转换不变

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-I + Risk-3（增量续接上下文一致性的根基）[P0] |
| 测试层级 | unit |
| 覆盖准则 | branch：system/user/assistant/tool 四分支全走到 |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：混合角色序列（system 不在首位）→ 代表值 =
`[{"role":"system","content":"内嵌指令"},{"role":"user","content":"u1"},{"role":"assistant","content":"a1","tool_calls":[{"id":"call_1","function":{"name":"get_weather","arguments":"{}"}}]},{"role":"tool","tool_call_id":"call_1","content":"晴"},{"role":"system","content":"追加指令"}]`

**Given**：无前置。

**When**：调用 `_convert_messages_to_input(以上消息)`。

**Then**：
- 返回值按序为 6 项：
  1. `{"role":"system","content":"内嵌指令"}`
  2. `{"role":"user","content":"u1"}`
  3. `{"role":"assistant","content":"a1"}`
  4. `{"type":"function_call","id":"call_1","call_id":"call_1","name":"get_weather","arguments":"{}"}`
  5. `{"type":"function_call_output","call_id":"call_1","output":"晴"}`
  6. `{"role":"system","content":"追加指令"}`
- 首尾两条 system 均原样保留（穿插位置不影响保留逻辑）
- 副作用：无副作用

#### T7：failed status → finish_reason "error"（全状态映射）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-5 failed 误映射为 stop [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch：映射表全 5 个键（含未知→默认 stop） |
| Oracle | golden value（映射表白纸黑字） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：status 值域 → 代表值 = 参数化 5 组

**Given**：无前置。

**When**：`_status_to_finish_reason(status)` 参数化调用。

**Then**：
- `"completed"` → `"stop"`
- `"incomplete"` → `"length"`
- `"cancelled"` → `"stop"`
- `"failed"` → `"error"`（**本次改动核心**，上游可区分失败与正常停止）
- `"unknown_xyz"` → `"stop"`（未知值兜底不变）
- 副作用：无副作用

### Group 2 — 流式事件分发（unit，零 mock）：`_dispatch_stream_event`

> 前置：构造 client（`ResponsesAPIClient(api_key="sk-test", model_name="m", base_url="https://api.openai.com/v1", capabilities=OPENAI_RESPONSES)`），
> 直接调用 `client._dispatch_stream_event(parsed, stream_state)`；`stream_state` 初始
> `{"pending_usage": None, "usage_flushed": False, "response_id": None, "usage_yielded": False}`。

#### T8：response.created 收集 response_id，不产出 chunk

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-H 流状态（response_id 来源）+ Risk-9 映射回归 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：`response.created` 分支 |
| Oracle | golden value |
| Mock | 否 — 纯逻辑零 mock |

**Given**：`stream_state` 初始态。

**When**：`_dispatch_stream_event({"type":"response.created","response":{"id":"resp_s1"}}, stream_state)`。

**Then**：
- 返回 `[]`（created 不产出 chunk）
- state 副作用：`stream_state["response_id"] == "resp_s1"`
- 其余 state 键不变（`pending_usage is None`、`usage_flushed is False`）

#### T9：response.completed → 终止 chunk（finish_reason + usage）并置 usage_flushed

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-G usage 单次 + Risk-6/Risk-9 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：`response.completed` 分支（含 status 非 completed 子路径） |
| Oracle | golden value |
| Mock | 否 — 纯逻辑零 mock |

**等价类划分**：completed 的 status 值 → 代表值 = `"completed"` 与 `"failed"` 两例

**Given**：`stream_state["response_id"]="resp_s1"`。

**When**：`_dispatch_stream_event({"type":"response.completed","response":{"id":"resp_s1","status":"completed","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}}, stream_state)`。

**Then**：
- 返回 1 个 chunk：`finish_reason == "stop"`、`usage == {"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}`（归一后）
- state 副作用：`usage_flushed is True`、`pending_usage` 为上述 usage
- 变体（status=`"failed"`）→ `finish_reason == "error"`（与 T7 联动）

#### T10：delta 文本类事件（output_text / reasoning_text / refusal）——非空产出、空不产出

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-9 映射回归（空 delta 误产出/字段错位）[P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：三个 delta 分支 + 各自空 delta 子路径 |
| Oracle | golden value |
| Mock | 否 — 纯逻辑零 mock |

**等价类划分**：delta 值域 → 代表值 = 非空 `"今天"` / 空 `""`

**Given**：`stream_state` 初始态。

**When**：依次分发三类事件（各含非空与空两个变体）。

**Then**：
- `{"type":"response.output_text.delta","delta":"今天"}` → 1 chunk，`delta_content == "今天"`
- `{"type":"response.output_text.delta","delta":""}` → `[]`（空 delta 不产出）
- `{"type":"response.reasoning_text.delta","delta":"推理"}` → 1 chunk，`delta_reasoning_content == "推理"`
- `{"type":"response.refusal.delta","delta":"拒绝"}` → 1 chunk，`refusal_delta == "拒绝"`
- 空变体均 → `[]`
- 所有情况 state 无变化（response_id/pending_usage/usage_flushed 均保持）

#### T11：function_call_arguments.delta → tool_call_delta（index/id/name=""/arguments_delta）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-9 字段错位（name 必须为空串，name 由 output_item.added 提供）[P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：arguments.delta 分支 + 空 delta 子路径 |
| Oracle | golden value |
| Mock | 否 — 纯逻辑零 mock |

**Given**：`stream_state` 初始态。

**When**：`_dispatch_stream_event({"type":"response.function_call_arguments.delta","item_id":"fc_1","output_index":0,"delta":"{\"city\":"}, stream_state)`。

**Then**：
- 返回 1 chunk，`tool_call_delta == {"index":0,"id":"fc_1","name":"","arguments_delta":"{\"city\":"}`（name 恒为空串）
- 空 delta 变体 → `[]`；state 无变化

#### T12：output_item.added 的 function_call → 带 name 的 delta；非 function_call / 空 name → pass

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-9 name/id 字段语义（id 取 call_id 优先；name 非空才产出）[P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：item.type==function_call 且 name 非空 / name 空 / 非 function_call 三分支 |
| Oracle | golden value |
| Mock | 否 — 纯逻辑零 mock |

**等价类划分**：item 形态 → 代表值 = 三个变体

**Given**：`stream_state` 初始态。

**When**：依次分发三个变体。

**Then**：
- `{"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"get_weather"}}` → 1 chunk，`tool_call_delta == {"index":0,"id":"call_1","name":"get_weather","arguments_delta":""}`（id 取 call_id）
- `item` 为 `{"type":"function_call","name":""}` → `[]`（空 name 不产出）
- `item` 为 `{"type":"message",...}`（非 function_call）→ `[]`
- state 无变化

#### T13：done 系列事件全部 pass（无 chunk、状态不变）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-9 重构后 done 事件误产出（防回归）[P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：三个 done 事件分支 |
| Oracle | golden value |
| Mock | 否 — 纯逻辑零 mock |

**等价类划分**：done 事件类型 → 代表值 = `output_text.done` / `output_item.done` / `content_part.done` / `function_call_arguments.done` 四类

**Given**：`stream_state["response_id"]="resp_s1"`、`pending_usage` 非 None。

**When**：依次分发四个 done 事件。

**Then**：
- 每个均返回 `[]`
- state 四键全部保持不变（response_id/pending_usage/usage_flushed/usage_yielded）

### Group 3 — call() 路径选择与状态（component(fake)，MockTransport）

> 构造：`client = ResponsesAPIClient(api_key="sk-test", model_name="m", base_url="https://api.openai.com/v1", capabilities=OPENAI_RESPONSES)`；
> `client._http_client = httpx.AsyncClient(transport=MockTransport(handler))`；结束后 `await client.aclose()`。
> 成功响应体模板：`{"id":"resp_N","status":"completed","output":[],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2},"model":"m","created_at":123}`。
> 期望值中的 `H(x)` 表示参考实现 `hashlib.sha256(x.encode("utf-8")).hexdigest()[:16]`。

#### C1：首次调用（无 previous_response_id）→ 全量请求 + 完整状态更新

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-D + inv-J + Risk-4 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：路径选择 `previous_response_id is None` → full；检测链全部 False（首次） |
| Oracle | golden value |
| Mock | 是 — MockTransport（假网络，理由见 §4） |

**等价类划分**：冷启动输入 → 代表值 = `messages=[{"role":"system","content":"你是助手"},{"role":"user","content":"你好"}]`，`tools=None`

**Given**：
- client 默认构造（`_previous_response_id=None`、`_last_messages_len=0`、`_tools_hash=None`、`_instructions_hash=None`）
- handler 捕获请求体，返回成功响应 `{"id":"resp_1",...}`

**When**：`await client.call(messages)`。

**Then**：
- 网络副作用：handler 恰好收到 1 个 POST 到 `https://api.openai.com/v1/responses` 的请求
- 请求体（解析 JSON）：**不含** `previous_response_id` 键；`instructions == "你是助手"`；`input == [{"role":"user","content":"你好"}]`（完整转换）；`model == "m"`
- 返回值：`result["id"] == "resp_1"`、`result["finish_reason"] == "stop"`
- 状态副作用：`client._previous_response_id == "resp_1"`、`client._last_messages_len == 2`、`client._tools_hash is None`、`client._instructions_hash == H("你是助手")`（inv-J）

#### C2：正常增量（消息增长 + tools/instructions 未变）→ previous_response_id + 仅增量 input

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-E 正常增量 + Risk-2 反向（未误触发）[P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：检测链全 False（instructions 未变 / 未缩短 / 未相等）→ incremental 路径 |
| Oracle | golden value |
| Mock | 是 — MockTransport |

**Given**：
- 第一次调用已建立状态：`messages1=[sys("你是助手"), user("你好")]` → 成功后 `_previous_response_id="resp_1"`、`_last_messages_len=2`、`_instructions_hash=H("你是助手")`
- 本次 `messages2=[sys("你是助手"), user("你好"), user("再问一个问题")]`（消息增长 1 条，system/tools 未变）
- handler 返回 `{"id":"resp_2",...}`

**When**：`await client.call(messages2, tools=None)`。

**Then**：
- 请求体：`previous_response_id == "resp_1"`；`input == [{"role":"user","content":"再问一个问题"}]`（仅增量，截断自 `messages[_last_messages_len:]`）；**不含** `instructions` 与 `tools` 键（增量不重发）
- 状态副作用：`client._previous_response_id == "resp_2"`、`client._last_messages_len == 3`、`client._instructions_hash == H("你是助手")`（不变）

#### C3：instructions 改写（system 压缩/变化）→ 冷启动全量 + 新 instructions 入 body

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-1 instructions 变化漏检 [P0] + inv-D |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_instructions_hash is not None and new != old` → True 分支 |
| Oracle | golden value |
| Mock | 是 — MockTransport |

**Given**：
- 第一次调用建立状态：`messages1=[sys("旧版指令"), user("u1")]` → `_instructions_hash=H("旧版指令")`、`_previous_response_id="resp_1"`
- 本次 `messages2=[sys("新版指令"), user("u1"), user("u2")]`（system 改写 + 消息增长）
- handler 返回 `{"id":"resp_2",...}`

**When**：`await client.call(messages2)`。

**Then**：
- 请求体：**不含** `previous_response_id`（已 invalidate → 全量）；`instructions == "新版指令"`；`input == [{"role":"user","content":"u1"},{"role":"user","content":"u2"}]`（完整 input）
- 状态副作用：`_previous_response_id == "resp_2"`、`_last_messages_len == 3`、`_instructions_hash == H("新版指令")`
- 服务端只收到 1 个请求（无多余的 incremental 尝试）

#### C4：system 被删除（new hash=None ≠ 旧 hash）→ 冷启动全量

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-1 变体（system 消失也必须冷启动）+ inv-B 边界 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：instructions_changed 的 `new is None` 子路径 |
| Oracle | golden value |
| Mock | 是 — MockTransport |

**Given**：
- 第一次调用建立状态：`messages1=[sys("旧版指令"), user("u1")]` → `_instructions_hash=H("旧版指令")`
- 本次 `messages2=[user("u1")]`（system 被删除，消息缩短到 1 条）
- handler 返回 `{"id":"resp_2",...}`

**When**：`await client.call(messages2)`。

**Then**：
- 请求体：**不含** `previous_response_id`（触发路径：instructions_changed 先 invalidate，随后 messages_shortened 分支因 `len(1) < 0` 为 False 不再重复触发）
- 状态副作用：`_previous_response_id == "resp_2"`、`_last_messages_len == 1`、`_instructions_hash is None`（本次无 system，hash 落为 None，下次不参与检测）
- 服务端只收到 1 个请求

#### C5：no_increment（增量模式下消息未增长）→ 冷启动全量

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-2 重发相同消息误走增量 [P0] + inv-D |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_previous_response_id is not None and len == _last_messages_len` → True 分支 |
| Oracle | golden value |
| Mock | 是 — MockTransport |

**Given**：
- 第一次调用建立状态：`messages1=[sys("指令"), user("u1")]` → `_previous_response_id="resp_1"`、`_last_messages_len=2`
- 本次 `messages2=[sys("指令"), user("u1")]`（**完全相同**，调用方重发）
- handler 返回 `{"id":"resp_2",...}`

**When**：`await client.call(messages2)`。

**Then**：
- 请求体：**不含** `previous_response_id`（no_increment → invalidate → 全量）
- 状态副作用：`_previous_response_id == "resp_2"`、`_last_messages_len == 2`
- 服务端只收到 1 个请求

#### C6：首次调用（初始态 None/0）不误触发 no_increment / instructions_changed

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-10 初始态误伤 [P2] + inv-B/inv-D |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：no_increment 的 `_previous_response_id is not None` 前置条件 False；instructions_changed 的 `_instructions_hash is not None` 前置条件 False |
| Oracle | golden value |
| Mock | 是 — MockTransport |

**等价类划分**：初始态 + 空消息（len 恰好等于初始 0）→ 代表值 = `messages=[]`

**Given**：
- client 默认构造（`_previous_response_id=None`、`_last_messages_len=0`、`_instructions_hash=None`）
- handler 返回 `{"id":"resp_1",...}`

**When**：`await client.call([])`。

**Then**：
- 请求体：不含 `previous_response_id`；不含 `input`（空 messages → `_build_full_request` 不设 input）；不含 `instructions`
- 未发生 invalidate（路径为自然 full，非检测触发）：handler 只收到 1 个请求，且该请求无 `previous_response_id`
- 状态副作用：`_previous_response_id == "resp_1"`、`_last_messages_len == 0`、`_instructions_hash is None`

#### C7：非流式成功后四状态全更新（instructions_hash 参与下次检测的闭环）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-J + Risk-1 的检测数据基础 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | N/A（状态更新无分支；C3 依赖此用例证明的前置） |
| Oracle | golden value |
| Mock | 是 — MockTransport |

**Given**：handler 返回 `{"id":"resp_1",...}`。

**When**：`await client.call([sys("指令"), user("u1")], tools=[{"type":"function","function":{"name":"f1"}}])`。

**Then**：
- 状态副作用：`_previous_response_id == "resp_1"`、`_last_messages_len == 2`、`_tools_hash == H_tools`（`_compute_tools_hash` 参考实现：`sha256(json.dumps(tools, sort_keys=True, ensure_ascii=False))[:16]`）、`_instructions_hash == H("指令")`
- 闭环验证：紧接着再次 `call([sys("指令2"), user("u1"), user("u2")])` 必走全量（C3 已覆盖，此处仅注明前置成立）

#### C8：invalidate 保留 tools/instructions hash，下次计算覆盖（不残留陈旧比较）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-F + Risk-4 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`invalidate` 的保留逻辑 |
| Oracle | golden value |
| Mock | 是 — MockTransport |

**Given**：
- 已建立状态：`_previous_response_id="resp_1"`、`_last_messages_len=2`、`_instructions_hash=H("旧指令")`
- 调用 `client.invalidate("test")`

**When**：`client.invalidate("test")`。

**Then**：
- 状态副作用：`_previous_response_id is None`、`_last_messages_len == 0`
- `_instructions_hash` **仍为** `H("旧指令")`（保留，不参与比较——因为冷启动必全量，下次成功即覆盖；若错误地清空为 None，下次成功前无法与旧值比较，但也不会误伤；若错误地用作比较则会误伤。断言保留以锁定当前语义）
- 下次 `call` 成功后 `_instructions_hash` 被覆盖为本次值（闭环，可并入 C3 验证）

#### C9：call() fallback 降级——incremental 400 invalid_request_error → 全量重试 → 状态含 instructions_hash

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-7 fallback 状态更新（含新增的 instructions_hash）+ Risk-4 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_is_response_id_expired_error` True 且 `path=="incremental"` → `_handle_expired` |
| Oracle | golden value |
| Mock | 是 — MockTransport（故障注入点） |

**Given**：
- 第一次调用建立状态：`_previous_response_id="resp_1"`、`_last_messages_len=2`、`_instructions_hash=H("指令")`
- handler 分流：请求体含 `previous_response_id` → 返回 400 `{"error":{"message":"previous_response_id ... expired"}}`（`_classify_http_error` → `LLMRequestError` status 400 → `_is_response_id_expired_error` True）；不含 → 返回 `{"id":"resp_2",...}`
- 本次 `messages2=[sys("指令"), user("u1"), user("u2")]`（正常增量形态）

**When**：`await client.call(messages2)`。

**Then**：
- 网络副作用：handler 收到 **2 个请求**——第 1 个含 `previous_response_id=="resp_1"`（incremental，被 400），第 2 个**不含** `previous_response_id` 且 `input` 完整（fallback 全量重试）
- 返回值：`result["id"] == "resp_2"`（来自重试响应，非异常抛出）
- 状态副作用：`_previous_response_id == "resp_2"`、`_last_messages_len == 3`、`_instructions_hash == H("指令")`（fallback 路径也走 `_update_state_after_success` → 新增的 instructions_hash 更新生效）
- 故障注入：注入点 = MockTransport handler 的 incremental 分支；期望行为 = 不向调用方抛错，内部重建全量并成功返回

### Group 4 — stream_response()（component(fake)，MockTransport）

> 流式响应用 `httpx.Response(200, text=SSE文本)` 返回；SSE 行格式 `data: {json}\n\n`（`_parse_sse_line` 只解析 `data:` 前缀行，`[DONE]` 结束）。handler 按请求体是否含 `previous_response_id` 分流（同 C9）。
> 收集：`chunks = [c async for c in client.stream_response(messages)]`。

#### S1：完整事件流 → chunk 序列正确 + 流结束状态更新（含 instructions_hash）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-9 映射回归 + inv-H + Risk-1（流式状态含 instructions_hash）[P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：主循环全事件路径 + 流结束状态更新分支 |
| Oracle | golden value（chunk 序列逐项可推导） |
| Mock | 是 — MockTransport |

**Given**：
- 首次调用（`_previous_response_id=None`），`messages=[sys("指令"), user("u1")]`
- SSE 文本按序含 9 个事件（顺序即产出顺序）：
  1. `{"type":"response.created","response":{"id":"resp_s1"}}`
  2. `{"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"get_weather"}}`
  3. `{"type":"response.function_call_arguments.delta","item_id":"fc_1","output_index":0,"delta":"{\"city\":"}`
  4. `{"type":"response.function_call_arguments.delta","item_id":"fc_1","output_index":0,"delta":"\"北京\"}"}`
  5. `{"type":"response.output_text.delta","delta":"今天"}`
  6. `{"type":"response.output_text.delta","delta":"晴"}`
  7. `{"type":"response.reasoning_text.delta","delta":"思考中"}`
  8. `{"type":"response.refusal.delta","delta":"拒绝"}` + `output_text.done`/`output_item.done`/`content_part.done` 各一
  9. `{"type":"response.completed","response":{"id":"resp_s1","status":"completed","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}}` + `[DONE]`

**When**：`chunks = [c async for c in client.stream_response(messages)]`。

**Then**：
- 请求体：不含 `previous_response_id`（首次全量）
- chunk 序列逐项（共 9 个）：
  1. `tool_call_delta == {"index":0,"id":"call_1","name":"get_weather","arguments_delta":""}`
  2. `tool_call_delta == {"index":0,"id":"fc_1","name":"","arguments_delta":"{\"city\":"}`
  3. `tool_call_delta == {"index":0,"id":"fc_1","name":"","arguments_delta":"\"北京\"}"}`
  4. `delta_content == "今天"`
  5. `delta_content == "晴"`
  6. `delta_reasoning_content == "思考中"`
  7. `refusal_delta == "拒绝"`
  8. （三个 done 事件 → 无 chunk，不占位）
  9. 末 chunk：`finish_reason == "stop"` 且 `usage == {"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}`（completed 产出）
- usage chunk 总数 == 1（无兜底重复，见 S2）
- 状态副作用（inv-H）：`_previous_response_id == "resp_s1"`、`_last_messages_len == 2`、`_instructions_hash == H("指令")`（**本次改动新增的流式状态更新**）

#### S2：usage 不重复 flush——completed 已 flush 后循环兜底不再 yield

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-G + Risk-6 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：兜底 flush 的 `not usage_flushed` 条件为 False |
| Oracle | golden value |
| Mock | 是 — MockTransport |

**Given**：SSE = `created` + `completed`（含 usage）+ `[DONE]`。

**When**：收集全部 chunks。

**Then**：
- 恰有 1 个 chunk 携带 `usage`（completed 那次）
- 无第二个 usage chunk（兜底 flush 因 `usage_flushed=True` 被短路；`usage_yielded` 未被置位也无影响）
- `finish_reason == "stop"` 仅出现在该 chunk

#### S3：无 completed 事件 → 无 usage chunk（pending_usage 为 None，兜底不产出）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-G 反向 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：兜底 flush 的 `pending_usage is not None` 条件为 False |
| Oracle | golden value |
| Mock | 是 — MockTransport |

**Given**：SSE = `created` + 若干 `output_text.delta` + `[DONE]`（无 completed，usage 数据从未出现）。

**When**：收集全部 chunks。

**Then**：
- 仅 delta_content chunks（如 `["今天","晴"]`），全部 chunk 的 `usage is None`
- 无异常抛出，流正常结束

#### S4：流式 fallback——主循环 400 expired → 全量流式重试 → 状态含 instructions_hash

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-7 + Risk-9（降级循环共用分发）+ inv-H [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_is_response_id_expired_error` True 且 `path=="incremental"` → `need_fallback` |
| Oracle | golden value |
| Mock | 是 — MockTransport（故障注入点） |

**Given**：
- 已建立状态：`_previous_response_id="resp_1"`、`_last_messages_len=2`、`_instructions_hash=H("指令")`
- handler 分流：含 `previous_response_id` → 400 expired（同 C9）；不含 → 200 + SSE（`created{id:"resp_f1"}` + `output_text.delta{delta:"fallback文本"}` + `completed{usage}` + `[DONE]`）
- `messages2=[sys("指令"), user("u1"), user("u2")]`

**When**：收集全部 chunks。

**Then**：
- 网络副作用：2 个请求——第 1 个含 `previous_response_id`（incremental，400），第 2 个不含且 `input` 完整（fallback 全量）
- chunk 序列：`delta_content == "fallback文本"` + 末 chunk `finish_reason=="stop"` 且 `usage` 归一正确（来自 fallback 循环的 `_dispatch_stream_event`）
- 无异常向调用方抛出（need_fallback 后正常 yield）
- 状态副作用（inv-H）：`_previous_response_id == "resp_f1"`、`_last_messages_len == 3`、`_instructions_hash == H("指令")`（fallback 结束同样更新 instructions_hash）

#### S5：流无 response_id（无 created/completed）→ 四状态全部保持

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-H 守卫 + Risk-8 异常流用旧 id 续接 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`if stream_state["response_id"]:` 为 False |
| Oracle | golden value |
| Mock | 是 — MockTransport |

**Given**：
- 已建立状态：`_previous_response_id="resp_1"`、`_last_messages_len=2`、`_tools_hash="old_tools"`、`_instructions_hash=H("旧指令")`
- SSE = 仅 `output_text.delta` 系列 + `[DONE]`（无 created、无 completed）
- 本次 `messages2=[sys("新指令"), user("u1"), user("u2")]`

**When**：收集全部 chunks。

**Then**：
- chunk 序列仅 delta_content
- 状态副作用：`_previous_response_id` **仍为** `"resp_1"`、`_last_messages_len` **仍为** `2`、`_tools_hash == "old_tools"`、`_instructions_hash == H("旧指令")`（全部未被污染——不会用旧 id 做错误续接，也不会用本次未完成的流覆盖状态）

---

## 7. known-gap 与观察项

| 类型 | 位置 | 说明 |
|------|------|------|
| 观察项（死代码） | `_convert_messages_to_input` L848-853 | 改动 F 在 L805 新增 `if role == "system"` 后，L848 的 `elif role == "system"` 成为**不可达死代码**（注释为「额外的 system 消息」并转成 `[System]` 前缀 user 消息）。行为上所有 system 消息均走 L805 原样保留，L848 永不执行。**测试按 L805 行为断言（T5/T6），不受影响**；建议后续清理该死分支，不阻塞本次改动 |
| 已知设计声明 | `_compute_instructions_hash` 仅收集 str content | list 结构（`[{type:"text",text:...}]`）的 system content 不参与 hash 检测（T4 锁定）。而 `_extract_instructions`（L776-782）与 `_convert_messages_to_input`（T5）对 list content 的处理不同——若未来 system 用结构化 content，变化检测会失效。属设计边界，非本次改动引入，已在 Risk-11 标注 |
| 既有怪癖（不纳入断言主目标） | `_extract_instructions` L769-784 | 不看 role，`messages[0].content` 为 str 即提取为 instructions。C4 等用例避免让首条非 system 消息干扰主断言（构造时保证 messages[0] 为 system 或断言聚焦于 previous_response_id 缺失与状态更新） |
| known-gap 声明 | 无 | 本次设计基于当前实现（L967-992 / L351-434 / L912-954 / L1304-1412 / L1213-1227 / L805-809），未发现「设计期望 ≠ 实现现状」的差距；若 test-coder 落码时发现偏差，按本文档期望行为写断言并标 `[known-gap]`，不要迁就实现 |

---

## 8. 推断与确认清单

| 项 | 状态 | 说明 |
|----|------|------|
| 测试框架 | 已确认 | `pyproject.toml`：pytest + pytest-asyncio，`asyncio_mode="auto"`，`testpaths=["pandapal","pandaren","scripts"]`；用例文件建议放 `pandaren/llm/tests/test_responses_client.py`（该目录当前仅有 `__init__.py`，无既有测试需兼容） |
| MockTransport 注入方式 | 已确认（用户指定） | 替换 `client._http_client` 的 transport；等价方案 monkeypatch `_send_request` / `_http_client.stream`，两者选一即可 |
| `capabilities` 取值 | 推断（请确认） | 用例用 `OPENAI_RESPONSES`（`pandaren/llm/capabilities.py`）；仅影响 `_build_usage_info` 的 L4 归一回填（本设计 usage 断言用标准 input_tokens/output_tokens 路径，不触发 caps 回填，换用其他 provider caps 不影响断言） |
| fallback 错误形态 | 推断（请确认） | 用 400 + `previous_response_id ... expired` 文案触发 `_is_response_id_expired_error`（L1094-1113 宽泛匹配）；若测试环境中 handler 无法构造 400 文案，可改用 404（同样判定为 expired，L1105） |
| 流式 `aiter_lines` 行为 | 已确认 | MockTransport 返回 `httpx.Response(200, text=SSE文本)` 时逐行迭代按 `\n` 切分，空行被 `continue` 跳过（L513-514），SSE 文本无需 `event:` 行（`_parse_sse_line` 只读 `data:` 前缀，L1437） |
| 非功能测试 | 不适用 | 性能/并发/安全需专项流程，本设计只标注（§3 末行） |
