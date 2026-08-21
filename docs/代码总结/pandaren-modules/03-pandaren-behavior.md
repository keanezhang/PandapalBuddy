# 03 — pandaren/behavior（行为层：运行时保护）

> 模块总结 · 以代码为准（不依赖外部设计文档）· 锚点均为本次核实的 file:line
> 生成时点：2026-08-18 @ git 09b92ff（锚点逐行核实；复跑：34 passed 全绿——
> 曾有的 test_idempotency 顺序依赖失败已修复，见 §10 测试注记）
>
> 行为层是四层架构中「管得住 + 看得见」硬约束的落点：Agent 每一步动作在执行前被
> 权限/审批/边界三重闸门过滤，执行中被五道 Harness 关卡包裹，执行后由反馈机制接管。
>
> **覆盖范围**：`pandaren/behavior/` 全部 14 个源文件（4 条控制链 + Harness 编排器
> + ToolFeedback），并引用其消费的值对象（`tool/types.py`、`tool/definition/tool_result.py`）
> 与装配/调用点（`builder.py`、`engine/run_core.py`）。
> **编号说明**：现有序列 01-identity / 02-llm / 04-tool / 05-memory，本份补 03——
> 行为层在依赖方向 `engine → behavior → capability → identity` 中位于 engine 之下，
> capability 之上，序号与层级位置一致。

---

## 1. 模块定位与职责

行为层回答一个核心问题：**Agent 的每一步是否被允许、是否安全、是否越界？**

- **职责**：在工具执行前后施加运行时约束——权限判定、人工审批、执行上限、失败重试、
  每步停机守卫、上下文预算、调用频率、输出大小、熔断、幂等、硬停止、执行后反馈。
- **不职责**：不实现具体工具（capability/tool 层）、不驱动循环（engine 层）、
  不定义身份（identity 层）。所有判定只依赖传入的**事实**，自身尽量无状态或冻结。

**三件核心事务**：

1. **执行前控制链**（两条）：权限 + 审批（PermissionGuard / HITLController）；
   执行边界（ExecutionLimits / ErrorPolicy / StepGuard / ContextWindowBudget）。
2. **执行中五道 Harness 关卡**（R1 频率 / R2 输出 / R3 熔断 / R4 幂等 / S6 硬停止），
   由 `HarnessExecutor` 包裹在 `ToolRegistry.execute_tool()` 外层统一编排。
3. **执行后控制链**：`ToolFeedbackProvider` 反馈注入（费用记账、观测等「执行后行为」）。

### 覆盖文件清单

```
pandaren/behavior/
├── permission_guard.py          # 权限校验器（无状态纯函数）
├── hitl_controller.py           # HITL 审批决策器（纯决策，无状态）
├── execution_limits.py          # 执行上限（HC5 完全冻结）
├── error_policy.py              # LLM 失败重试策略（冻结）
├── step_guard.py                # 通用每步停机守卫契约（Protocol，应用层实现）
├── context_window_budget.py     # 上下文窗口 Token 配额（S1 不可变）
├── exceptions.py                # BehaviorConfigError（配置错误统一异常）
└── harness/
    ├── __init__.py              # 导出 RateLimiter/OutputGuard/CircuitBreakerManager/
    │                            #   IdempotencyGuard/HaltChecker/HarnessExecutor
    ├── executor.py              # ★ HarnessExecutor：五道关卡的编排器（527 行）
    ├── rate_limiter.py          # R1 调用频率控制（turn 级）
    ├── output_guard.py          # R2 输出大小控制（截断 + 二分）
    ├── circuit_breaker.py       # R3 熔断保护（状态机 + 指数退避）
    ├── idempotency.py           # R4 幂等性保护（turn 级去重）
    ├── halt.py                  # S6 失败硬停止（halt 标记 → 终止 run）
    └── tool_feedback.py         # 执行后反馈 Provider 契约

消费的值对象（capability 层，依赖方向向下）：
pandaren/tool/types.py                     # CircuitState / CircuitBreakerConfig
pandaren/tool/definition/tool_result.py    # ToolResult（halt/truncated/dedup/feedback 字段）
```

---

## 2. 架构全景：四条控制链 + 五道 Harness 关卡

```
             ┌────────────────── engine/run_core.py（8-Phase ReAct）──────────────────┐
             │                                                                        │
  Phase 1    │  reset_turn()                        ← R1/R4 turn 级状态清零 (run_core.py:1286)
  Phase 4    │  prescan 预扫描 Guard + HITL          ← 权限闸门 A (run_core.py:2029-2042)
  Phase 4    │  单工具 Guard + HITL                  ← 权限闸门 A (run_core.py:2079-2113)
  Phase 6    │  harness_executor.execute_tool()      ← 五道关卡 + 执行 (executor.py)
  Phase 7    │  StepGuard.should_halt(StepUsage)     ← 每步停机守卫 (run_core.py:1848-1856)
             └────────────────────────────────────────────────────────────────────────┘
```

**一次工具调用的完整经过**（`HarnessExecutor.execute_tool()`，顺序严格，executor.py）：

```
工具调用（含 tool_name + arguments）
  │
  ├─① R1 RateLimiter.check()        超本轮上限 → 拒绝 ToolResult（不执行）
  ├─② R3 CircuitBreaker.check()     该工具熔断 OPEN → 拒绝 ToolResult（不执行）
  ├─③ R4 Idempotency.check()        in-flight 重复调用 → 返回 deduplicated 结果
  ├─④ ToolRegistry.execute_tool()   真实执行（此处是 capability 层，不在 behavior）
  ├─⑤ R4 Idempotency.record()       in-flight → completed
  ├─⑥ R3 CircuitBreaker.record()    成功 → 复位计数 / 失败 → 计数，达阈值 → OPEN
  ├─⑦ R2 OutputGuard.clean()        输出超限 → 截断 + truncated 标记
  ├─⑧ S6 HaltChecker.check()        result.halt=True → 硬停止信号（终止整个 run）
  ├─⑨ _write_audit()                审计写入（HC4，不可关闭）——「看得见」
  └─⑩ _run_feedback_stage()         ToolFeedbackProvider 依次收集反馈（执行后行为）
```

关键点：**①②③ 是「执行前拒绝」**（不碰真实执行），**⑤⑥⑦⑧ 是「执行后收紧」**
（对结果做去重登记/熔断记账/截断/硬停止），**⑨ 审计全程留痕**，**⑩ 反馈供应用层
（费用记账、观测）在每一步之后接管**。

---

## 3. 装配与注入（builder.py）

### 3.1 `.behavior()` 入口（builder.py:533-579）

```python
def behavior(
    self, *,
    max_steps: int = DEFAULT_MAX_STEPS,            # 30
    step_timeout: float = DEFAULT_STEP_TIMEOUT,    # 120.0s
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,  # 600.0s
    step_guard: StepGuard | None = None,           # 应用层注入的每步停机守卫
    tool_feedback_providers: Sequence[ToolFeedbackProvider] | None = None,
    auto_confirm_high: bool = False,               # HIGH 敏感度工具自动放行
    max_retries: int = 3,                          # LLM 失败重试次数
    base_delay_s: float = 1.0,                     # 指数退避基值
    max_delay_s: float = 30.0,                     # 指数退避上限
) -> "AgentBuilder"
```

组装（builder.py:567-579）：`ExecutionLimits(...)`、`ErrorPolicy(...)`、
`HITLController(auto_confirm_high=...)`、`step_guard`、`tool_feedback_providers` 全部
收集进 `_behavior_defaults`（builder.py:883-889 处落地默认值），构建时统一注入。

### 3.2 Harness 编排器组装（builder.py:964-969）

构建阶段 `HarnessExecutor(tool_registry=..., audit_log=self._audit_log)` 实例化，
随后 **`set_hooks(self._hooks)` 一次性注入**——hooks 注入后不可替换（HC4，
保证审计/观测链路不被后续代码换掉）。

### 3.3 子 Agent 继承（builder.py:1253-1263）

- **继承**：`step_timeout` / `total_timeout`、`step_guard`、`tool_feedback_providers`、
  `context_window_budget`（builder.py:1263）。
- **不继承**：`hooks`（子 Agent 各自装配 hook，防止父级 hook 意外传播到子任务）。

### 3.4 与上下文预算的联动（builder.py:1051-1054）

记忆压缩阈值 `compact_threshold = context_window_budget.get_slot_tokens("conversation")`
——context_window_budget 是 context window token 的**单一真相源**（context_window_budget.py:56），
各消费方（Memory、MessageBuilder、AgentLoop）都从它取配额，不各自为政。

---

## 4. 执行前控制链 A：权限与审批

### 4.1 PermissionGuard（permission_guard.py）

**无状态、确定性纯函数**（permission_guard.py:26），判定只依据三个入参：

```python
def check_permission(
    self,
    sensitive_permissions: frozenset[SensitivePermission],  # 身份声明的敏感权限集合
    tool_sensitivity: SensitivityLevel,                     # 工具敏感度
    tool_permission: SensitivePermission | None,            # 工具要求的敏感权限
) -> str:  # "allow" | "deny"   (permission_guard.py:28-33, 48/52/56/63)
```

决策规则：
- 工具不要求敏感权限（`tool_permission is None`）→ **allow**（普通工具人人可用）。
- 工具要求敏感权限且身份拥有 → **allow**。
- 工具要求敏感权限但身份**未声明** → **deny**（物理阻断，无「提醒一下继续」的中间态）。

运行期由 engine 在 Phase 4 调用（run_core.py:2029-2042），与 HITL 判断组合成
「权限拒绝 → PERMISSION_DENIED 事件」或「放行进入审批」。

### 4.2 HITLController（hitl_controller.py）

**纯决策器、无状态**（hitl_controller.py:82-84）：只回答「是否需要审批」和
「恢复时做什么」，审批结果通过参数显式传入，自身不存储。

**审批决策** `check_approval(sensitivity_value, tool_name)` → `"pass" | "need_approval"`
（hitl_controller.py:109-132）：

| 敏感度 | 决策 | 依据 |
|--------|------|------|
| CRITICAL (4) | 强制 `need_approval` | **HC6：不可绕过**（hitl_controller.py:116, 120-122） |
| HIGH (3) | `auto_confirm_high=True` → pass；否则 need_approval | 创建后冻结的开关（HC1/HC2） |
| MEDIUM / LOW | `pass` | 低风险放行 |

**恢复决策** `resolve_resume(hitl_decision, pending)` → `ResumeDecision`
（hitl_controller.py:136-140）：
- `approved` → `execute_pending`（直接执行 pending 中保存的工具调用）
- `rejected` → `reject_and_halt`（终止 run）

`PendingApproval` 是 frozen dataclass（hitl_controller.py:29-33），从 HITL_REQUESTED
到 resume 之间传递的不可篡改上下文。engine 在 run_core.py:670-672 调 `resolve_resume`
完成暂停恢复。

---

## 5. 执行前控制链 B：执行边界

四个组件全部是**冻结值对象**（构造后不可改），把「边界」固化成不可变事实：

### 5.1 ExecutionLimits（execution_limits.py）

| 字段 | 默认值 | 校验（execution_limits.py:34-50） |
|------|--------|-----------------------------------|
| `max_steps` | 30 | 正整数 |
| `step_timeout` | 120.0s | > 0，且 ≤ total_timeout |
| `total_timeout` | 600.0s | > 0 |

`__slots__` + `__setattr__` 抛 `PermissionError` 物理阻断修改（execution_limits.py:61-65，
HC5「完全冻结」）。

### 5.2 ErrorPolicy（error_policy.py）

LLM 调用失败重试策略：`max_retries=3` / `base_delay_s=1.0` / `max_delay_s=30.0`
（error_policy.py:16-18）。退避公式（error_policy.py:72-77）：

```
delay = min(base_delay_s * 2^attempt, max_delay_s)
```

### 5.3 StepGuard（step_guard.py）——「SDK 不知道停机理由」

**分层原则**：SDK 只提供一个**通用机制**——每步 LLM 调用结束后，把本步用量事实
`StepUsage`（model / input / output / cached / reasoning tokens / provider / step，
step_guard.py:40-47）交给应用层注入的 `StepGuard`，由它返回 `GuardDecision(halt, reason)`
（step_guard.py:50-58）。`halt=True` → SDK 立即终止 run，`reason` 仅透传给终止事件与审计
（`TerminalReason.HALTED_BY_GUARD`）。

- SDK 完全不知道停机的业务理由——费用、token 总量、自定义策略都行（step_guard.py:9-10）。
- 费用超限只是应用层 `CostBudgetGuard` 的一种实现，机制通用化后同一钩子可承载任意
  「每步之后的业务否决权」（step_guard.py:12-15）。
- **Fail-Safe（O3）**：守卫实现内部任何异常都应吞掉返回 `GuardDecision(False)`，
  绝不因守卫自身问题把 run 炸断（step_guard.py:71-73）。
- 未注入守卫 → 永不停机（step_guard.py:10）。

### 5.4 ContextWindowBudget（context_window_budget.py）

**context window token 单一真相源**（context_window_budget.py:56）。默认配额
（context_window_budget.py:31-32 + constants.py:17/21/25）：

| slot | 默认比例 | 128K 窗口下配额 |
|------|----------|-----------------|
| `system_prompt` | 0.15 | 19200 |
| `tool_schema` | 0.10 | 12800 |
| `conversation` | 0.50 | 64000 |
| `recall` | 0.10 | 12800 |

- 校验：各 ratio ∈ [0, 1.0]；**四者之和必须 ≤ 1.0**（超限抛 `BehaviorConfigError`，
  context_window_budget.py:275-290）；绝对配额 `math.floor` 取整。
- 消费方式：`get_slot_tokens("conversation")`（builder.py:1051-1054 记忆压缩阈值）、
  `build_slot_snapshot()`（供 MessageBuilder 一次性读取，context_window_budget.py:257-270）。
- 保守默认值：未显式传入 context_window 时 WARNING 留痕（E4/E5，context_window_budget.py:108-113）。
- 边界：不管 `max_output_tokens`（属于 llm_settings）、不管实际 token 计数与超限触发行为
  （context_window_budget.py:12-14）。

---

## 6. Harness 五道关卡（R1-R4 + S6）

Harness 从 `tool/harness/` 迁入 behavior 层——**运行时行为约束属于行为策略**
（harness/__init__.py:1-3）。五道关卡对应五项工程原则：

### R1 RateLimiter（rate_limiter.py）— 调用频率控制

- **turn 级**：`_counters: dict[str, int]` 按 tool_name 计数；每轮开头
  `reset_turn()` 清零（run_core.py:1286-1287 每轮 Phase 1 调用）。
- 每次 `execute_tool` 前 `check(tool_name, max_calls)`：超出 → 返回拒绝 ToolResult
  （不执行）；无论是否超限都计数（供观测层使用，rate_limiter.py:23）。
- 未配置 `max_calls` 时只计数不拦截。

### R2 OutputGuard（output_guard.py）— 输出大小控制

- 工具返回结果 JSON 序列化后超长 → **截断 + `truncated=True` 标记**（二分定位截断点）。
- 截断是「收紧」不是「丢弃」——截断后的结果仍回给 LLM，但明确标记。

### R3 CircuitBreaker（circuit_breaker.py + tool/types.py:41-73）— 熔断保护

- 状态机：`CLOSED → OPEN → HALF_OPEN → CLOSED / OPEN`（tool/types.py:41-45 `CircuitState`）。
- 配置（tool/types.py:49-73 `CircuitBreakerConfig`）：`failure_threshold=5`、
  `recovery_timeout=30.0s`、`max_recovery_timeout=300.0s`，`__post_init__` 三档校验
  （失败阈值必须 ≤ 最大恢复超时对应的代数，防止配置倒挂）。
- 流程（circuit_breaker.py:22-83，每工具独立 `_CircuitBreakerState`）：
  - **未注册熔断器的工具 check 直接放行**（circuit_breaker.py:103-105）——熔断只约束
    显式配置了 `CircuitBreakerConfig` 的工具。
  - CLOSED：执行失败 → 计数；达 5 次 → **OPEN**（此后 check 直接拒绝，不执行）。
  - OPEN：等待 `recovery_timeout` 后进入 HALF_OPEN（探活一次）。
  - HALF_OPEN：成功 → 复位 CLOSED；失败 → 回 OPEN 且退避**指数增长**
    （2x/4x/...，上限 `max_recovery_timeout`），达上限后不再探活（永久 OPEN）。
- **R3 铁律**：熔断阈值触发后必须立即终止循环——engine 收到熔断拒绝结果即停（PANDAPAL.md R3）。

### R4 IdempotencyGuard（idempotency.py）— 幂等性保护

- **turn 级去重**：key = `sha256(f"{tool_name}:{canonical_args}")`。
- `check()`：该 key 已在 in-flight 集合 → 返回重复 ToolResult（`deduplicated=True`，
  不真实执行）；否则加入 in-flight。
- `record()`：执行完成后 in-flight → completed。
- `reset_turn()` 清空两集合（与 R1 同节奏，run_core.py:1286-1287）。
- 保护对象：同一步内对同一工具同一参数的**并发/重复**调用（如 gather 并发工具）。

### S6 HaltChecker（halt.py）— 失败硬停止

- 检查 `ToolResult.halt`（工具显式请求停止，tool_result.py:99-131 字段）或
  provider 强制停机信号 → 立即返回硬停止信号，**终止整个 run**（不等本步其余工具）。
- 区别于 R3（熔断拒绝单工具）：S6 是**全局终止**——结果携带 halt 标志即整体停。

---

## 7. HarnessExecutor（harness/executor.py，527 行）

编排器，`ToolRegistry.execute_tool()` 的外层包裹（harness/__init__.py:11）。

### 7.1 `execute_tool()` 调用链（顺序严格，见 §2 图）

```
rate_limiter.check → circuit_breaker.check → idempotency.check
  → tool_registry.execute_tool
  → idempotency.record → circuit_breaker.record → output_guard.clean
  → halt_checker.check → _write_audit → _run_feedback_stage
```

任何一道「执行前拒绝」（R1/R3/R4 check 拒绝）都**短路返回**，不进入真实执行。

### 7.2 `execute_tools_concurrent()`（并发工具）

- `Semaphore(max_concurrency)`（默认 `DEFAULT_MAX_CONCURRENCY=5`）闸门。
- 每个工具独立走 `execute_tool` 全链路；`asyncio.gather` 聚合。
- 任一失败 → 整体失败（失败结果作为**唯一权威结果**返回，附因果信息）。

### 7.3 防御性副本 dc_replace

对 `ToolResult` 的修改一律以 `dc_replace(result, **changes)` 生成**新对象**替换，
不在原对象上就地 mutation（executor.py:353-355, 362-370）。两个硬理由：

1. **R4 幂等缓存污染**：幂等缓存存的是 `store()` 当时的对象引用，就地改会把新状态
   焊进缓存对象，后续命中时重放过期数据。
2. **反馈只读**：交给 feedback provider 的 probe 是防御性副本，物理上阻断对真实
   结果（及已落盘审计）的篡改。

### 7.4 `_write_audit`（HC4）

每次工具调用（含被拒绝的）都写审计——**任何代码路径都不得绕过审计写入**
（PANDAPAL.md HC4），拒绝路径同样留痕（谁在何时想干什么、被哪道关卡拦下）。

### 7.5 `set_hooks`（单次注入）

仅接受一次 hooks 注入，之后再次调用不生效（builder.py:966 装配），防止运行期
被替换导致观测链路断裂。

---

## 8. 执行后控制链：ToolFeedback（tool_feedback.py + tool_result.py）

### 8.1 设计动机

权限/审批/熔断/限频全是「执行前/执行中」约束；**执行之后**（如费用记账、质量观测）
需要一个统一注入点——这就是 `ToolFeedbackProvider`。

### 8.2 契约（tool_feedback.py:24-66）

```python
@runtime_checkable
class ToolFeedbackProvider(Protocol):
    async def provide(self, tool_name: str, args: dict,
                      result: ToolResult, ctx: ToolContext,
                      ) -> ToolFeedback | None: ...   # (tool_feedback.py:48-54)
```

典型实现（tool_feedback.py:28-29）：代码质量门控（写完 .py 就跑 lint 把诊断回灌给
LLM）、密钥泄漏扫描、敏感词检查——框架不含任何领域判断，「检查什么/什么算问题」
全在实现方。

- **只读是机制而非君子协定**（executor.py:362-370）：`result` 入参是 `dc_replace`
  产生的**防御性副本**（probe）——ToolResult 是可变 dataclass，光靠 Protocol 文档
  写「只读」拦不住 `result.success = False` 就地改写；副本让「Agent 看到的与审计
  （HC4）分道扬镳」这条路**物理上走不通**。用副本而非深拷贝的原因：浅拷贝挡住字段
  改绑（success/data/error 三个点名攻击面），深拷贝对任意工具载荷代价与风险过高。
- **为什么不能就地改**（executor.py:353-355）：R4 幂等缓存存的是 `store()` 当时的
  **对象引用**，就地 mutation 会把反馈焊进缓存对象，后续命中时重放过期诊断。
- **失败安全**：provider 抛异常 → 吞掉丢弃该条（O3 兜底）；单 provider 失败不影响
  其他（executor.py:384-389）。**但 `CancelledError` 不吞**——取消优先级最高，必须
  向上传播（executor.py:382-383）。
- **不阻塞**：顺序执行、每个 provider 独立硬超时（`PROVIDER_HARD_TIMEOUT_SECONDS=10.0`，
  超时丢弃该条，executor.py:372-381）；阻塞 I/O 必须走 async（tool_feedback.py:39-40）。
- **返回 None = 无反馈**（零打扰，框架不追加任何文本，tool_feedback.py:32）。

### 8.3 合并语义（executor.py `_merge_feedback`，404-437）

多个 provider 的反馈 → 合并为一个 `ToolFeedback`：

| 维度 | 规则 |
|------|------|
| 文本 | **全部拼接**（不取首个）——「有 lint 错误」与「泄漏密钥」是正交事件，两条都必须到达 LLM（executor.py:406-409） |
| severity | 取最高，供 block 级门控判定（executor.py:434） |
| 可见性 | **混合可见性**：全不可见 → 合并为不可见（纯状态播报，LLM 一个 token 不花）；有可见的 → 合并为可见且 **text 只拼可见的那些**——宁可少一个「检查通过」绿角标，也绝不能把状态播报混进 LLM 该读的告警里（executor.py:411-437） |
| 来源标记 | `source=COMPOSITE_SOURCE`（tool_result.py:51）；各分段 text 自带 `[source]` 标签（HC4 可溯源，executor.py:433） |

`ToolFeedback` 挂在 `ToolResult.feedback`（tool_result.py:65-98，含 source 列表），
`FeedbackSeverity` 枚举（tool_result.py:54-64）供应用层按严重度分级处理。

### 8.4 与 StepGuard 的形状对齐

ToolFeedback（执行后）与 StepGuard（每步后）是**同一分层哲学的两次落地**：
SDK 只提供机制与事实转交，业务判断（费用、质量、策略）全归应用层注入的实现。

---

## 9. 与 engine 的协作点（run_core.py 调用锚点）

| 时机 | 调用 | 锚点 |
|------|------|------|
| 每轮 Phase 1 | `harness_executor.reset_turn()`（R1/R4 turn 级状态清零） | run_core.py:1286-1287 |
| 每步 LLM 后 | `StepGuard.should_halt(run_id, StepUsage)`（含 cached/creation/reasoning token 组装） | run_core.py:1848-1856 |
| Phase 4 预扫描 | 批量工具 prescan：PermissionGuard + HITL 组合判定，首个 need_approval 即中断 | run_core.py:2029-2042 |
| Phase 4 单工具 | 单工具 Guard：`check_permission` + `check_approval` | run_core.py:2079-2085 |
| Phase 5 | HITL 暂停：`check_approval` → HITL_REQUESTED 事件挂起 | run_core.py:2109-2113 |
| 恢复路径 | `resolve_resume(hitl_decision, pending)` | run_core.py:670-672 |

**Phase 4 权限闸门组合逻辑**（prescan，run_core.py:2029-2042）：对每个候选工具依次
`check_permission` → `"deny"` 即排除并记 PERMISSION_DENIED；通过后 `check_approval`，
出现第一个 `"need_approval"` 即中断 prescan 进入 HITL 流程（批量场景只挂起第一个，
避免一次请求弹多个审批）。

---

## 10. 测试覆盖

| 测试文件 | 用例 | 覆盖 |
|----------|------|------|
| `behavior/tests/test_behavior.py` | 10 个（test_permission_guard:161 / test_hitl:228 / test_execution_limits:306 / test_error_policy:378 / test_cost_calculator:444 / test_context_window_budget:492 / test_rate_limiter:619 / test_output_guard:668 / test_circuit_breaker:723 / test_idempotency:795） | 各组件核心行为 + 冻结/校验负例 |
| `behavior/tests/test_behavior_mock.py` | 7 个（test_*_mock:165/227/279/310/359/418/496） | 无 LLM 依赖的快速回归版 |
| `behavior/harness/tests/test_feedback_stage.py` | 22 个（test_feedback_stage.py:107-292） | feedback stage 全覆盖：无 provider / 全静默 / 单 provider / 失败隔离 / 超时 / 不可改写 result / multi-source 合并 / severity 取最高 / composite 标记 / 静默跳过 |

覆盖亮点：反馈阶段用 6 个特殊 Provider（Recording/Silent/Exploding/Mutating/Slow，
test_feedback_stage.py:30-71）逐一验证失败模式——这是「预判失效模式」原则的测试落地。

> **测试注记（已修复）**：曾存在 33 passed + 1 failed（`test_idempotency`）——根因是
> 测试辅助函数 `async_run`（test_behavior.py:140）使用 Python 3.12 已弃用的
> `asyncio.get_event_loop()`，受前置用例的 event loop 状态影响（顺序依赖），属测试
> 基础设施兼容问题，非 IdempotencyGuard 逻辑缺陷。已修复为 `asyncio.new_event_loop()`
> （与 test_behavior_mock.py:70 对齐），复跑 `pytest pandaren/behavior` 为 **34 passed**。

---

## 11. 关键设计决策与权衡

1. **Harness 为什么在 behavior 层**（harness/__init__.py:1-3）：原 `tool/harness/` 迁入。
   运行时行为约束属于行为策略而非工具机制——五道关卡与权限/审批同一层、同一装配入口。
2. **值对象为什么在 capability 层**：`ToolResult.feedback`/`CircuitBreakerConfig` 定义在
   `tool/` 下——依赖方向 `behavior → capability` 单向，行为层只能**消费**工具层的值对象，
   不能反向定义。ToolResult 承载 halt/truncated/deduplicated/feedback 四类行为结果标记。
3. **冻结 vs 可变**：决策器（PermissionGuard/HITLController）无状态纯函数；
   边界值对象（ExecutionLimits/ErrorPolicy/ContextWindowBudget）创建后完全冻结——
   「边界一旦声明不可被运行期篡改」，是 HC1 物理阻断哲学的推广。
4. **机制与判定分离**：StepGuard / ToolFeedback 都是「SDK 提供机制 + 应用层注入判定」，
   SDK 层不引入费用/质量概念（step_guard.py:12-15），避免 SDK 被业务概念污染。
5. **失败隔离白名单**：StepGuard 内部异常必须吞掉返回不停机（step_guard.py:71-73）、
   反馈 provider 异常吞掉继续（§8.2）——守卫/反馈是「附加层」，它们的失败不能炸断主链路；
   但**权限判定/HC6 审批/熔断触发**是主链路闸门，失败必须显式呈现。
6. **拒绝也留痕**：被 R1/R3/R4 拒绝的工具调用同样写审计（§7.4）——「看得到」包括
   「看不到的事为什么没发生」。

---

## 12. 失效模式与防护

| 失效场景 | 后果 | 防护 |
|----------|------|------|
| StepGuard 内部抛异常 | 每步停机判断失效 | Fail-Safe 约定：吞掉返回 `GuardDecision(False)`（step_guard.py:71-73） |
| 反馈 provider 抛异常/挂起 | 费用记账、观测中断 | 失败隔离：单 provider 异常不影响主链路；10s 硬超时只丢该 provider（test_feedback_stage.py:158-211） |
| 反馈 provider 就地改写 result | Agent 看到的与审计（HC4）分歧 | 防御性副本 probe：篡改只作用于副本，物理上走不通（executor.py:362-370） |
| 反馈 provider 吞掉取消 | 取消信号失效、session 冻结 | `CancelledError` 不吞，向上传播（executor.py:382-383） |
| 就地改 ToolResult 污染幂等缓存 | 后续命中重放过期诊断 | 一律 `dc_replace` 生成新对象（executor.py:353-355） |
| 熔断 OPEN 期间请求 | 该工具被直接拒绝 | check 短路返回拒绝 ToolResult，**不触碰真实执行**（§7.1 ②） |
| 同步并发重复调用同一工具 | 副作用重复执行 | R4 turn 级去重，`deduplicated=True` 标记（§6 R4） |
| 工具输出超大 | 上下文爆炸 / 计费失真 | R2 截断 + `truncated` 标记（§6 R2） |
| 配置倒挂（失败阈值 ≥ 恢复上限） | 熔断永不恢复或反复开关 | `CircuitBreakerConfig.__post_init__` 三档校验 fail-fast（tool/types.py:49-73） |
| 未注入 step_guard / feedback | 每步停机 / 执行后记账缺失 | 显式约定：未注入即不启用（step_guard.py:10）；应用层装配责任（builder.py:539-540） |
| hooks 运行期被替换 | 审计/观测链路断裂 | `set_hooks` 单次注入（§7.5，HC4） |
| 上下文 slot 比例之和 > 1.0 | 各 slot 配额叠加超窗口 | 构造期 `_validate_ratios` 抛 `BehaviorConfigError`（context_window_budget.py:275-290） |

---

## 附：快速事实卡

- 行为层默认边界：`max_steps=30` / `step_timeout=120s` / `total_timeout=600s` /
  `max_retries=3` / 退避 `1.0s·2^n`（封顶 30s）/ 熔断 5 次失败 / 恢复 30s·2^n（封顶 300s）。
- 审批底线：**CRITICAL 强制 HITL，`auto_confirm_high` 只能放行 HIGH，永远碰不到 CRITICAL**（HC6）。
- 五道关卡代号：R1 频率 / R2 输出 / R3 熔断 / R4 幂等 / S6 硬停止（harness/__init__.py:6-9）。
- 每轮节奏：`reset_turn()` 清零 R1/R4（run_core.py:1286-1287）；`should_halt` 每步判定
  （run_core.py:1848-1856）。
