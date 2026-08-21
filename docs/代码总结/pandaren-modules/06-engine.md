# 06 — pandaren/engine（8-Phase ReAct 执行内核）

> 模块总结 · 以代码为准（不依赖外部设计文档）· 锚点均为本次核实的 file:line
> 生成时点：2026-08-18 @ git 09b92ff

## 1. 模块定位与职责

**一句话**：pandaren 的心脏——把「思考 → 行动 → 观察」的 ReAct 循环固化为单一体 `_run_stream_core()` 的 8-Phase 流水线，并提供 `run()`（drain 消费）与 `run_stream()`（passthrough）双出口；上层所有 Agent 行为（工具调用、HITL、权限、Plan Mode、取消）都在这条流水线上发生。

它是四层架构（engine → behavior → capability → identity）的**最顶层**：engine 向下 import behavior（PermissionGuard/HITLController/Harness）、capability（ToolRegistry/LLM/Memory）、identity（Identity），是唯一同时接触四层的编排中枢。engine 自己不实现任何安全机制，它**在硬编码位置调用** behavior 的闸门（HC3/HC4 硬编码在主路径，loop.py:6-11）。

**角色分工**：behavior 层做「能不能做」的判断（权限/审批/熔断），engine 层做「按什么顺序做」的执行（8-Phase 编排 + 消息构建 + 流式事件发射）。engine 不决策，只按契约驱动各层。

覆盖文件与测试清单：

| 文件 | 行数 | 角色 |
|------|------|------|
| `pandaren/engine/run_core.py` | 3310 | ★ 执行内核 RunCoreMixin：`_run_stream_core()` 单一体承载 8-Phase |
| `pandaren/engine/loop.py` | 269 | AgentLoop：`__slots__` + `_FROZEN_ATTRS` 冻结安全组件、Prefix Cache 脏检查 |
| `pandaren/engine/message_builder.py` | 235 | MessageBuilder：静态前缀 + 动态 reminder（PC1-PC5） |
| `pandaren/engine/models.py` | 73 | AgentResult / RunState / StepRecord 数据模型 |
| `pandaren/engine/types.py` | 116 | NextStep / RunStatus / TerminalReason / MessageTrust 枚举 |
| `pandaren/engine/stream.py` | 157 | StreamEventType 17 种 + StreamEvent（零依赖，可被上层 import） |
| `pandaren/engine/output_parser.py` | 45 | LLM 响应解析 → FINAL / TOOL_CALLS |
| `pandaren/engine/step_counter.py` | 54 | 只增不减的步数计数器（HC5） |
| `pandaren/engine/tests/` | — | test_engine_mock / test_loop_integration / test_cancel_resume_mock / test_render_tool_result + ob_engine_list.md |

---

## 2. 方案总览（产品视角）

### 2a. 在什么场景下解决什么问题（场景穷举）

| 场景 | 已有/缺失 | 该场景下的问题（业务语言） |
|------|-----------|---------------------------|
| 用户提一个问题，Agent 要"边想边做"多轮工具调用 | 已有 | 8-Phase 流水线把每轮循环切成固定节拍：准备上下文 → 让模型想 → 解析输出 → 过权限 → 执行工具 → 决定下一步 |
| 想实时看到 Agent 在干什么（打字机效果/工具进度） | 已有 | `run_stream()` 逐个 yield 17 种 StreamEvent，消费方无状态渲染（delta + snapshot 双字段） |
| 想拿到最终结果而非流 | 已有 | `run()` drain 流 → 永远返回 AgentResult，**任何异常都不外抛**（O3） |
| 高敏感工具被调用时暂停等人审批 | 已有 | HITL 暂停：发出 `hitl_requested` 事件 + `terminal_reason=HITL_PAUSED`，run_state 快照可 resume |
| 用户中途喊停 | 已有 | 协作式取消：CancelToken 单向闸门 + 分层检查点，取消转 `agent_cancelled` 事件而非裸异常 |
| Agent 陷入死循环/超时/超步数 | 已有 | 有界循环 + StepCounter 只增不减 + TerminalReason 19 种终止原因可观测 |
| 上下文塞满 | 已有 | Phase 1 压缩（compact_if_needed），压完仍超 → `CONTEXT_OVERFLOW` 终止 |
| 多会话并发下日志/审计归属混乱 | 已有 | `_current_session_id` 每次 run 入口设置、finally 清空，审计/tracer 透传（run_core.py:329 `_audit`） |
| 想实现「同样上下文不重复计费」（Prefix Cache） | 已有 | 静态前缀一次序列化 + 三注册表版本脏检查，字节级稳定保命中 |

### 2b. 总体方案思路

| 关键思路 | 回答的问题 | 核心机制 |
|---------|-----------|---------|
| 单一体内核 | "8 个 Phase 的编排逻辑放哪？" | `_run_stream_core()` 一个方法（run_core.py:512 起 2600+ 行），run/run_stream 只是它的两种消费方式 |
| 双出口共享内核 | "流式和非流式会不会两套逻辑两处 bug？" | 共享零代码，`run()` drain、`run_stream()` passthrough（stream.py:8） |
| 安全闸门硬编码 | "怎么保证权限/审计不被绕过？" | HC3/HC4：check_permission / audit.write_sync 硬编码在 Phase 4/关键节点，不在可选 hook 里（loop.py:6-11） |
| 安全组件冻结 | "运行中能换掉审计/权限对象吗？" | `__slots__` + `_FROZEN_ATTRS`，构造后 `__setattr__` 拦截（loop.py:78-154） |
| 取消是协作式 | "取消会不会把 Agent 打死？" | CancelledSignal 继承 Exception（非 CancelledError），可在 step try 内捕获转事件，永不逃逸出 run() |
| 消息字节级稳定 | "怎么让 LLM Provider 的 Prefix Cache 生效？" | 静态前缀一次性序列化 + 版本脏检查，动态内容尾插 `<system-reminder>`（PC1-PC5） |

---

## 3. 8-Phase 执行流程（技术核心）

> 全部锚点在 `run_core.py`，Phase 行号标记为该方法内对应的执行区段。

```
Phase 0       task 入口 → memory 追加 user message（+ 手动指定 Skill 预加载）
Phase 0.5     802  Manual Skill 预校验：命中 → 直接执行返回，不走 LLM
Phase 1       1293 Prepare — memory.compact_if_needed()（溢出 → AGENT_TERMINATED）
                            — build_tool_schemas（含 ContextWindowBudget 配额）
                            — Plan Mode 工具过滤（plan_manager.filter_tools，1349-1367）
Phase 2       1480 Think   — LLM 调用：真增量流式 path(1503) + 非流式降级(1605)
Phase 3       1898 Parse   — output_parser.parse()，is_final / is_empty 分支
Phase 3.5     1994 预扫描 tool_calls — id 归一化(generate_id)、注册/权限/HITL 边界识别
Phase 4-5     2064 Guard + HITL 串行预检
                            — permission_guard.check_permission → deny 分支(2087)
                            — hitl_controller.check_approval(2109)
                            — interaction 工具（ask_user）识别(2243)
Phase 6       2378 Act     — 构建 ToolContext.metadata（tool_store/discovery/cancel_token/
                             skill_registry/agent_registry，2389-2404）
                            — 并发执行 + 取消竞态（_execute_tools_with_cancel_race:359）
                            — 原子提交：assistant(tool_calls) 与工具结果一并写 memory
                              （_commit_tool_step_atomically:450）
Phase 7-8     2451 Halt + Observe — _tool_halt_signal / _plan_complete_signal
                            统一提交再停机；工具结果全部落 _pending_tool_results
```

关键内部方法（run_core.py）：

| 方法 | 行号 | 职责 |
|------|------|------|
| `run_stream()` | 199 | passthrough：逐个 yield StreamEvent |
| `run()` | 256 | drain 消费内核，永远返回 AgentResult（O3） |
| `_audit()` | 329 | 审计写入单点，session_id 透传（HC4） |
| `_execute_tools_with_cancel_race()` | 359 | 工具并发执行 + 取消竞速（asyncio.wait） |
| `_commit_tool_step_atomically()` | 450 | assistant(tool_calls) 与工具结果原子写入 memory |
| `_resolve_run_llm()` | 481 | 解析本次 run 的 LLM 客户端（子 Agent 覆盖） |
| `_run_stream_core()` | 512 | ★ 8-Phase 单一体 |
| `_safe_hook()` | 3124 | hook 调用单点：session_id 注入 + 异常抑制（hook 不炸主循环） |
| `_resolve_progress_label()` | 3153 | 进度标签解析 |
| `_rebuild_pending_approval()` | 3188 | resume 时从 RunState 重建 PendingApproval |

### 3a. Step 循环与 NextStep 决策

每个 step 结束后由 `NextStep`（types.py:6）决定去向：

| NextStep | 含义 | 典型路径 |
|----------|------|---------|
| `CONTINUE` | 正常进入下一个 step | 工具调用成功 |
| `RETRY` | 本 step 失败但可重试（LLM 临时错误） | 受 ErrorPolicy 控制（max_retries） |
| `FINAL` | LLM 返回最终答案（无 tool_call） | 正常完成 |
| `HALT` | 不可恢复终止（超时/超预算/熔断/强制停止） | TerminalReason 记录原因 |
| `PAUSE` | HITL / 交互暂停，等人工 | 携带 RunState 快照 |
| `HANDOFF` | 移交给另一个 Agent（P2 预留） | 当前未启用 |

### 3b. 有界循环与 StepCounter

`for step in range(max_steps)`（HC5）+ `StepCounter`（step_counter.py:13）只增不减、不可重置、不可改上限；`increment()` 返回 False 即触顶 → `MAX_STEPS_EXCEEDED`。

---

## 4. 双出口：run / run_stream

| 维度 | `run()` | `run_stream()` |
|------|---------|----------------|
| 行号 | run_core.py:256 | run_core.py:199 |
| 语义 | drain 消费内核 | passthrough |
| 返回 | `AgentResult`（永不抛异常，O3） | `AsyncIterator[StreamEvent]` |
| 消费方 | 应用层直接调用（pandapal AgentExecutor） | 需要实时进度的前端/流式消费方 |
| 异常 | 内部转换为 AgentResult.error | 内部转换为 AGENT_HALTED / AGENT_CANCELLED 事件 |

**AgentResult**（models.py:46）关键字段：`success/output/error/terminal_reason/run_id/total_steps/total_duration_ms/total_input_tokens/total_output_tokens/steps/run_state/started_at/finished_at/plan_path`。`paused` property = `not success and run_state is not None`（models.py:71）。

> 注：费用**不在 SDK 计算**——价格与预算归应用层（models.py:59 注释：SDK 只报 token 用量，应用层按价格表自算，如看板 cost_breakdown）。

### 4a. HITL 暂停与恢复（RunState）

`RunState`（models.py:25）是 PAUSE 时的**可序列化快照**：`run_id/agent_id/step_n/session_id/messages/pending_tool_call/working/metadata`，全部字段 JSON 安全（禁存自定义对象）。`session_id` 是隔离必填字段——resume 时必须与 pause 时一致，防跨会话越权恢复（SESSION_ID 契约红线 7）。恢复路径：`resume_state` → `_rebuild_pending_approval`（run_core.py:3188）重建审批上下文。

### 4b. 协作式取消

- `CancelToken`（cancellation.py:38）：`__slots__` 单向闸门，`cancel()` 幂等（只记录首个 reason）；`raise_if_cancelled()` 检查点抛 `CancelledSignal`。
- `CancelledSignal` 继承 `Exception` 而非 `asyncio.CancelledError`——后者继承 BaseException 会绕过 O3 的 `except Exception` 兜底（cancellation.py:16-19）。
- 检查点分层（loop.py:159-163）：Layer 0 step 循环头 / Layer 1 LLM 流式逐 chunk / Layer 2-3 工具边界与子 Agent。
- 每次 run 入口**重建 token**（loop.py:127-128），确保干净起点、AgentLoop 可复用。
- `AgentLoop.cancel()`（loop.py:156）供外部触发；`_cancelled` 保留为只读 property 读 token（loop.py:171）。

---

## 5. 流式事件（StreamEventType，17 种）

> ⚠️ stream.py:19 的 docstring 写「16 种」是**过时注释**——实际枚举 17 个（新增 `PLAN_APPROVAL_REQUESTED` 或 `LLM_REASONING_TOKEN` 时未同步注释）。

| # | 事件 | 值 | 携带数据 | 层级 |
|---|------|----|---------|------|
| 1 | `RUN_START` | run_start | run_id、task 摘要 | Run |
| 2 | `RUN_END` | run_end | AgentResult 字段（不含 run_state） | Run |
| 3 | `STEP_START` | step_start | step_n | Step |
| 4 | `STEP_END` | step_end | step_n + duration_ms + tokens | Step |
| 5 | `LLM_CALL_START` | llm_call_start | model_name（含重试） | LLM |
| 6 | `LLM_TOKEN` | llm_token | delta + snapshot（P0 全量 / P1 真增量） | LLM |
| 7 | `LLM_REASONING_TOKEN` | llm_reasoning_token | 推理内容增量（qwen3-plus/doubao-thinking 等） | LLM |
| 8 | `LLM_CALL_END` | llm_call_end | input_tokens + output_tokens | LLM |
| 9 | `TOOL_CALL_START` | tool_call_start | tool_name + args 摘要 | Tool |
| 10 | `TOOL_CALL_END` | tool_call_end | tool_name + success + data 摘要 | Tool |
| 11 | `PERMISSION_DENIED` | permission_denied | tool_name + permission_required | 安全反馈 |
| 12 | `HITL_REQUESTED` | hitl_requested | tool_name + sensitivity（发出后 run 立即结束 = PAUSE） | 安全反馈 |
| 13 | `INTERACTION_REQUESTED` | interaction_requested | tool_args + run_state（ask_user 等交互工具） | 安全反馈 |
| 14 | `AGENT_HALTED` | agent_halted | terminal_reason + error | 终止 |
| 15 | `AGENT_CANCELLED` | agent_cancelled | — | 终止 |
| 16 | `HANDOFF` | handoff | target_agent_id（P2 预留） | 预留 |
| 17 | `PLAN_APPROVAL_REQUESTED` | plan_approval_requested | plan_path + plan_content | Plan Mode |

`StreamEvent`（stream.py:130）frozen dataclass：`type/data/run_id/agent_id/step_n(-1=Run 级)/tool_name`。设计原则：最小传输单元、不携带可变对象引用、枚举可扩展（stream.py:5-9）。

---

## 6. 其他枚举

| 枚举 | 值 | 说明 |
|------|----|------|
| `NextStep` | 6 种（types.py:6） | CONTINUE/RETRY/FINAL/HALT/PAUSE/HANDOFF |
| `RunStatus` | 6 种（types.py:26） | PENDING/RUNNING/PAUSED/COMPLETED/FAILED/CANCELLED |
| `TerminalReason` | 19 种（types.py:46-102） | 正常 1 + 预算 4 + 上下文 1 + 工具/权限 3 + 安全/质量 4 + 外部中断 4 + 规划 1 + LLM 错误 1 |
| `MessageTrust` | 3 种（types.py:105） | HIGH 系统/用户 / MEDIUM LLM 生成 / LOW 外部工具返回 |

---

## 7. MessageBuilder 与 Prefix Cache（PC1-PC5）

MessageBuilder（message_builder.py:41）把 Memory 的 messages + 工具/技能/子 Agent 目录拼装成最终发给 LLM 的 payload：

- **静态前缀** `build_static_context_str()`（classmethod，纯函数）：对 run 稳定的三块 XML 清单（deferred tools / skills / agents）序列化为单一字符串。AgentLoop 构造时调用一次并缓存（`_static_context_str`，loop.py:141），每轮 build 直接复用 → **字节级一致保 Prefix Cache 命中**（PC1/PC2）。
- **动态 reminder** `build_dynamic_reminder()`：每轮变化的内容（本轮激活 Skill 正文 / recall 结果）以独立 `role=user` `<system-reminder>` 消息**尾插**到历史末尾（PC3）。
- **脏检查**（loop.py:176-236）：三注册表 version 全未变 → 直接返回缓存；`ContextWindowBudget` 存在时对 system_prompt 配额校验，超额丢弃/截断 static_context（loop.py:211-234）。

---

## 8. 关键设计决策与权衡

| 决策 | 选择 | 理由 / 代价 |
|------|------|------------|
| 单一体 vs 分阶段类 | `_run_stream_core` 单方法 2600+ 行 | 8-Phase 状态全在局部变量，避免跨对象传状态；代价是方法超大、新人不友好 |
| 双出口共享内核 | run()/run_stream() 零共享代码 | 同一逻辑不会分叉成两套行为；代价是 run() 必须先消费流 |
| 安全闸门硬编码 vs hook | HC3/HC4 在主路径 | hook 可能被覆盖/遗漏，硬编码保证不可绕过；代价是扩展点灵活性下降 |
| 安全组件冻结 | `__slots__` + `_FROZEN_ATTRS`（12 个） | 防运行期替换审计/权限/工具注册表；静态缓存字段（_static_context_str）故意不冻结，因为 Skill/Tool 增删后需重建（loop.py:74-77） |
| 自定义取消信号 | CancelledSignal(Exception) | 协作式 + 可转事件 + 不逃逸 O3 兜底；代价是不能享受 asyncio 强制取消的便捷 |
| Prefix Cache | 静态序列化 + 版本脏检查 | 长 run 每轮省去重复拼装与 token 计费；代价是三注册表任一变化即失效重建 |

---

## 9. 失效模式与处理

| 失效场景 | 后果 | 处理 |
|---------|------|------|
| LLM 调用失败（网络/API/解析） | 本 step 失败 | ErrorPolicy 控制 RETRY（max_retries=3 默认）；重试耗尽 → `LLM_ERROR` |
| 上下文压缩后仍超限 | 无法继续 | `CONTEXT_OVERFLOW` 终止（Phase 1） |
| 单步超时 / 总时长超时 | run 中断 | `STEP_TIMEOUT` / `TOTAL_TIMEOUT`（ExecutionLimits） |
| 连续权限拒绝 | HITL 审批被拒次数超限 | `PERMISSION_EXHAUSTED` 终止 |
| 工具返回强制停止信号 | 工具主动终止 | `TOOL_HALT`，统一提交再停机（Phase 7-8） |
| 熔断器触发 | 连续失败过多 | `CIRCUIT_BREAKER` 立即终止（R3） |
| LLM 输出雷同死循环 | 浪费 token | `LLM_LOOP_DETECTED` 终止 |
| hook 抛异常 | 不应炸主循环 | `_safe_hook` 异常抑制（run_core.py:3124） |
| 审计写入失败 | 审计不可丢 | `_audit` 走 AuditLog.write_sync → AuditWriteError 不可忽略（HC4） |
| 工具并发执行中取消 | 部分工具已跑 | `_execute_tools_with_cancel_race` 竞速，结果不落 memory（原子提交保护） |

---

## 10. 测试情况

| 测试文件 | 覆盖重点 |
|---------|---------|
| `tests/test_engine_mock.py` (37KB) | Mock LLM 下的 8-Phase 主路径、NextStep 决策、终止原因 |
| `tests/test_loop_integration.py` (65KB) | 集成：真实 Memory/Tool 下的完整循环、HITL 暂停恢复 |
| `tests/test_cancel_resume_mock.py` (14.8KB) | 取消语义 + RunState 快照恢复 |
| `tests/test_render_tool_result.py` (7.9KB) | 工具结果渲染格式 |
| `tests/ob_engine_list.md` (7.9KB) | 观测清单（engine 侧可观测点盘点） |

---

## 11. 编号说明与依赖方向

本份为序列 06。engine 是四层架构最顶层（依赖方向 `engine → behavior → capability → identity`），位于已覆盖的 03-behavior 之上；observability（横切）与 hook（横切）将在 07/08 覆盖，agent/builder 门面在 09。
