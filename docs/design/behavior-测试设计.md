# Behavior 层 pytest 测试设计（风险驱动重建）

> 用途：替换即将删除的自制框架测试 `test_behavior.py`（139 断言）+ `test_behavior_mock.py`（约 48 断言，
> 非 pytest 可收集 → CI 假绿）。本文档为 **test-coder 的输入**：每条用例含 Given/When/Then、
> 等价类代表值、Mock/Fake 决策、Oracle、故障注入声明，可直接落成 pytest 代码。
>
> 设计依据：① 被测 API 全景（从源码提取，可信）；② `docs/代码总结/pandaren-modules/03-pandaren-behavior.md`；
> ③ `docs/design/behavior-旧测试覆盖矩阵.md`；④ 真 pytest 参考 `pandaren/behavior/harness/tests/test_feedback_stage.py`
> （已存在 22 条真 pytest，**复用不重测**，见 §3.3）。
>
> 遵守约束：未读取被测源码（executor.py 等），仅有 API 全景与上述文档；所有签名细节的推断
> 在 §6「推断与确认清单」集中声明，落码前以源码核对。

---

## 0. 设计元信息

| 项 | 值 |
|----|----|
| 测试框架 | pytest + pytest-asyncio（或 `asyncio.run` 包装）；日志断言用 `caplog`（次要） |
| 测试层级 | unit + component(fake) 两层；**无 integration / e2e**（理由见下） |
| 独立运行 | 全部用例无网络、无外部依赖、无真实文件/DB；时间敏感用例全部钉死（§2.3） |
| 用例总数 | 60（含 20 条 [P0]） |
| 风险映射 | 每条用例绑定 ≥1 条不变式/风险；风险清单见 §1，覆盖矩阵见 §3 |

**层级声明（为什么没有 integration/e2e）**：behavior 层所有外部依赖（tool_registry、audit_log、
hooks、feedback provider）均为可注入接口，构造期即注入内存实现；CI 要求无网络无外部依赖。
旧测试亦为单元/组件级，重建保持同层级即可无覆盖倒退。审计验证用内存 Fake 而非真实落盘日志。

**Oracle 策略总述**：
- 决策/枚举输出（allow/deny、pass/need_approval、halt）→ golden value（规格白纸黑字，可独立推导）；
- 计数/次数（registry 调用次数、hook 触发次数、_locks 长度）→ golden value；
- 截断输出（OG/EX-05）→ **蜕变关系**：断言「序列化后长度 ≤ max_bytes 且 truncated=True」，
  不硬编码截断后的具体内容（截断点取决于序列化细节，硬编码即自指 oracle）；
- 退避公式（EP）→ golden value（`min(1.0·2^n, 30.0)` 可手算）。

---

## 1. 风险清单（按 P0→P3 排序，S×L 定级）

| 编号 | 风险 | 严重度×可能性 | 优先级 | 关联用例 |
|------|------|---------------|--------|----------|
| Risk-EX-1 | 关卡顺序颠倒：R1/R3/R4 拒绝后仍触碰真实执行（副作用泄漏） | 高×高 | **P0** | EX-02/03/04 |
| Risk-EX-2 | 拒绝路径绕过审计写入（HC4 破坏，「被拦的事」不可见） | 高×高 | **P0** | EX-02/03/04/07 |
| Risk-EX-3 | 并发工具任一失败被吞，调用方拿到「假成功」 | 高×中 | **P0** | EX-10 |
| Risk-EX-4 | S6 halt 硬停止信号丢失，run 未终止 | 高×中 | **P0** | EX-06 |
| Risk-PG-1 | 敏感权限工具被放行（身份未声明或权限不匹配）——物理阻断失效 | 高×中 | **P0** | PG-03/04 |
| Risk-HITL-1 | CRITICAL 被 auto_confirm_high 绕过（HC6 不可绕过底线被击穿） | 高×中 | **P0** | HITL-01 |
| Risk-HITL-2 | 审批结果处理 fail-open：rejected 仍执行（门禁类必须 fail-closed） | 高×低 | **P0** | HITL-06 |
| Risk-CB-1 | 熔断阈值判定错误：未达阈值即 OPEN / 达阈值不 OPEN（故障扩散或误杀） | 高×中 | **P0** | CB-02/03 |
| Risk-CB-2 | OPEN 期间放行（熔断失效） | 高×中 | **P0** | CB-04 |
| Risk-CB-3 | HALF_OPEN 探活恢复错误：探活失败仍回 CLOSED（反复开关） | 高×中 | **P0** | CB-05/06 |
| Risk-ID-1 | 同 turn 同参数并发重复调用 → 副作用重复执行 | 高×中 | **P0** | ID-02/08 |
| Risk-ID-2 | asyncio.Lock 泄漏（check 后不释放）→ 后续调用死锁 | 高×低 | **P0** | ID-06 |
| Risk-OG-1 | 超限输出未截断 → 上下文爆炸 / 计费失真 | 高×中 | **P0** | OG-01 |
| Risk-CWB-1 | ratio 之和 >1.0 未拦截 → 各 slot 配额叠加超窗口 | 高×中 | **P0** | CWB-02 |
| Risk-RL-1 | 超限调用未拒绝 → 频率失控 | 高×中 | **P0** | RL-02 |
| Risk-HALT-1 | 失败且 halt_on_failure=True 未触发硬停止 | 高×中 | **P0** | HALT-01 |
| Risk-EX-5 | 截断/反馈就地 mutation 污染 R4 幂等缓存 → 后续命中重放过期数据 | 中×中 | **P1** | EX-05、OG-01 |
| Risk-EX-6 | 审计先于反馈的时序颠倒 → 反馈内容进审计（HC4 失真） | 中×中 | **P1** | EX-07 |
| Risk-CB-4 | HALF_OPEN 探活失败后退避不翻倍 → 恢复风暴 | 中×中 | **P1** | CB-06 |
| Risk-CB-5 | 熔断 hook 重复触发或参数失真（观测/下游告警失真） | 中×中 | **P1** | CB-03/08 |
| Risk-ID-3 | async check 与 sync store 缓存不同步 → 去重失效 | 中×中 | **P1** | ID-07 |
| Risk-EP-1 | 退避封顶失效（delay 无界）→ 重试等待失控 | 中×中 | **P1** | EP-02 |
| Risk-EL-1 | step_timeout > total_timeout 配置倒挂未拦截 | 中×中 | **P1** | EL-02 |
| Risk-OG-2 | hook 未注册时截断静默失败（降级路径坏掉） | 中×低 | **P2** | OG-04 |
| Risk-EX-7 | hooks 运行期被替换 → 观测链路断裂（HC4） | 中×低 | **P2** | EX-08 |
| Risk-EX-8 | 并发闸门失效（Semaphore 不生效）→ 超出 max_concurrency | 中×低 | **P2** | EX-09 |
| Risk-ID-4 | _make_key 确定性/参数参与错误 → 不同调用误判为重复 | 低×中 | **P2** | ID-03/04 |
| Risk-PG-2 | 决策不确定性（同输入不同输出）→ 权限闸门行为漂移 | 低×低 | **P3** | PG-04（含确定性断言） |
| Risk-CWB-2 | 冻结字段被篡改 → 配额运行期漂移 | 低×低 | **P2** | CWB-04 |
| Risk-CWB-3 | 未显式传 context_window 时静默用默认值 → 用户不知情 | 低×高 | **P2** | CWB-05 |
| 非功能风险（本设计只标注不展开） | 熔断/审批/审计的响应时间、并发压力、日志注入安全 | — | — | 需专项流程，功能设计不假装覆盖 |

---

## 2. 全局测试双与确定性控制

### 2.1 测试双清单（Phase 2 Mock/Fake 决策）

| 依赖 | 决策 | 理由 |
|------|------|------|
| `ToolRegistry`（capability 层真实对象） | **Fake**：`FakeRegistry`（内存，记录 `execute_tool` 调用次数与入参，返回可编程 `ToolResult`） | 真实 registry 触发真实工具副作用；行为层只关心「被调用/未被调用」 |
| `audit_log` | **Fake**：`FakeAudit`（内存 list，`append(entry)`） | 验证 HC4 留痕；真实落盘日志不在 CI 范围 |
| `hooks`（观测回调） | **Fake**：`RecordingHooks`（记录 `on_tool_output_truncated` / `on_tool_circuit_open` / `on_tool_circuit_close` 的调用与参数） | 断言 hook 参数与触发次数（旧 mock 矩阵覆盖点） |
| `ToolFeedbackProvider` | 复用 `test_feedback_stage.py` 的 `RecordingProvider` / `SilentProvider` / `ExplodingProvider` 模式 | 既有真 pytest 已验证 stage 契约，此处只需全链路挂载 |
| `StepGuard` | 不注入（验证「未注入即不启用」）；协议契约用 Mock 鸭子类型 | StepGuard 是 Protocol，业务实现属应用层 |
| 算法/纯函数层（PG/HITL/EL/EP/CWB/HaltChecker） | **零 mock** | 无协作对象，直接测 |

> FakeRegistry 接口（推断，见 §6-1）：`async def execute_tool(self, tool_name: str, args: dict) -> ToolResult`，
> 支持预置返回结果与抛异常两种模式。

### 2.2 HarnessExecutor 构造手法（来自 test_feedback_stage.py 参考）

完整 `execute_tool` 用例用**真实构造**：`HarnessExecutor(tool_registry=fake_registry, audit_log=fake_audit)`
+ `set_hooks(recording_hooks)` + `register_circuit_breaker(...)` + 注入 `rate_limiter` / `output_guard` /
`idempotency_guard` / `halt_checker`（注入方式：构造参数或属性注入，见 §6-2）。
仅 `_run_feedback_stage` 直测场景沿用 `HarnessExecutor.__new__(HarnessExecutor)` + 手工设
`_feedback_providers`（与 test_feedback_stage.py 一致）。

### 2.3 确定性控制（防 flaky）

| 不确定源 | 对策（写入 Given） |
|----------|-------------------|
| 熔断 `recovery_timeout` 计时 | 配置极小值 `recovery_timeout=0.01~0.05s` + `await asyncio.sleep(0.06~0.1)` 真实推进；时间量级差 ≥2 倍，杜绝竞态。若 `_CircuitBreakerState` 支持注入时钟，优先注入（见 §6-3） |
| 反馈 provider 超时 | 仿 test_feedback_stage.py：`ex.PROVIDER_HARD_TIMEOUT_SECONDS = 0.05` + `SlowProvider(delay=5.0)`，不用真实等待 |
| 并发用例时序 | `asyncio.gather` 聚合后断言；并发上限用「事件计数证明」而非 sleep 猜时序（EX-09） |
| 集合/字典顺序 | 断言集合相等或显式排序，不依赖插入序 |
| 浮点 | 退避/配额断言用整数或 `==` 精确值（`1.0·2^n`、floor 结果均可精确表示），不用容差 |

---

## 3. 覆盖矩阵对照（旧 → 新，确保无覆盖倒退）

### 3.1 test_behavior.py（139 断言）映射

| 旧覆盖点 | 新用例 | 状态 |
|----------|--------|------|
| PermissionGuard：allow/deny、敏感权限匹配、信任等级 | PG-01..04（信任等级处理见 §6-4） | ✅ |
| HITLController：HIGH 触发审批、auto_confirm_high、approved/rejected | HITL-01..06 | ✅ |
| ExecutionLimits：步数/时长上限 | EL-01..03 | ✅ |
| CostCalculator：未知模型警告、费用精度 | **不在本次 API 全景范围** | ⚠️ 见 §6-5 |
| ContextWindowBudget：窗口配额、ratio 校验 | CWB-01..05 | ✅ |
| OutputGuard：截断 + 二分 | OG-01..05 + EX-05 | ✅ |
| CircuitBreakerManager：状态机、退避、恢复 | CB-01..08 | ✅ |
| IdempotencyGuard：去重、turn 级重置 | ID-01..08 | ✅ |
| HarnessExecutor：五道关卡编排、审计（HC4） | EX-01..08 | ✅（旧测试实测无此段，重建补齐） |
| StepGuard / ToolFeedback：协议、反馈收集 | SG-01 + 复用 test_feedback_stage.py 22 条 + EX-01/07 | ✅ |

### 3.2 test_behavior_mock.py（约 48 断言）映射

| 旧覆盖点 | 新用例 | 断言方式 |
|----------|--------|----------|
| `_match_permission` 内部逻辑；空 sensitive_permissions → warning | PG-03（caplog 次要） | 行为为主 + caplog |
| 权限拒绝 → warning；放行不触发 | PG-03/04 | caplog 次要 |
| HITL 日志（CRITICAL/HIGH 含 need_approval、pass） | HITL-01/02/03 | 行为为主 + caplog 可选 |
| StepGuard 协议契约 | SG-01 | isinstance + Mock |
| context_window 默认值 warning / sum>1.0 error / 冻结 warning | CWB-05/02/04 | 行为为主 + caplog |
| output_guard：truncated hook 参数、未截断不触发、无 hooks warning、data=None 不触发、3 次触发 3 次 | OG-01/02/03/04/05 | hook 记录断言 + caplog |
| circuit_breaker：未达阈值不触发、达 threshold 触发（参数）、HALF_OPEN→CLOSED close hook、CLOSED 成功不触发、恰好 1 次 | CB-02/03/05/08 | hook 记录断言 |
| idempotency：并发命中、缓存数据、_make_key 参数、store 覆盖、_locks 非空→reset 清空、async/sync 共享缓存 | ID-01..08 | 状态 + 计数断言 |

### 3.3 复用既有真 pytest（不重测）

`test_feedback_stage.py` 22 条已覆盖反馈 stage 全部契约（空列表零开销、失败隔离、CancelledError 传播、
硬超时只丢该条、防御性副本防篡改、多源合并/severity 取最高/composite/静默跳过）。新设计只补
**executor 全链路中的反馈时序**（EX-01/07），不重复设计 stage 内部契约。

---

## 4. 用例设计

### 4.1 HarnessExecutor —— execute_tool 五道关卡编排（component(fake)）

**不变式**：
- inv-EX-1：关卡顺序严格 `R1→R3→R4 check→执行→R4 record→R3 record→R2→S6→审计→反馈`；
- inv-EX-2：执行前拒绝（R1/R3/R4 check 拒绝）→ 短路返回，**registry.execute_tool 零调用**；
- inv-EX-3：每次工具调用（含被拒绝的）都写审计（HC4，不可关闭）；
- inv-EX-4：R4 去重命中 → 返回 `deduplicated=True` 结果，不重复执行；
- inv-EX-5：对 ToolResult 的一切修改用 `dc_replace` 产出新对象，**不在原对象上就地 mutation**；
- inv-EX-6：审计写入先于反馈收集（反馈挂载不影响已落盘审计）；
- inv-EX-7：`set_hooks` 单次注入，重复注入不生效；
- inv-EX-8：`execute_tools_concurrent` 受 `Semaphore(max_concurrency)` 闸门约束，任一失败 → 整体失败。

> 用例 When 中 `execute_tool(tool_name, args, ctx)` 签名与 max_calls/max_bytes 来源为推断（§6-1/6-2），
> 落码前核对。所有用例先构造：`ex = HarnessExecutor(tool_registry=fake_registry, audit_log=fake_audit)`；
> `ex.set_hooks(recording_hooks)`；`fake_registry` 预置返回 `ToolResult(success=True, data="ok")`。

---

#### 用例 EX-01：全链路 happy path —— 五道关卡全部放行、执行 1 次、审计 1 条、反馈挂载

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EX-1 顺序 + inv-EX-3 审计 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 主路径全关卡（R1/R3 放行、R4 首次、执行、R2 未超限、S6 不 halt） |
| Oracle | golden value（次数） |
| Mock | 是（FakeRegistry + FakeAudit + RecordingHooks），理由：行为层只测编排不测真实工具 |

**等价类划分**：无拒绝触发的常规调用（R1 未超限 / R3 未熔断 / R4 首次 / R2 未超限 / 结果无 halt）
→ 代表值 = `tool_name="write_file", args={"path": "a.txt"}`

**Given**：
- `fake_registry` 预置返回 `ToolResult(success=True, data="ok")`；`RecordingProvider(text="审计完成")` 注入 `_feedback_providers`
- R1 未超限（`rate_limiter.get_count("write_file") < max_calls`）、R3 未注册熔断器、R2 输出远小于上限

**When**：
- `result = await ex.execute_tool("write_file", {"path": "a.txt"}, ctx)`

**Then**：
- 返回值：`result.success is True`、`result.data == "ok"`
- 副作用：`fake_registry.calls == [("write_file", {"path": "a.txt"})]`（执行恰好 1 次）
- 副作用：`len(fake_audit.entries) == 1`，条目含 `tool_name="write_file"`（HC4 留痕）
- 副作用：`result.feedback is not None` 且 text 含"审计完成"（反馈已挂载，10 号 stage 执行）
- 副作用：`recording_hooks.on_tool_output_truncated` 未触发（未超限不触发）

---

#### 用例 EX-02：R1 超限拒绝 —— registry 零调用 + 拒绝结果 + 审计留痕

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-EX-1 顺序 + Risk-EX-2 拒绝留痕 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | R1 拒绝分支（rate 超限短路，不进 R3/R4/执行） |
| Oracle | golden value |
| Mock | 是（FakeRegistry + FakeAudit） |

**等价类划分**：`get_count(tool_name) == max_calls`（恰好超限）→ 代表值 = `max_calls=2, get_count=2`

**Given**：
- `rate_limiter` 已对该 tool 计数 2 次（`max_calls=2`），`fake_registry` 预置返回成功结果（**若被调用将产生副作用**）

**When**：
- `result = await ex.execute_tool("write_file", {"path": "a.txt"}, ctx)`

**Then**：
- 返回值：`result` 为拒绝结果（`success=False` 或含 `reason` 字段，见 §6-2），**非** `data="ok"`
- 副作用：`fake_registry.calls == []`（**零调用**——执行前拒绝不碰真实执行，inv-EX-2）
- 副作用：`len(fake_audit.entries) == 1`（**拒绝也留痕**，inv-EX-3/HC4）

---

#### 用例 EX-03：R3 熔断 OPEN 拒绝 —— registry 零调用 + 审计留痕

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-EX-1 + Risk-EX-2 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | R3 拒绝分支（circuit OPEN 短路） |
| Oracle | golden value |
| Mock | 是（FakeRegistry + FakeAudit） |

**等价类划分**：`circuit_breaker.is_tripped(tool_name) is True`（OPEN 态）→ 代表值 = 先触发 OPEN 再调用

**Given**：
- `ex.register_circuit_breaker("write_file", CircuitBreakerConfig(failure_threshold=1, recovery_timeout=30.0, max_recovery_timeout=300.0))`
- 注入 1 次失败使熔断 OPEN（见 CB-03 手法），`is_circuit_tripped("write_file") is True`
- `fake_registry` 预置返回成功结果（**若被调用将产生副作用**）

**When**：
- `result = await ex.execute_tool("write_file", {"path": "a.txt"}, ctx)`

**Then**：
- 返回值：`result` 为熔断拒绝结果（含熔断语义字段，见 §6-2）
- 副作用：`fake_registry.calls == []`（inv-EX-2）
- 副作用：`len(fake_audit.entries) == 1`（拒绝留痕，inv-EX-3）

---

#### 用例 EX-04：R4 去重命中 —— registry 仅 1 次、第二次 deduplicated=True

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EX-2 + inv-EX-4 + Risk-ID-1 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | R4 check 命中分支（in-flight 重复短路） |
| Oracle | golden value |
| Mock | 是（FakeRegistry + FakeAudit） |

**等价类划分**：同 turn 内同 tool_name + 同 args 二次调用 → 代表值 = 连续两次 `("write_file", {"path": "a.txt"})`

**Given**：
- 首次调用已完成（`idempotency` 已登记该 key）

**When**：
- `r1 = await ex.execute_tool("write_file", {"path": "a.txt"}, ctx)`（已登记的 in-flight/completed 状态见 §6-6）
- `r2 = await ex.execute_tool("write_file", {"path": "a.txt"}, ctx)`

**Then**：
- 返回值：`r1` 为正常执行结果；`r2.deduplicated is True`
- 副作用：`fake_registry.calls` 长度为 **1**（第二次不重复执行，inv-EX-4）
- 副作用：审计 2 条（两次调用各自留痕——去重命中同样写审计）

---

#### 用例 EX-05：R2 截断 —— truncated=True 且序列化后长度 ≤ max_bytes（蜕变关系）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EX-5 dc_replace + Risk-EX-5 缓存污染 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | R2 截断分支（超限 → 截断 → 标记） |
| Oracle | 蜕变关系（输出内容无法预知，断言「长度 ≤ 上限」而非硬编码截断内容） |
| Mock | 是（FakeRegistry + FakeAudit） |

**等价类划分**：输出序列化大小 vs `max_bytes` → 超限代表值 = `data="x" * 100_000, max_bytes=1024`

**Given**：
- `fake_registry` 预置返回 `ToolResult(success=True, data="x" * 100_000)`
- R2 上限配置 `max_bytes=1024`（来源见 §6-2）

**When**：
- `result = await ex.execute_tool("write_file", {"path": "a.txt"}, ctx)`

**Then**：
- 返回值：`result.truncated is True`
- 蜕变关系：`len(json.dumps(result.data)) <= 1024`（截断收敛到上限内，不硬编码截断内容）
- 副作用：`recording_hooks.on_tool_output_truncated` 触发 1 次，参数含 `tool_name="write_file"`、`original_size`、`max_size=1024`
- 副作用：registry 返回的**原对象未被就地改写**（`original.truncated is False`——dc_replace 产出新对象，防 R4 缓存污染，inv-EX-5）

---

#### 用例 EX-06：S6 halt 硬停止 —— halt 标记透传，run 应终止

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-EX-4 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | S6 检查分支（result.halt=True → 硬停止信号） |
| Oracle | golden value（halt 标志透传） |
| Mock | 是（FakeRegistry + FakeAudit） |

**等价类划分**：执行结果 `halt` 标志 ∈ {True, False} → 代表值 = `ToolResult(success=True, data="ok", halt=True)`

**Given**：
- `fake_registry` 预置返回 `ToolResult(success=True, data="ok", halt=True)`（工具显式请求停止）

**When**：
- `result = await ex.execute_tool("write_file", {"path": "a.txt"}, ctx)`

**Then**：
- 返回值：`result.halt is True`（硬停止信号到达调用方，engine 据此终止 run，不等本步其余工具）
- 副作用：审计 1 条（halt 路径同样留痕）
- 副作用：反馈 stage 正常执行不因 halt 短路（halt 是信号不是异常；若实现为短路则标注，见 §6-2）

---

#### 用例 EX-07：审计先于反馈 —— 反馈挂载不污染已落盘审计（含被拒路径）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EX-6 时序 + Risk-EX-6 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 10 号 stage 与 9 号 stage 的时序分支 |
| Oracle | golden value（审计条目内容比对） |
| Mock | 是（FakeRegistry + FakeAudit + RecordingProvider） |

**等价类划分**：有反馈 vs 无反馈 → 代表值 = `RecordingProvider(text="诊断")` + 成功执行

**Given**：
- `RecordingProvider` 注入；`fake_registry` 返回 `ToolResult(success=True, data="ok")`

**When**：
- `await ex.execute_tool("write_file", {"path": "a.txt"}, ctx)`

**Then**：
- 副作用：`fake_audit.entries[0]` 中 **不含** feedback 文本（审计在反馈前落盘，反馈内容不得进入审计——inv-EX-6）
- 副作用：`fake_audit.entries[0]` 含 `tool_name="write_file"` 与 `args`（留痕字段完整）
- 副作用：`result.feedback is not None`（反馈在审计后挂载到返回值，不影响审计）

---

#### 用例 EX-08：set_hooks 单次注入 —— 重复注入不生效

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EX-7 + Risk-EX-7 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | set_hooks 幂等分支（首次生效、二次忽略） |
| Oracle | golden value（hook 归属） |
| Mock | 是（RecordingHooks ×2） |

**等价类划分**：set_hooks 调用次数 ∈ {1, 2} → 代表值 = 连续调用 2 次

**Given**：
- `hooks_a = RecordingHooks()`、`hooks_b = RecordingHooks()`（b 的 `on_tool_output_truncated` 计数器独立）

**When**：
- `ex.set_hooks(hooks_a)`；`ex.set_hooks(hooks_b)`（第二次调用）
- 触发一次截断（复用 EX-05 前置）

**Then**：
- 副作用：`hooks_a.on_tool_output_truncated` 触发 1 次、`hooks_b` 触发 **0** 次（二次注入不生效，inv-EX-7）

---

#### 用例 EX-09：execute_tools_concurrent 并发闸门 —— 同时 in-flight ≤ max_concurrency

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EX-8 + Risk-EX-8 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | Semaphore 闸门分支（并发超过上限被排队） |
| Oracle | golden value（并发峰值计数） |
| Mock | 是（FakeRegistry，慢工具用事件阻塞模拟） |

**等价类划分**：工具数 vs `max_concurrency` → 代表值 = 3 个工具、`max_concurrency=2`

**Given**：
- `fake_registry` 每个调用先 `started` 事件计数、`await asyncio.sleep(0.05)` 再返回（模拟真实耗时）
- `max_concurrency=2`（execute_tools_concurrent 参数，见 §6-2）

**When**：
- `results = await ex.execute_tools_concurrent([(t1, {}), (t2, {}), (t3, {})], ctx, max_concurrency=2)`

**Then**：
- 副作用：并发峰值 `max(started 计数) == 2`（事件计数证明，不用 sleep 猜时序——inv-EX-8）
- 返回值：`len(results) == 3` 且全部成功（闸门排队不丢工具）

---

#### 用例 EX-10：execute_tools_concurrent 任一失败 → 整体失败（唯一权威结果）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-EX-3 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | gather 失败聚合分支 |
| Oracle | golden value（失败结果与因果信息） |
| Mock | 是（FakeRegistry 抛异常模式） |

**等价类划分**：并发工具成败组合 → 代表值 = `[成功, 抛异常, 成功]`

**Given**：
- `fake_registry` 对 `t2` 抛 `RuntimeError("boom")`，其余返回成功

**When**：
- `results = await ex.execute_tools_concurrent([(t1, {}), (t2, {}), (t3, {})], ctx, max_concurrency=2)`

**Then**：
- 返回值：`results` 标记为整体失败（或抛异常——见 §6-2），失败结果作为**唯一权威结果**返回且含因果信息（哪个工具、什么错误）
- 副作用：不允许「t1/t3 成功、t2 失败被静默吞掉」的假成功形态（Risk-EX-3）

---

### 4.2 PermissionGuard —— 决策/门禁（unit，纯函数零 mock）

**不变式**：
- inv-PG-1：`tool_permission is None`（普通工具）→ `"allow"`；
- inv-PG-2：工具要求敏感权限且身份声明了**该权限** → `"allow"`；
- inv-PG-3：工具要求敏感权限但身份未声明 → `"deny"`（物理阻断，无中间态）；
- inv-PG-4：确定性——同输入恒同输出（property 候选：对整类输入 `f(x)==f(x)`）；
- inv-PG-5：身份持有敏感权限**不等于**持有工具要求的那个权限（必须精确匹配）。

> 信任等级（EXTERNAL/SUB_AGENT/ORCHESTRATOR）不在 `check_permission` 三参数签名内，属身份层输入
> （ToolContext.trust_level）。本层以 `sensitive_permissions` 集合差异间接反映信任等级差异
> （低信任身份声明空集/小子集 → deny）。见 §6-4。

---

#### 用例 PG-01：普通工具（不要求敏感权限）→ allow

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-PG-1 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：`tool_permission is None` → allow |
| Oracle | golden value（决策枚举） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：`tool_permission` ∈ {None, 某权限} × `sensitive_permissions` 任意 → 代表值 = `frozenset(), SensitivityLevel.LOW, None`

**Given**：
- 无前置，直接调用

**When**：
- `PermissionGuard().check_permission(frozenset(), SensitivityLevel.LOW, None)`

**Then**：
- 返回值 = `"allow"`
- 副作用：无副作用，仅验证返回值

---

#### 用例 PG-02：身份声明了工具要求的敏感权限 → allow

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-PG-2 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：权限匹配 → allow |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：身份权限集 vs 工具权限的包含关系 → 代表值 = `frozenset({SensitivePermission.DELETE}), tool_permission=DELETE`

**Given**：
- 无前置

**When**：
- `PermissionGuard().check_permission(frozenset({SensitivePermission.DELETE}), SensitivityLevel.HIGH, SensitivePermission.DELETE)`

**Then**：
- 返回值 = `"allow"`
- 副作用：无副作用

---

#### 用例 PG-03：身份未声明所需敏感权限（含空集合）→ deny + 权限拒绝 warning

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-PG-3 + Risk-PG-1 [P0] |
| 测试层级 | unit |
| 覆盖准则 | 分支：权限缺失 → deny |
| Oracle | golden value + caplog（次要） |
| Mock | 否 |

**等价类划分**：身份权限集 ∈ {空集, 不含目标权限的非空集} → 代表值 = `frozenset()`（空集，覆盖旧 mock「空 sensitive_permissions → warning」）

**Given**：
- 无前置

**When**：
- `PermissionGuard().check_permission(frozenset(), SensitivityLevel.HIGH, SensitivePermission.DELETE)`

**Then**：
- 返回值 = `"deny"`（物理阻断，无「提醒一下继续」中间态——inv-PG-3）
- 副作用：caplog 含 warning（权限拒绝留痕，旧 mock 覆盖点，次要断言）

---

#### 用例 PG-04：持有其他敏感权限 ≠ 持有工具要求的权限 → deny（精确匹配）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-PG-5 + Risk-PG-1 + inv-PG-4 确定性 [P0] |
| 测试层级 | unit |
| 覆盖准则 | 分支：权限存在但不匹配 → deny（防「有权限就放行」的宽松匹配 bug） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：身份权限集与工具权限「部分重叠/无关」→ 代表值 = `frozenset({SensitivePermission.SEND_EMAIL}), tool_permission=DELETE`（完全无关）

**Given**：
- 无前置

**When**：
- `d1 = PermissionGuard().check_permission(frozenset({SensitivePermission.SEND_EMAIL}), SensitivityLevel.HIGH, SensitivePermission.DELETE)`
- `d2 = PermissionGuard().check_permission(frozenset({SensitivePermission.SEND_EMAIL}), SensitivityLevel.HIGH, SensitivePermission.DELETE)`

**Then**：
- 返回值：`d1 == d2 == "deny"`（精确匹配拒绝 + 确定性，inv-PG-4/5）
- 副作用：无副作用

---

### 4.3 HITLController —— 审批决策/门禁（unit，纯决策零 mock，fail-closed）

**不变式**：
- inv-HITL-1：CRITICAL 强制 `need_approval`，**`auto_confirm_high` 永不触及 CRITICAL**（HC6）；
- inv-HITL-2：HIGH 且 `auto_confirm_high=True` → `"pass"`；否则 `"need_approval"`；
- inv-HITL-3：MEDIUM/LOW → `"pass"`；
- inv-HITL-4：`resolve_resume("approved", pending)` → `execute_pending`（执行 pending 中保存的调用）；
- inv-HITL-5：`resolve_resume("rejected", pending)` → `reject_and_halt`（终止 run）；
- inv-HITL-6（fail-closed）：未知/非法 hitl_decision → 拒绝路径（不执行 pending），见 §6-7。

---

#### 用例 HITL-01：CRITICAL 强制审批 —— auto_confirm_high=True 也不能放行（HC6）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-HITL-1 + Risk-HITL-1 [P0] |
| 测试层级 | unit |
| 覆盖准则 | 分支：sensitivity=CRITICAL → need_approval（无论 auto_confirm_high） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：sensitivity ∈ {CRITICAL} × auto_confirm_high ∈ {True, False} → 代表值 = `auto_confirm_high=True`

**Given**：
- `HITLController(auto_confirm_high=True)`

**When**：
- `HITLController(auto_confirm_high=True).check_approval(SensitivityLevel.CRITICAL.value, "delete_all")`

**Then**：
- 返回值 = `"need_approval"`（CRITICAL 不可绕过，auto_confirm_high 只对 HIGH 生效——HC6 底线）
- 副作用：无副作用

---

#### 用例 HITL-02：HIGH 无 auto_confirm → need_approval

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-HITL-2 [P0] |
| 测试层级 | unit |
| 覆盖准则 | 分支：HIGH × auto_confirm_high=False → need_approval |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：HIGH × auto_confirm_high ∈ {False} → 代表值 = 默认 `auto_confirm_high=False`

**Given**：
- `HITLController(auto_confirm_high=False)`（默认）

**When**：
- `ctrl.check_approval(SensitivityLevel.HIGH.value, "send_email")`

**Then**：
- 返回值 = `"need_approval"`
- 副作用：无副作用

---

#### 用例 HITL-03：HIGH + auto_confirm_high=True → pass

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-HITL-2 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：HIGH × auto_confirm_high=True → pass |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：HIGH × auto_confirm_high ∈ {True} → 代表值 = `True`

**Given**：
- `HITLController(auto_confirm_high=True)`

**When**：
- `ctrl.check_approval(SensitivityLevel.HIGH.value, "send_email")`

**Then**：
- 返回值 = `"pass"`
- 副作用：无副作用

---

#### 用例 HITL-04：MEDIUM/LOW → pass

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-HITL-3 [P2] |
| 测试层级 | unit |
| 覆盖准则 | 分支：MEDIUM/LOW → pass（低风险放行） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：sensitivity ∈ {MEDIUM, LOW} → 代表值 = `SensitivityLevel.LOW`

**Given**：
- `HITLController(auto_confirm_high=False)`

**When**：
- `ctrl.check_approval(SensitivityLevel.LOW.value, "read_file")`

**Then**：
- 返回值 = `"pass"`
- 副作用：无副作用

---

#### 用例 HITL-05：resolve_resume(approved) → execute_pending

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-HITL-4 [P0] |
| 测试层级 | unit |
| 覆盖准则 | 分支：approved → execute_pending |
| Oracle | golden value（ResumeDecision 字段） |
| Mock | 否 |

**等价类划分**：hitl_decision ∈ {approved, rejected} × pending → 代表值 = `"approved"` + `PendingApproval`（含 tool_name/args）

**Given**：
- `pending = PendingApproval(tool_name="send_email", args={...}, ...)`（frozen dataclass，字段见 §6-7）

**When**：
- `decision = HITLController().resolve_resume("approved", pending)`

**Then**：
- 返回值：`decision` 为 `ResumeDecision` 且 action = `execute_pending`（执行 pending 中保存的工具调用）
- 副作用：无副作用（纯决策，不实际执行）

---

#### 用例 HITL-06：resolve_resume(rejected) → reject_and_halt（fail-closed 拒绝）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-HITL-5 + Risk-HITL-2 [P0] |
| 测试层级 | unit |
| 覆盖准则 | 分支：rejected → reject_and_halt（终止 run，不执行 pending） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：hitl_decision ∈ {rejected} → 代表值 = `"rejected"`

**Given**：
- `pending = PendingApproval(tool_name="send_email", args={...}, ...)`

**When**：
- `decision = HITLController().resolve_resume("rejected", pending)`

**Then**：
- 返回值：`decision` action = `reject_and_halt`（终止 run，pending 中的调用**绝不执行**——fail-closed）
- 副作用：无副作用

---

### 4.4 CircuitBreakerManager —— 状态机全路径（component(fake)，真实状态机 + Fake hooks）

**状态转换表**（覆盖准则：0-switch 转换覆盖 + 非法/边界）：

| 当前状态 | 事件 | 期望 |
|----------|------|------|
| （未注册） | check | 放行（None） |
| CLOSED | record_failure × n < threshold | 保持 CLOSED，check 放行 |
| CLOSED | record_failure × n == threshold | → OPEN（hook: on_tool_circuit_open） |
| CLOSED | record_success | 计数复位，不触发 hook |
| OPEN | check | 拒绝（不执行） |
| OPEN | recovery_timeout 到期 | → HALF_OPEN（探活一次） |
| HALF_OPEN | check/探活成功 | → CLOSED（hook: on_tool_circuit_close） |
| HALF_OPEN | 探活失败 | → OPEN + recovery_timeout × 2（指数退避） |
| OPEN | 退避达 max_recovery_timeout | 不再探活（永久 OPEN） |

**不变式**：
- inv-CB-1：未注册熔断器的工具 check 直接放行；
- inv-CB-2：CLOSED 失败计数达 threshold 才 OPEN（阈值判定精确）；
- inv-CB-3：OPEN 期间 check 拒绝（熔断生效）；
- inv-CB-4：HALF_OPEN 只探活一次，成功回 CLOSED、失败回 OPEN 且退避翻倍；
- inv-CB-5：退避达 `max_recovery_timeout` → 永久 OPEN（不再探活）；
- inv-CB-6：hook 触发次数与参数精确（`on_tool_circuit_open(tool_name, failure_count, recovery_timeout)`、`on_tool_circuit_close`），CLOSED 成功不触发。

> 时钟策略：`recovery_timeout=0.02s`、`max_recovery_timeout=0.5s`（退避档位 0.02/0.04/0.08/.../0.5），
> 每次等待 `asyncio.sleep(0.06)`（≥2 倍量级差，杜绝竞态）。若支持注入时钟则优先（§6-3）。

---

#### 用例 CB-01：未注册熔断器的工具 → check 放行

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CB-1 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 状态表：未注册行 |
| Oracle | golden value |
| Mock | 是（RecordingHooks——断言不触发） |

**等价类划分**：工具是否注册 → 代表值 = `"unregistered_tool"` 未注册

**Given**：
- `mgr = CircuitBreakerManager()`，未注册任何熔断器

**When**：
- `result = mgr.check("unregistered_tool")`

**Then**：
- 返回值：`result is None`（放行——熔断只约束显式配置的工具）
- 副作用：hooks 均未触发

---

#### 用例 CB-02：失败未达 threshold → 保持 CLOSED 放行

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CB-2 + Risk-CB-1 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 状态表：CLOSED × 失败 < threshold |
| Oracle | golden value |
| Mock | 是（RecordingHooks） |

**等价类划分**：失败次数 vs threshold → 代表值 = `threshold=2`、失败 1 次（边界下界）

**Given**：
- `mgr.register("tool_a", CircuitBreakerConfig(failure_threshold=2, recovery_timeout=0.02, max_recovery_timeout=0.5))`
- `mgr.record_failure("tool_a")` × 1

**When**：
- `result = mgr.check("tool_a")`

**Then**：
- 返回值：`result is None`（1 次失败未达阈值，仍放行——不误杀）
- 副作用：`hooks.on_tool_circuit_open` 未触发（未 OPEN 不触发，旧 mock 覆盖点）

---

#### 用例 CB-03：恰好达 threshold → OPEN + hook 恰好 1 次（参数精确）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CB-2 + Risk-CB-1 + Risk-CB-5 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 状态表：CLOSED × 失败 == threshold → OPEN（边界精确） |
| Oracle | golden value（hook 参数） |
| Mock | 是（RecordingHooks） |

**等价类划分**：失败次数 == threshold（边界）→ 代表值 = `threshold=2`、失败恰好 2 次

**Given**：
- `mgr.register("tool_a", CircuitBreakerConfig(failure_threshold=2, recovery_timeout=0.02, max_recovery_timeout=0.5))`
- `mgr.record_failure("tool_a")` × 2

**When**：
- `mgr.record_failure("tool_a")`（第 2 次触发 OPEN 的瞬间）
- `tripped = mgr.is_tripped("tool_a")`

**Then**：
- 返回值：`tripped is True`（OPEN）
- 副作用：`hooks.on_tool_circuit_open` 触发**恰好 1 次**，参数 `tool_name="tool_a"`、`failure_count=2`、`recovery_timeout=0.02`（hook 参数精确——旧 mock 覆盖点）

---

#### 用例 CB-04：OPEN 期间 check → 拒绝

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CB-3 + Risk-CB-2 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 状态表：OPEN × check → 拒绝 |
| Oracle | golden value（拒绝 ToolResult 语义） |
| Mock | 是（RecordingHooks） |

**等价类划分**：熔断状态 ∈ {OPEN} → 代表值 = 先触发 OPEN（复用 CB-03 前置）

**Given**：
- 熔断已 OPEN（`failure_threshold=1`，1 次失败即 OPEN）

**When**：
- `result = mgr.check("tool_a")`

**Then**：
- 返回值：`result` 为拒绝结果（OPEN 拒绝语义字段，见 §6-2），**非** None 放行
- 副作用：无 hook 触发（OPEN 拒绝不是状态迁移）

---

#### 用例 CB-05：recovery_timeout 到期 → HALF_OPEN 探活成功 → CLOSED + close hook

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CB-4 + Risk-CB-3 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 状态表：OPEN →(超时) HALF_OPEN →(成功) CLOSED |
| Oracle | golden value |
| Mock | 是（RecordingHooks） |

**等价类划分**：探活结果 ∈ {成功} → 代表值 = `record_success` 在 HALF_OPEN 期间调用

**Given**：
- `mgr.register("tool_a", CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.02, max_recovery_timeout=0.5))`
- 触发 OPEN（`record_failure` × 1），`is_tripped is True`
- `await asyncio.sleep(0.06)`（> recovery_timeout，进入 HALF_OPEN）

**When**：
- `mgr.record_success("tool_a")`（HALF_OPEN 探活成功）
- `tripped = mgr.is_tripped("tool_a")`

**Then**：
- 返回值：`tripped is False`（复位 CLOSED）
- 副作用：`hooks.on_tool_circuit_close` 触发 1 次，参数含 `tool_name="tool_a"`（旧 mock 覆盖点）

---

#### 用例 CB-06：HALF_OPEN 探活失败 → 回 OPEN + 退避翻倍

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CB-4 + Risk-CB-4 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 状态表：HALF_OPEN × 失败 → OPEN（退避 ×2） |
| Oracle | golden value（第二次 OPEN 的 recovery_timeout == 2× 首次） |
| Mock | 是（RecordingHooks） |

**等价类划分**：探活结果 ∈ {失败} → 代表值 = `record_failure` 在 HALF_OPEN 期间调用

**Given**：
- `mgr.register("tool_a", CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.02, max_recovery_timeout=0.5))`
- 触发 OPEN（首次 `recovery_timeout=0.02`），`await asyncio.sleep(0.06)` 进入 HALF_OPEN

**When**：
- `mgr.record_failure("tool_a")`（HALF_OPEN 探活失败）
- 记录第二次 OPEN 的 recovery_timeout：`hooks.on_tool_circuit_open` 第 2 次调用参数

**Then**：
- 返回值：`is_tripped("tool_a") is True`（回 OPEN）
- 副作用：`hooks.on_tool_circuit_open` 第 2 次触发，`recovery_timeout` 参数 = `0.04`（**退避翻倍**——不翻倍则是恢复风暴，Risk-CB-4）

---

#### 用例 CB-07：退避达 max_recovery_timeout → 永久 OPEN（不再探活）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CB-5 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 状态表：退避达上限 → 永久 OPEN |
| Oracle | golden value（超时后仍 tripped） |
| Mock | 是（RecordingHooks） |

**等价类划分**：退避档位 vs 上限 → 代表值 = `recovery_timeout=0.02, max_recovery_timeout=0.05`（2 档即触顶）

**Given**：
- `mgr.register("tool_a", CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.02, max_recovery_timeout=0.05))`
- 第 1 轮：OPEN → sleep(0.06) → HALF_OPEN 失败 → OPEN（退避 0.04）
- 第 2 轮：sleep(0.06) → 探活（0.04 < 0.05 仍探）失败 → OPEN（退避 0.08 > 0.05，达上限）

**When**：
- `await asyncio.sleep(0.06)`（即使超过 0.05 上限）
- `mgr.check("tool_a")` / `is_tripped("tool_a")`

**Then**：
- 返回值：`is_tripped("tool_a") is True` 且 check 仍拒绝（达上限后不再探活，永久 OPEN——inv-CB-5）

---

#### 用例 CB-08：CLOSED 成功 record_success → 不触发 hook、计数复位

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CB-6 + Risk-CB-5 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 状态表：CLOSED × success（旧 mock「CLOSED 成功不触发」） |
| Oracle | golden value |
| Mock | 是（RecordingHooks） |

**等价类划分**：CLOSED 态成功次数任意 → 代表值 = 失败 1 次（threshold=2）后成功 1 次

**Given**：
- `mgr.register("tool_a", CircuitBreakerConfig(failure_threshold=2, recovery_timeout=0.02, max_recovery_timeout=0.5))`
- `record_failure` × 1（计数=1，未 OPEN）

**When**：
- `mgr.record_success("tool_a")` × 1（成功复位计数）

**Then**：
- 副作用：`hooks.on_tool_circuit_open` / `on_tool_circuit_close` 均**未触发**（CLOSED 内成功不触发 hook）
- 语义：计数复位——随后再失败 2 次仍按 threshold=2 判定（可加断言：`record_failure`×2 后 `is_tripped is True`，证明计数确已复位而非累加）

---

### 4.5 IdempotencyGuard —— turn 级去重与并发安全（component(fake)）

**不变式**：
- inv-ID-1：key = `sha256(f"{tool_name}:{canonical_args}")`，由 `_make_key(tool_name, args)` 生成，确定性；
- inv-ID-2：check 命中 in-flight → 返回重复结果（`deduplicated=True`），不执行；未命中 → 登记 in-flight 放行；
- inv-ID-3：record 后 in-flight → completed（completed 是否参与 check 见 §6-6）；
- inv-ID-4：`reset_turn()` 清空全部状态；
- inv-ID-5：`_locks` 无泄漏——check 后非空、`reset_turn()` 后清空；
- inv-ID-6：async check 与 sync store 共享同一缓存（`_cache`）；
- inv-ID-7：`asyncio.Lock` 保证并发 check 同一 key 时只有一个真正执行。

---

#### 用例 ID-01：首次 check 未命中 → 放行并登记

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ID-2 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：key 不在 in-flight → 放行 |
| Oracle | golden value |
| Mock | 是（Fake hooks 记录器，可选） |

**等价类划分**：key ∈ {未登记} → 代表值 = `("write_file", {"path": "a.txt"})`

**Given**：
- `guard = IdempotencyGuard()`

**When**：
- `result = await guard.check("write_file", {"path": "a.txt"})`

**Then**：
- 返回值：`result is None`（或通行语义，见 §6-6——未命中可执行）
- 副作用：该 key 已登记（随后同 key check 命中，见 ID-02）

---

#### 用例 ID-02：in-flight 命中 → deduplicated=True，不执行

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ID-2 + Risk-ID-1 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：key 在 in-flight → 重复结果 |
| Oracle | golden value |
| Mock | 是 |

**等价类划分**：同 key 二次调用（in-flight 中）→ 代表值 = 连续两次 `("write_file", {"path": "a.txt"})`

**Given**：
- `guard = IdempotencyGuard()`；`await guard.check("write_file", {"path": "a.txt"})`（已登记 in-flight，未 record）

**When**：
- `result = await guard.check("write_file", {"path": "a.txt"})`

**Then**：
- 返回值：`result.deduplicated is True`（同一步内重复调用被去重，副作用只发生一次——Risk-ID-1）

---

#### 用例 ID-03：不同 args → 不同 key，不误判为重复

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ID-1 + Risk-ID-4 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：args 不同 → key 不同 |
| Oracle | golden value |
| Mock | 是 |

**等价类划分**：args 差异 ∈ {完全无关, 仅值不同} → 代表值 = `{"path": "a.txt"}` vs `{"path": "b.txt"}`

**Given**：
- `await guard.check("write_file", {"path": "a.txt"})`

**When**：
- `result = await guard.check("write_file", {"path": "b.txt"})`

**Then**：
- 返回值：`result is None`（不同参数不误判重复——不同调用照常执行）
- 副作用：两次调用 registry 各执行 1 次（去重粒度到参数）

---

#### 用例 ID-04：_make_key 确定性 + 参数参与

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ID-1 + Risk-ID-4 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：key 生成规则 |
| Oracle | golden value（key 相等/不相等关系） |
| Mock | 是 |

**等价类划分**：同参多次 vs 异参 → 代表值 = 同 `("t", {"a": 1})` 两次 + 异参 `("t2", {"a": 1})`

**Given**：
- `guard = IdempotencyGuard()`

**When**：
- `k1 = guard._make_key("t", {"a": 1})`；`k2 = guard._make_key("t", {"a": 1})`；`k3 = guard._make_key("t2", {"a": 1})`

**Then**：
- 返回值：`k1 == k2`（确定性）；`k1 != k3`（tool_name 参与 key）
- 副作用：无（纯计算）

---

#### 用例 ID-05：reset_turn 清空 → 同 key 再次可执行

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ID-4 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：turn 边界（跨轮状态清零） |
| Oracle | golden value |
| Mock | 是 |

**等价类划分**：turn ∈ {同 turn, 跨 turn} → 代表值 = 同 key 跨 `reset_turn()`

**Given**：
- `await guard.check("write_file", {"path": "a.txt"})`（已登记）

**When**：
- `guard.reset_turn()`
- `result = await guard.check("write_file", {"path": "a.txt"})`

**Then**：
- 返回值：`result is None`（新 turn 可重新执行——与 R1 同节奏，run_core 每轮 Phase 1 调用）

---

#### 用例 ID-06：_locks 无泄漏 —— check 后非空、reset_turn 后清空

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ID-5 + Risk-ID-2 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：lock 生命周期 |
| Oracle | golden value（_locks 长度） |
| Mock | 是 |

**等价类划分**：生命周期阶段 ∈ {check 后, reset_turn 后} → 代表值 = 1 个 key

**Given**：
- `guard = IdempotencyGuard()`

**When**：
- `await guard.check("write_file", {"path": "a.txt"})`（触发 lock 创建）

**Then**：
- 副作用：`len(guard._locks) == 1`（check 后非空——lock 已创建）
- 副作用：`guard.reset_turn()` 后 `len(guard._locks) == 0`（**无泄漏**——否则后续调用死锁，Risk-ID-2，旧 mock 覆盖点）

---

#### 用例 ID-07：async check 与 sync store 共享缓存

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ID-6 + Risk-ID-3 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：async/sync 接口共享 `_cache` |
| Oracle | golden value（同一 key 互见） |
| Mock | 是 |

**等价类划分**：写入端 ∈ {async, sync} → 代表值 = `store_sync` 写入、`async check` 读取

**Given**：
- `guard = IdempotencyGuard()`；`guard.store_sync("write_file", {"path": "a.txt"}, cached_result)`（sync 写入）

**When**：
- `result = await guard.check("write_file", {"path": "a.txt"})`

**Then**：
- 返回值：`result` 命中 sync 写入的缓存（async/sync 共享 `_cache`，不各自为政——Risk-ID-3，旧 mock 覆盖点）

---

#### 用例 ID-08：并发 check 同一 key → 仅 1 次真正执行（lock 生效）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ID-7 + Risk-ID-1 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：并发同 key（gather 聚合） |
| Oracle | golden value（执行计数） |
| Mock | 是（计数型 Fake 执行器） |

**等价类划分**：并发请求数 vs key 数 → 代表值 = 5 个并发请求、同一 key

**Given**：
- `guard = IdempotencyGuard()`；`executed = []`（list 追加计数）

**When**：
- `results = await asyncio.gather(*[guard.check_and_execute("write_file", {"path": "a.txt"}, executed) for _ in range(5)])`（check_and_execute 为测试辅助：check → 未命中则执行并 record，见 §6-6）

**Then**：
- 副作用：`len(executed) == 1`（并发同 key 只有一个真正执行，其余 deduplicated——asyncio.Lock 并发安全，Risk-ID-1）
- 返回值：`sum(r.deduplicated for r in results) == 4`（4 条重复、1 条真实）

---

### 4.6 OutputGuard —— 截断 + hooks（component(fake)）

**不变式**：
- inv-OG-1：超限 → 截断 + `truncated=True`，且截断后序列化长度 ≤ max_bytes（二分收敛）；
- inv-OG-2：未超限 → 原对象原样返回（`is` 同一对象），不触发 hook；
- inv-OG-3：每次截断触发一次 `on_tool_output_truncated(tool_name, original_size, max_size)`；
- inv-OG-4：无 hooks 时降级（仍截断）+ warning；
- inv-OG-5：`data=None` → 不截断、不触发、不崩溃。

---

#### 用例 OG-01：超限截断 —— 长度 ≤ max_bytes + truncated 标记

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-OG-1 + Risk-OG-1 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：序列化大小 > max_bytes → 截断 |
| Oracle | 蜕变关系（长度 ≤ 上限；不硬编码截断内容） |
| Mock | 是（RecordingHooks） |

**等价类划分**：输出大小 vs max_bytes → 代表值 = `data="x" * 100_000, max_bytes=1024`

**Given**：
- `guard = OutputGuard()`；`guard.set_hooks(hooks)`；`result = ToolResult(success=True, data="x" * 100_000)`

**When**：
- `out = guard.check(result, max_bytes=1024)`

**Then**：
- 返回值：`out.truncated is True`
- 蜕变关系：`len(json.dumps(out.data)) <= 1024`（二分定位截断点收敛到上限内）
- 副作用：`hooks.on_tool_output_truncated` 触发 1 次，参数 `tool_name` / `original_size` / `max_size=1024`

---

#### 用例 OG-02：未超限 → 原样返回 + 不触发 hook

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-OG-2 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：未超限 → 不截断 |
| Oracle | golden value（对象同一性） |
| Mock | 是（RecordingHooks） |

**等价类划分**：输出大小 < max_bytes → 代表值 = `data="ok", max_bytes=1024`

**Given**：
- `guard = OutputGuard()`；`guard.set_hooks(hooks)`；`original = ToolResult(success=True, data="ok")`

**When**：
- `out = guard.check(original, max_bytes=1024)`

**Then**：
- 返回值：`out is original`（未超限零拷贝返回）；`out.truncated is False`
- 副作用：`hooks.on_tool_output_truncated` 未触发（旧 mock 覆盖点）

---

#### 用例 OG-03：hook 参数精确 + 3 次截断触发 3 次

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-OG-3 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：触发次数与参数 |
| Oracle | golden value（调用记录） |
| Mock | 是（RecordingHooks） |

**等价类划分**：截断次数 ∈ {3} → 代表值 = 同一 result 连续 check 3 次

**Given**：
- `guard = OutputGuard()`；`guard.set_hooks(hooks)`；超大 result（同 OG-01）

**When**：
- 连续 `guard.check(result, max_bytes=1024)` × 3

**Then**：
- 副作用：`hooks.on_tool_output_truncated` 触发 **3** 次（每次截断各触发 1 次，旧 mock 覆盖点）
- 副作用：每次参数均含 `tool_name` / `original_size` / `max_size=1024`

---

#### 用例 OG-04：无 hooks → 降级（仍截断）+ warning

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-OG-4 + Risk-OG-2 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：hooks 未设置 → 降级路径 |
| Oracle | golden value + caplog（次要） |
| Mock | 是 |

**等价类划分**：hooks ∈ {未设置} → 代表值 = 不调用 `set_hooks`

**Given**：
- `guard = OutputGuard()`（未设 hooks）；超大 result

**When**：
- `out = guard.check(result, max_bytes=1024)`

**Then**：
- 返回值：`out.truncated is True` 且长度 ≤ 1024（**截断不因无 hooks 而失效**——降级正确，Risk-OG-2）
- 副作用：caplog 含 warning（无 hooks 提示，旧 mock 覆盖点，次要断言）

---

#### 用例 OG-05：data=None → 不截断、不触发、不崩溃

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-OG-5 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：data=None |
| Oracle | golden value |
| Mock | 是（RecordingHooks） |

**等价类划分**：data ∈ {None} → 代表值 = `ToolResult(success=False, data=None, error="tool error")`

**Given**：
- `guard = OutputGuard()`；`guard.set_hooks(hooks)`；`result = ToolResult(success=False, data=None, error="tool error")`

**When**：
- `out = guard.check(result, max_bytes=1024)`

**Then**：
- 返回值：`out is result`（原样返回，不截断）
- 副作用：`hooks.on_tool_output_truncated` 未触发（data=None 不触发，旧 mock 覆盖点）

---

### 4.7 ContextWindowBudget —— ratio 校验 + 冻结 + slot 计算（unit，零 mock）

**不变式**：
- inv-CWB-1：各 ratio ∈ [0, 1.0] 且**四者之和 ≤ 1.0**，超限抛 `BehaviorConfigError`；
- inv-CWB-2：构造后完全冻结（`__setattr__` 抛异常）；
- inv-CWB-3：`get_slot_tokens(slot) = floor(ratio × context_window)`（128K 默认：19200/12800/64000/12800）；
- inv-CWB-4：`build_slot_snapshot()` 一次性返回全部 slot 配额；
- inv-CWB-5：未显式传 context_window → 默认值 + warning。

---

#### 用例 CWB-01：默认比值计算正确（128K → floor 取整配额）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CWB-3 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：默认 ratio 路径 |
| Oracle | golden value（floor 可手算：0.15×131072=19660.8→19660；见 §6-8） |
| Mock | 否 |

**等价类划分**：context_window ∈ {128K 显式传入} → 代表值 = `context_window=131072`

**Given**：
- `budget = ContextWindowBudget(context_window=131072)`（显式传入，见 §6-8）

**When**：
- `t = budget.get_slot_tokens("system_prompt")`

**Then**：
- 返回值：`t == math.floor(0.15 * 131072)`（floor 取整，不四舍五入——inv-CWB-3）
- 副作用：无副作用

---

#### 用例 CWB-02：ratio 之和 > 1.0 → BehaviorConfigError（fail-fast）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CWB-1 + Risk-CWB-1 [P0] |
| 测试层级 | unit |
| 覆盖准则 | 分支：sum > 1.0 → 抛异常 |
| Oracle | golden value（异常类型） |
| Mock | 否 |

**等价类划分**：sum ∈ {>1.0} → 代表值 = `[0.5, 0.3, 0.2, 0.1]`（sum=1.1）

**Given**：
- 构造参数：`system_prompt_ratio=0.5, tool_schema_ratio=0.3, conversation_ratio=0.2, recall_ratio=0.1`（sum=1.1）

**When**：
- `ContextWindowBudget(context_window=131072, system_prompt_ratio=0.5, tool_schema_ratio=0.3, conversation_ratio=0.2, recall_ratio=0.1)`

**Then**：
- 抛 `BehaviorConfigError`（构造期 fail-fast，各 slot 配额不得叠加超窗口——Risk-CWB-1）

---

#### 用例 CWB-03：单个 ratio 越界（>1.0 或 <0）→ 报错

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CWB-1 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：ratio 超出 [0, 1.0] |
| Oracle | golden value（异常类型） |
| Mock | 否 |

**等价类划分**：越界方向 ∈ {>1.0, <0} → 代表值 = `conversation_ratio=1.5`（>1.0 侧）

**Given**：
- 构造参数：`conversation_ratio=1.5`，其余默认

**When**：
- `ContextWindowBudget(context_window=131072, conversation_ratio=1.5)`

**Then**：
- 抛 `BehaviorConfigError`（单 ratio 越界同样 fail-fast）

---

#### 用例 CWB-04：冻结 —— 修改任何 slot 比例 → 抛异常

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CWB-2 + Risk-CWB-2 [P2] |
| 测试层级 | unit |
| 覆盖准则 | 分支：__setattr__ 冻结路径 |
| Oracle | golden value（异常类型） |
| Mock | 否 |

**等价类划分**：字段 ∈ {system_prompt_ratio, ...} → 代表值 = `system_prompt_ratio`

**Given**：
- `budget = ContextWindowBudget(context_window=131072)`

**When**：
- `budget.system_prompt_ratio = 0.9`

**Then**：
- 抛异常（冻结——配额声明后不可运行期篡改，inv-CWB-2；旧 mock「修改冻结字段触发 warning」升级为硬拒绝，见 §6-8）

---

#### 用例 CWB-05：build_slot_snapshot 完整 + 未显式传 context_window → warning

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-CWB-4 + inv-CWB-5 + Risk-CWB-3 [P2] |
| 测试层级 | unit |
| 覆盖准则 | 分支：快照路径 + 默认值路径 |
| Oracle | golden value + caplog |
| Mock | 否 |

**等价类划分**：context_window ∈ {未传} → 代表值 = 不传该参数（用默认值）

**Given**：
- `budget = ContextWindowBudget()`（未传 context_window）

**When**：
- `snapshot = budget.build_slot_snapshot()`

**Then**：
- 返回值：`snapshot` 含全部 4 个 slot 且各自配额 == `floor(ratio × 默认窗口)`（快照一次性可读，inv-CWB-4）
- 副作用：caplog 含 warning（未显式传窗口时 WARNING 留痕，用户知情——Risk-CWB-3，旧 mock 覆盖点）

---

### 4.8 ExecutionLimits / ErrorPolicy —— 冻结 + 指数退避边界（unit，零 mock）

**不变式**：
- inv-EL-1：默认值 `max_steps=30` / `step_timeout=120.0` / `total_timeout=600.0`；
- inv-EL-2：`step_timeout ≤ total_timeout`（倒挂报错）；max_steps 正整数；各值 > 0；
- inv-EL-3：`__setattr__` / `__delattr__` 抛 `PermissionError`（HC5 完全冻结）；
- inv-EP-1：`calculate_delay(attempt) = min(base_delay_s × 2^attempt, max_delay_s)`；
- inv-EP-2：ErrorPolicy 冻结。

---

#### 用例 EL-01：默认值正确

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EL-1 [P3] |
| 测试层级 | unit |
| 覆盖准则 | 分支：默认构造 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：构造参数 ∈ {全部默认} → 代表值 = `ExecutionLimits()`

**Given**：
- 无前置

**When**：
- `limits = ExecutionLimits()`

**Then**：
- 返回值：`limits.max_steps == 30`、`limits.step_timeout == 120.0`、`limits.total_timeout == 600.0`
- 副作用：无副作用

---

#### 用例 EL-02：step_timeout > total_timeout 倒挂 → 报错

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EL-2 + Risk-EL-1 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：step_timeout > total_timeout → 拒绝 |
| Oracle | golden value（异常类型） |
| Mock | 否 |

**等价类划分**：step_timeout vs total_timeout ∈ {倒挂} → 代表值 = `step_timeout=600.0, total_timeout=120.0`

**Given**：
- 构造参数 `ExecutionLimits(max_steps=30, step_timeout=600.0, total_timeout=120.0)`

**When**：
- 构造

**Then**：
- 抛异常（单步超时大于总超时是配置倒挂——Risk-EL-1，构造期 fail-fast）

---

#### 用例 EL-03：冻结 —— setattr 与 delattr 均拒绝

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EL-3 [P2] |
| 测试层级 | unit |
| 覆盖准则 | 分支：__setattr__ / __delattr__ 冻结 |
| Oracle | golden value（PermissionError） |
| Mock | 否 |

**等价类划分**：操作 ∈ {setattr, delattr} → 代表值 = 两者都试

**Given**：
- `limits = ExecutionLimits()`

**When**：
- `limits.max_steps = 100`（setattr）
- `del limits.max_steps`（delattr）

**Then**：
- 两者均抛 `PermissionError`（HC5 完全冻结——边界声明后不可被运行期篡改）

---

#### 用例 EP-01：退避公式 —— attempt=0..2 → 1.0/2.0/4.0

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EP-1 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：未封顶区（2^n < max/base） |
| Oracle | golden value（可手算） |
| Mock | 否 |

**等价类划分**：attempt ∈ {0, 1, 2}（未触及封顶）→ 代表值 = 三个值各算一次

**Given**：
- `policy = ErrorPolicy(max_retries=3, base_delay_s=1.0, max_delay_s=30.0)`（默认）

**When**：
- `policy.calculate_delay(0)`、`policy.calculate_delay(1)`、`policy.calculate_delay(2)`

**Then**：
- 返回值：`1.0`、`2.0`、`4.0`（`min(1.0×2^n, 30.0)` 指数退避精确）
- 副作用：无副作用

---

#### 用例 EP-02：退避封顶 —— 大 attempt → max_delay

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EP-1 + Risk-EP-1 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：封顶区（2^n ≥ max/base） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：attempt ∈ {封顶区} → 代表值 = `attempt=6`（2^6=64 > 30）

**Given**：
- `policy = ErrorPolicy(max_retries=3, base_delay_s=1.0, max_delay_s=30.0)`

**When**：
- `policy.calculate_delay(6)`

**Then**：
- 返回值：`30.0`（封顶不无界增长——Risk-EP-1）
- 边界：`calculate_delay(5) == 30.0`（2^5=32>30，同样封顶）；`calculate_delay(4) == 16.0`（未封顶）

---

#### 用例 EP-03：ErrorPolicy 冻结

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-EP-2 [P2] |
| 测试层级 | unit |
| 覆盖准则 | 分支：__setattr__ 冻结 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：字段 ∈ {max_retries} → 代表值 = `max_retries`

**Given**：
- `policy = ErrorPolicy()`

**When**：
- `policy.max_retries = 10`

**Then**：
- 抛异常（冻结——重试策略不可运行期篡改）

---

### 4.9 RateLimiter / HaltChecker / StepGuard —— 简单守卫（unit / component(fake)）

**不变式**：
- inv-RL-1：`check(tool_name, max_calls)` 未超限 → 放行（None）；
- inv-RL-2：超限 → 返回拒绝 ToolResult；无论是否超限都计数；
- inv-RL-3：未配置 max_calls → 只计数不拦截；
- inv-RL-4：`reset_turn()` 清零计数；`get_count(tool_name)` 读取计数；
- inv-HALT-1：`should_halt(success, halt_on_failure)` —— 仅当 `success=False and halt_on_failure=True` 返回 halt；
- inv-SG-1：StepGuard 为 runtime_checkable Protocol，鸭子类型实现可被 isinstance 识别；未注入即不启用。

---

#### 用例 RL-01：未超限 → 放行

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-RL-1 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：count < max_calls → 放行 |
| Oracle | golden value |
| Mock | 否（直接实例化） |

**等价类划分**：count vs max_calls → 代表值 = `max_calls=5`、count=0

**Given**：
- `rl = RateLimiter()`

**When**：
- `result = rl.check("write_file", max_calls=5)`

**Then**：
- 返回值：`result is None`（放行）
- 副作用：`rl.get_count("write_file") == 1`（无论是否超限都计数——观测层数据）

---

#### 用例 RL-02：超限 → 拒绝

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-RL-2 + Risk-RL-1 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：count ≥ max_calls → 拒绝 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：count ∈ {== max_calls, > max_calls} → 代表值 = `max_calls=2`、第 3 次调用

**Given**：
- `rl = RateLimiter()`；`rl.check("write_file", max_calls=2)` × 2（已满）

**When**：
- `result = rl.check("write_file", max_calls=2)`

**Then**：
- 返回值：`result` 为拒绝 ToolResult（超限拒绝语义，非 None——频率失控被拦，Risk-RL-1）
- 副作用：`rl.get_count("write_file") == 3`（超限调用同样计数）

---

#### 用例 RL-03：未配 max_calls → 只计数不拦截

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-RL-3 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：max_calls=None |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：max_calls ∈ {None} → 代表值 = 不传 max_calls

**Given**：
- `rl = RateLimiter()`

**When**：
- `result = rl.check("write_file")` × 10

**Then**：
- 返回值：每次都 `is None`（未配置上限只计数不拦截）
- 副作用：`rl.get_count("write_file") == 10`

---

#### 用例 RL-04：reset_turn 清零

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-RL-4 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：turn 边界（与 R4 同节奏） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：turn ∈ {同 turn, 跨 turn} → 代表值 = 计数 3 后 reset

**Given**：
- `rl = RateLimiter()`；`rl.check("write_file", max_calls=5)` × 3

**When**：
- `rl.reset_turn()`；`rl.get_count("write_file")`

**Then**：
- 返回值：`get_count == 0`（清零，下一轮重新计数——run_core 每轮 Phase 1 调用）

---

#### 用例 HALT-01：失败 + halt_on_failure=True → halt

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-HALT-1 + Risk-HALT-1 [P0] |
| 测试层级 | unit |
| 覆盖准则 | 分支：success=False × halt_on_failure=True → halt |
| Oracle | golden value（布尔） |
| Mock | 否 |

**等价类划分**：(success, halt_on_failure) ∈ {(False, True)} → 代表值 = 该组合

**Given**：
- `checker = HaltChecker()`

**When**：
- `should_halt = checker.should_halt(success=False, halt_on_failure=True)`

**Then**：
- 返回值：`should_halt is True`（硬停止信号——run 必须终止，Risk-HALT-1）
- 副作用：无副作用

---

#### 用例 HALT-02：成功 + halt_on_failure=True → 不停

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-HALT-1 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：success=True × halt_on_failure=True → 不停 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：(success, halt_on_failure) ∈ {(True, True)} → 代表值 = 该组合

**Given**：
- `checker = HaltChecker()`

**When**：
- `should_halt = checker.should_halt(success=True, halt_on_failure=True)`

**Then**：
- 返回值：`should_halt is False`（成功不硬停）
- 副作用：无副作用

---

#### 用例 HALT-03：失败但 halt_on_failure=False → 不停（仅失败不硬停）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-HALT-1 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：success=False × halt_on_failure=False → 不停 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：(success, halt_on_failure) ∈ {(False, False), (True, False)} → 代表值 = `(False, False)`（关键组合）

**Given**：
- `checker = HaltChecker()`

**When**：
- `should_halt = checker.should_halt(success=False, halt_on_failure=False)`

**Then**：
- 返回值：`should_halt is False`（halt_on_failure 未开启时失败不触发硬停止）
- 副作用：无副作用

---

#### 用例 SG-01：StepGuard 协议契约 —— 鸭子类型实现可识别、未注入即不启用

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-SG-1 [P3] |
| 测试层级 | unit |
| 覆盖准则 | 分支：Protocol isinstance 契约 |
| Oracle | golden value（isinstance） |
| Mock | 是（Mock 实现满足/不满足契约） |

**等价类划分**：实现形态 ∈ {满足契约, 缺方法} → 代表值 = 两个 Mock 类

**Given**：
- `class GoodGuard: def should_halt(self, *, run_id, usage) -> GuardDecision: return GuardDecision(halt=False, reason="")`
- `class BadGuard: pass`（缺 should_halt）

**When**：
- `isinstance(GoodGuard(), StepGuard)`；`isinstance(BadGuard(), StepGuard)`

**Then**：
- 返回值：前者 `True`（runtime_checkable 鸭子类型可识别）、后者 `False`
- 语义：未注入 step_guard 时 executor 全链路不受影响（EX-01 已隐含验证——SDK 不因无守卫而停机）

---

## 5. 覆盖矩阵汇总（新设计自检）

| 领域 | 用例 | 断言类型 | P0 数 |
|------|------|----------|-------|
| A. HarnessExecutor | EX-01..10（10） | 行为 + hook + 审计 | 5 |
| B. PermissionGuard | PG-01..04（4） | 决策 golden | 2 |
| C. HITLController | HITL-01..06（6） | 决策 golden | 4 |
| D. CircuitBreaker | CB-01..08（8） | 状态机 + hook 参数 | 3 |
| E. Idempotency | ID-01..08（8） | 状态 + 并发计数 | 3 |
| F. OutputGuard | OG-01..05（5） | 蜕变 + hook | 1 |
| G. ContextWindowBudget | CWB-01..05（5） | golden + 异常 | 1 |
| H. Limits/ErrorPolicy | EL-01..03 + EP-01..03（6） | golden + 异常 | 0 |
| I. RateLimiter/Halt/StepGuard | RL-01..04 + HALT-01..03 + SG-01（8） | golden | 2 |
| **合计** | **60** | — | **21** |

旧矩阵对比：test_behavior.py 10 个领域覆盖点全部映射（除 CostCalculator，见 §6-5）；
test_behavior_mock.py 的 hooks 参数 / _locks 泄漏 / async-sync 共享缓存 / 日志点全部覆盖；
新增旧测试缺失的 HarnessExecutor 编排（EX-01..08）与并发闸门（EX-09/10）。

---

## 6. 推断与确认清单（known-gap / 需核对）

| # | 项 | 设计取值 | 状态 |
|---|----|----------|------|
| 1 | `execute_tool(tool_name, args, ctx)` 参数签名与顺序 | 按 `_run_feedback_stage(tool_name, args, result, ctx)` 推断 | ⚠️ 落码前核对 executor.py |
| 2 | 拒绝 ToolResult 的具体语义字段（R1/R3 拒绝结果如何标记 success/reason）、`execute_tools_concurrent` 失败返回形态（返回聚合结果 vs 抛异常）、max_bytes/max_calls 的注入来源（构造参数 vs 每次调用参数） | 设计按「非 None 拒绝结果 + 含语义字段」占位 | ⚠️ 核对后回填具体字段名 |
| 3 | `_CircuitBreakerState` 是否支持注入时钟 | 设计用短超时 + 真实 sleep（量级差 ≥2 倍），已确定性 | ⚠️ 若支持注入时钟，test-coder 优先注入 |
| 4 | 信任等级（EXTERNAL/SUB_AGENT/ORCHESTRATOR）不在 `check_permission` 签名内 | 以 `sensitive_permissions` 集合差异覆盖；若签名另有 trust_level 参数则追加 PG 边界用例 | ⚠️ 核对 |
| 5 | **CostCalculator 不在本次 API 全景范围**（旧矩阵有「未知模型警告、费用精度」覆盖行） | 未设计用例 | ⚠️ **旧覆盖缺口**，需主 Agent 确认是否补测（可能属 llm/费用模块） |
| 6 | IdempotencyGuard check/record 返回类型；completed 状态是否参与 check 去重；`_cache` 存储内容 | 按「未命中返回 None、命中返回 deduplicated 结果、in-flight/completed 双集合」设计；ID-08 的 check_and_execute 为测试辅助（若源码无此方法则用 check→执行→record 三步） | ⚠️ 核对 |
| 7 | `PendingApproval` / `ResumeDecision` 具体字段名与 `resolve_resume` 非法决策的 fail-closed 行为 | 设计按 fail-closed（未知决策不执行 pending）写 | ⚠️ 若实现非 fail-closed 属 [known-gap]，按设计预期断言 + xfail |
| 8 | `ContextWindowBudget` 构造参数名（slots ratio 形参名）、默认 context_window 值；冻结抛异常类型（PermissionError vs 其他） | 默认值按模块总结（0.15/0.10/0.50/0.10）；CWB-04 抛异常类型未指定 | ⚠️ 核对 |
| 9 | `CircuitBreakerConfig.__post_init__` 三档校验（失败阈值 ≤ 最大恢复超时对应代数，防配置倒挂） | 配置对象定义在 capability 层（tool/types.py），**不在 behavior 测试范围**；CB 用例全部使用合法配置 | 明确不测（属 tool 层） |
| 10 | `HITLController.check_approval` 入参形态（sensitivity_value 是 int 还是枚举） | 按 `SensitivityLevel.X.value` 推断（模块总结写 `check_approval(sensitivity_value, tool_name)`） | ⚠️ 核对 |

---

## 7. 给 test-coder 的落地约定

1. **async 用例**统一 `@pytest.mark.asyncio` 或 `asyncio.run` 包装；事件循环每个用例独立，杜绝顺序依赖
   （旧测试曾因 `asyncio.get_event_loop()` 弃用产生顺序依赖，见模块总结 §10 注记——重建禁止复用共享 loop）。
2. **Fixture 粒度**：`fake_registry` / `fake_audit` / `recording_hooks` 定义为 module 级 fixture 工厂，
   每个用例内新建实例（防用例间状态串扰）。
3. **日志断言**：一律 caplog 断言**行为为主、日志为次**；仅 PG-03/OG-04/CWB-05 三处断言 warning/info 存在性，不断言日志文本细节。
4. **时间控制**：所有 sleep 用 `asyncio.sleep`（async）或短真实等待，禁止用真实时钟比对绝对值；
   熔断/反馈超时用例的量级差 ≥2 倍（设计已内置）。
5. **断言风格**：`assert result is original`（对象同一性）与 `assert result == expected`（值相等）严格区分；
   截断类断言用蜕变关系（长度 ≤ 上限），不硬编码截断内容。
6. **known-gap 落地**：§6 中若核对发现实现与设计预期不符，按「设计预期为准」写断言并标
   `pytest.xfail`（修复后意外通过即报警），不迁就现状改设计。
