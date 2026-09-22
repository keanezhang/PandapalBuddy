# compact budget / 上下文窗口预算重构 · 测试用例设计

- 状态：设计稿（从设计文档 + 源码从零推导，不以既有测试为基线）
- 真相源：
  - `pandaren/memory/COMPACT_THRESHOLD_SPEC.md`（§0 实测基线、§4 公式、§5.2 实测修正表）
  - `pandaren/memory/COMPACT_BUDGET_AUDIT.md`（G1~G16 治理清单）
  - `pandaren/memory/COMPACT_BUDGET_ROLLOUT.md`（B1/B2/B3/B5 落地）
- 唯一真相源公式（本文所有 golden value 均由下列公式独立手算，不抄实现输出）：
  ```
  CW      = floor(M × context_window_ratio)                 # 0.80 / huge 档 0.60
  slot    = max(floor(M × ratio), floor)                    # 固定槽位带兜底下限
  CONV    = CW − system − tool − recall                     # recall=0，绝对槽位模式下恒等式
  T       = CONV − derive_buffer_tokens(CONV)
  buffer  = derive_buffer_tokens(CONV) = max(0, min(5000, CONV//4))
  KEEP_CAP= max(1, int(T×0.45))
  MIN_KEEP= max(1, min(int(T×0.12), KEEP_CAP))
  TOOL_CAP= clamp(int(T×0.15), [8000, 30000])
  TARGET  = int(T×0.70) − SYS_ACTUAL − OLD_ATT − (RESERVED + REINJECT)
  ```
- 双技术栈：
  - Python → **pytest**（`pandaren/memory/tests`、`pandaren/behavior/tests`、`pandapal/config/tests`）
  - TypeScript → **vitest + @testing-library/react**（已确认：`pandapal_desktop/vitest.config.ts`、`package.json` 含 `vitest` 与 `@testing-library/react`）

---

## 1. 风险清单与 P0/P1 分级

| ID | 风险 | 严重度 S | 可能性 L | 优先级 | 落点 |
|----|------|:--:|:--:|:--:|------|
| R1 | 阈值跨层口径漂移：builder 的 `derive_compact_threshold(conversation_slot)` 与 run_local footer 的 `derive_compact_threshold(budget.conversation_tokens)` 不一致 | 高（压缩不触发/提前触发） | 高（两处独立计算） | **P0** | builder.py / run_local.py / resolver |
| R2 | 绝对槽位模式残差 ≤0 未 fail-fast；或只给一个 abs 时 `conversation_ratio` 未被忽略 | 高（预算超配→400） | 中（配置错误/极端小窗） | **P0** | context_window_budget.py |
| R3 | 派生函数边界：小窗口 buffer 收缩错误、`min_keep > max_keep`、tool cap 夹逼失效 | 中（策略参数非法） | 中 | **P1** | memory/constants.py |
| R4 | resolver 优先级错误（env / [models] / [patterns] / [default]），或非法 env 未忽略导致直接 400 | 高（配额错误→400/浪费） | 中 | **P0** | context_window_resolver.py |
| R5 | resolver 回退缺失：fallback table、I1 校验、防御性回落分支 | 中 | 低（异常配置） | **P1** | context_window_resolver.py |
| R6 | 压缩后预留缺失：`target_tokens` 未扣 `RESERVED+REINJECT`，压缩后被回注顶回阈值 → `context_overflow` 停机 | 高（停机） | 中（启用回注/摘要时） | **P0** | memory/memory.py |
| R7 | 压缩后固定占用 > 0.3×T 未告警（静默埋雷） | 中 | 中 | **P1** | memory/memory.py |
| R8 | `estimate_context_breakdown` 四段之和 ≠ real_tokens，或 estimator 抛异常时未 Fail-Safe → 观测数据炸断 run | 高（run 中断） | 低（异常路径） | **P0** | engine/run_core.py |
| R9 | 前端进度条：overLine 红色判定、ctxPct/markPct 上限、context_window=0 不渲染、parts 全零退化 | 低~中（展示错误） | 高 | **P1** | MessageBubble.tsx |
| R10 | micro_compact 单条工具结果截断中文"越截越大"（判据/裁剪两把尺子） | 高（单条结果吞满上下文） | 中（中文长结果） | **P0** | micro_compact.py |
| R11 | ToolBudget 口径未统一：未注入 estimator 回落 bytes/4；注入后异常兜底 100 | 中（配额误判） | 中 | **P1** | tool/exposure/budget.py |
| R12 | 子 Agent 未继承 abs 槽位 / token_estimator → 子 Agent 阈值与尺子与父级漂移 | 高（委派树预算错乱） | 中 | **P1** | builder.py |
| R13 | `init_from_restore` 用模块级默认而非 `self._compact_threshold`（恢复预算与配置脱钩） | 中 | 中 | **P2** | memory/memory.py |
| R14 | loop.py 摘要预算未补传 `context_window`；static_context 截断仍用 `chars/4` | 中 | 中 | **P2** | engine/loop.py |

### 故障注入子清单

| 故障类型 | 注入点 | 预期行为 | 对应用例 |
|---------|--------|---------|---------|
| estimator 抛异常 | `Memory.estimate_text`（breakdown 三小头估算） | `estimate_context_breakdown` 返回 None，不参与停机裁决 | BRK-3 |
| estimator 抛异常 | `ToolBudget._estimate_tokens` | 回落 100 + WARNING（计费类留痕） | TB-3 |
| toml 缺失/损坏 | `context_window_resolver._load_table` | 回落内置 fallback tier + WARNING | RES-7 |
| env 非正整数 | `resolve_model_max_context` | 忽略 env，继续查表 | RES-2 |
| 固定槽位配置过大 | `resolve_budget`（sys+tool+recall+min_conv > CW） | 按 CW 比例回落 + WARNING（不拒绝启动） | RES-10 |
| 压缩后固定占用过大 | `Memory.__init__`（RESERVED+REINJECT > 0.3T） | WARNING，不拒绝启动 | MEM-1 |
| 压缩 target ≤ 0 | `compact_if_needed`（system+attachments+固定占用自超） | 放弃压缩返回 `current_tokens` | MEM-7 |
| drop_summarizer 抛异常 | `compact_if_needed` Layer 3 | 摘要置 None，继续压缩 | （既有行为，回归备注） |

---

## 2. 不变式清单

| ID | 不变式 | 违反后果 |
|----|--------|---------|
| inv-1 | 阈值口径一致：`derive_compact_threshold(resolver.conversation_tokens) == derive_compact_threshold(builder.CWB.conversation_tokens)`，且等于 builder 注入 Memory 的 `compact_threshold` | 跨层漂移（R1） |
| inv-2 | 槽位守恒：绝对槽位模式下 `Σ(system+tool+conversation+recall) == CW` | 预算超配/泄漏（R2） |
| inv-3 | 残差正数：绝对槽位模式下 `conversation > 0`，否则 fail-fast | 非法配置静默（R2） |
| inv-4 | 派生边界：`0 ≤ buffer ≤ min(5000, CONV//4)`；`1 ≤ min_keep ≤ max_keep`；`8000 ≤ tool_cap ≤ 30000` | 策略参数非法（R3） |
| inv-5 | 回注不溢出：`target_tokens` 显式扣减 `RESERVED+REINJECT`；`RESERVED+REINJECT ≤ 0.3T`（否则告警） | 压缩后立即再触发（R6/R7） |
| inv-6 | breakdown 守恒：`system+tools+attachments+history == real_tokens`（history 为残差）；`real_tokens ≤ 0` 或异常 → None | 观测数据失真/炸断（R8） |
| inv-7 | 前端降级：`context_window=0` 不渲染；`ctxPct/markPct ∈ [0,100]`；parts 全零/缺省退化单段 | 进度条误导（R9） |
| inv-8 | 同一把尺子：builder 注入的 `token_estimator` 贯穿 Memory / WindowedKeepPolicy / MicroCompactor / ToolBudget / loop 截断 / breakdown 估算 | 口径漂移（R10/R11/R14） |

> inv-1 / inv-2 / inv-4 / inv-6 天然适合 **[property]** 属性测试：对一整类输入（任意 CW/sys/tool/recall 组合、任意 real_tokens/est 组合）恒成立，交由下游 test-coder 用随机生成验证，而非手写 3 个样例。

---

## 3. Oracle 与确定性控制策略

### Oracle 选型

| 被测对象 | Oracle 类型 | 说明 |
|---------|-----------|------|
| 派生函数 / CWB / resolver 档位表 | **golden value** | 公式白纸黑字写死，人工可独立手算（如 563,000、67,560、25,350） |
| derive_* 边界 | **property** | inv-4 对任意阈值/槽位恒成立 → 交给属性测试 |
| 阈值一致性（inv-1） | **property + golden** | 对任意合法 CW/sys/tool 组合断言两侧相等 |
| micro_compact 截断 | **蜕变关系** | 输出 token 无法预知 → 断言 `estimate(截断后) ≤ cap`、`截断后 ≤ 原文`（不许 golden 抄实现） |
| compact_if_needed | **参考实现/蜕变** | 断言 `split_with` 收到的 target == 公式值（公式独立推导），不硬编码最终 token 数 |
| context_breakdown | **property** | 四段之和 == real_tokens（对任意 est 组合成立） |
| 前端进度条 | **golden** | pct = `min(100, x/context_window×100)`，人工可算 |

### 确定性控制（防 flaky）

| 不确定源 | 对策（写入 Given） |
|---------|-------------------|
| 环境变量 `PANDAPAL_MODEL_MAX_CONTEXT` | resolver 测试用 `monkeypatch.setenv/delenv` 固定，且 fixture 重置 `resolver._TABLE = None` |
| `_load_table` 进程内缓存 | 每个 resolver 用例前置重置 `_TABLE=None`（或 monkeypatch `_load_table` 返回 crafted table），teardown 还原 |
| 日志告警 | 用 `caplog` 断言 warning 存在/不存在，不依赖 logger 配置 |
| 时间戳（compact boundary） | 不断言 `timestamp` 具体值，只断言字段存在（或 freeze 时钟） |
| 浮点比例 | 全部用 `math.floor` 后的 int 断言，不用浮点 `==` |

---

## 4. Mock / Fake 策略

| 依赖 | 决策 | 理由 |
|------|------|------|
| TokenEstimator | **Fake**（注入 `FakeTokenEstimator`，`estimate(msg)` 按 content→token 映射返回可配置值） | 需要精确控制 system/attachment/history 估算值，避免真实 tokenizer 的不确定 |
| DropSummarizer | **Fake**（返回固定 system 摘要 / 抛异常） | 应用层扩展点，不调真 LLM |
| PostCompactSource | **Fake**（返回固定 ReinjectionAttachment / 抛异常） | 回注预算测试需可控 |
| LongTermMemory.load_for_restore | **monkeypatch** | 断言 `token_budget == self._compact_threshold`，不碰真实持久化 |
| `_short_term.split_with` | **monkeypatch**（记录 target 并返回可控 split） | 断言 target_tokens 公式，隔离切分算法 |
| `_load_table` / `_TOML_PATH` | **monkeypatch** | 注入 crafted tier 表（含病态档位）触发防御分支 |
| LLMClient / ToolRegistry / HarnessExecutor | **Fake / 空** | builder 装配测试不需要真实 LLM |
| 前端 `ReplyUsage` | **纯值对象**（构造 fixture） | 纯渲染组件，无外部 I/O |

> 纯函数（derive_*）与纯数据类（CWB、StepUsage、ResolvedBudget、RunUsageSummary）**零 mock**，直接构造。

---

## 5. 覆盖矩阵（风险/不变式 → 用例）

| 风险/不变式 | 覆盖用例 |
|-----------|---------|
| R1 / inv-1 阈值一致性 | **XTH-1**、BLD-1、RES-11 |
| R2 / inv-2 / inv-3 绝对槽位 | CWB-2、CWB-3、CWB-4、CWB-5、CWB-6、CWB-7 |
| R3 / inv-4 派生边界 | DER-1、DER-2、DER-3、DER-4 |
| R4 / R5 resolver | RES-1~RES-12 |
| R6 / R7 / inv-5 压缩预留 | MEM-1、MEM-6、MEM-7、MEM-8 |
| R8 / inv-6 breakdown | BRK-1、BRK-2、BRK-3、BRK-4 |
| R9 / inv-7 前端进度条 | FE-1~FE-8 |
| R10 micro_compact 截断 | MIC-1~MIC-5 |
| R11 ToolBudget 口径 | TB-1、TB-2、TB-3 |
| R12 子 Agent 继承 | BLD-4 |
| R13 restore 预算 | MEM-2 |
| R14 loop 摘要/截断 | LOP-1、LOP-2 |
| inv-8 同一把尺子 | BLD-3、TB-1、LOP-1、BRK-1 |

---

## 6. 用例展开

### 6.1 派生函数（`pandaren/memory/constants.py`）

#### 用例 DER-1：`derive_buffer_tokens` 小窗口收缩与下限

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R3 [P1] + inv-4 **[property]** |
| 测试层级 | unit |
| 覆盖准则 | branch：`min(5000, slot//4)` 与 `max(0, ...)` 两分支 |
| Oracle | golden value（可手算） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：按 `slot` 相对 `4×5000=20000` 的位置切分 → 代表值：`1_000_000`（大）、`8_000`（中）、`4`/`3`（临界）、`1`/`0`/`-5`（极小/非法）。

**Given**：无前置。
**When**：`derive_buffer_tokens(1_000_000)`、`(8_000)`、`(4)`、`(3)`、`(1)`、`(0)`、`(-5)`。
**Then**：
- `== 5000` / `2000` / `1` / `0` / `0` / `0` / `0`
- 属性断言（下游属性测试）：对任意 `slot`，`0 ≤ buffer ≤ min(5000, max(0, slot)//4)`。
- 无副作用。

#### 用例 DER-2：`derive_compact_threshold` 口径 = slot − buffer 且下限 1

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R3 [P1] + inv-4 **[property]**（与 DER-1 联立） |
| 测试层级 | unit |
| 覆盖准则 | branch：`max(1, ...)` 下限分支 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：slot 大（buffer 饱和）→ `568_000`；slot 中 → `70_400`；slot 极小 → `1`/`0`。

**When**：`derive_compact_threshold(568_000)`、`(70_400)`、`(8_000)`、`(1)`、`(0)`。
**Then**：
- `== 563_000`（568000−5000）/ `65_400`（70400−5000）/ `6_000`（8000−2000）/ `1` / `1`
- 属性断言：`T == max(1, CONV − derive_buffer_tokens(CONV))`，且 `T ≥ 1`。
- 无副作用。

#### 用例 DER-3：`derive_keep_window` 边界与单调性

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R3 [P1] + inv-4 **[property]** |
| 测试层级 | unit |
| 覆盖准则 | branch：`max(1, int(T×0.45))`、`min(int(T×0.12), max_keep)`、`max(1, ...)` |
| Oracle | golden value（1M 档 67,560 / 253,350） |
| Mock | 否 |

**等价类划分**：T 大（563_000）/ 中（65_400）/ 极小（1、2、10）。

**When**：`derive_keep_window(563_000)`、`(65_400)`、`(10)`、`(1)`。
**Then**：
- `== (67_560, 253_350)` / `(7_848, 29_430)` / `(1, 4)` / `(1, 1)`
- 属性断言：任意 `T ≥ 1`：`1 ≤ min_keep ≤ max_keep`；`max_keep == max(1, int(T×0.45))`；单调（T↑ 则 min/max 均不降）。
- 无副作用。

#### 用例 DER-4：`derive_single_result_max_tokens` 夹逼 [8000, 30000]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R3 [P1] + inv-4 **[property]** |
| 测试层级 | unit |
| 覆盖准则 | branch：`min(ratio×T, 30000)` 与 `max(8000, ...)` 双夹逼 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：T 大（ratio×T > 30000）→ `563_000`；T 中（区间内）→ `65_400`、`60_000`；T 小（ratio×T < 8000）→ `1_000`、`50_000`。

**When**：`derive_single_result_max_tokens(563_000)`、`(65_400)`、`(60_000)`、`(1_000)`。
**Then**：`== 30_000` / `9_810` / `9_000` / `8_000`（floor）
- 属性断言：任意 T：`8000 ≤ cap ≤ 30000`。
- 无副作用。

---

### 6.2 上下文窗口预算（`pandaren/behavior/context_window_budget.py`）

#### 用例 CWB-1：默认 ratio 模式（recall 默认 0 回收配额）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R2 基线 [P1] + inv-2 |
| 测试层级 | unit |
| 覆盖准则 | branch：非 abs 分支（`_abs_mode=False`） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：CW=100_000，全默认 ratio（sys 0.15 / tool 0.10 / conv 0.50 / recall 0.00）。

**When**：`ContextWindowBudget(context_window=100_000)`。
**Then**：
- `system_prompt_tokens == 15_000`、`tool_schema_tokens == 10_000`、`recall_tokens == 0`、`conversation_tokens == 50_000`
- `is_abs_mode == False`；`system_prompt_tokens_abs is None`、`tool_schema_tokens_abs is None`
- `get_slot_tokens("recall") == 0`（G4 回归：废弃 recall 不再白占 10_000）
- 无副作用。

#### 用例 CWB-2：双绝对槽位模式——conversation 吸收剩余，ratio 被忽略

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R2 [P0] + inv-2 + inv-3 |
| 测试层级 | unit |
| 覆盖准则 | branch：双 abs 分支（`_abs_mode=True`，两 slot 均走 abs） |
| Oracle | golden value（568_000） |
| Mock | 否 |

**等价类划分**：CW=600_000，sys_abs=24_000，tool_abs=8_000，conversation_ratio=0.50（应被忽略）。

**When**：`ContextWindowBudget(context_window=600_000, system_prompt_tokens_abs=24_000, tool_schema_tokens_abs=8_000, conversation_ratio=0.50, recall_ratio=0.0)`。
**Then**：
- `system_prompt_tokens == 24_000`、`tool_schema_tokens == 8_000`、`recall_tokens == 0`
- `conversation_tokens == 568_000`（== `600_000 − 24_000 − 8_000 − 0`；≠ `floor(600_000×0.50)=300_000`，证明 ratio 被忽略）
- `is_abs_mode == True`
- `build_slot_snapshot()` 四字段之和 == 600_000（inv-2）
- 无副作用。

#### 用例 CWB-3：单绝对槽位模式——只给 sys abs，tool 仍走 ratio

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R2 [P0] + inv-2 |
| 测试层级 | unit |
| 覆盖准则 | branch：单 abs（`_abs_mode=True`，tool 走 ratio 分支） |
| Oracle | golden value（516_000） |
| Mock | 否 |

**等价类划分**：CW=600_000，仅 sys_abs=24_000，tool 默认 ratio 0.10。

**When**：`ContextWindowBudget(context_window=600_000, system_prompt_tokens_abs=24_000, recall_ratio=0.0)`。
**Then**：
- `system_prompt_tokens == 24_000`、`tool_schema_tokens == 60_000`（floor(600_000×0.10)）、`conversation_tokens == 516_000`（=600_000−24_000−60_000）
- `is_abs_mode == True`；`tool_schema_tokens_abs is None`
- 无副作用。

#### 用例 CWB-4：残差 ≤0 fail-fast（含恰好 =0 边界）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R2 [P0] + inv-3 |
| 测试层级 | unit |
| 覆盖准则 | branch：`_conv_tokens <= 0` → raise 分支 |
| Oracle | golden（异常类型 + 信息含各 slot 值） |
| Mock | 否 |

**等价类划分**：残差 <0（CW=1_000, sys=600, tool=600 → −200）与 恰好 =0（CW=1_200, sys=600, tool=600 → 0）。

**When**：
- `ContextWindowBudget(context_window=1_000, system_prompt_tokens_abs=600, tool_schema_tokens_abs=600, recall_ratio=0.0)`
- `ContextWindowBudget(context_window=1_200, system_prompt_tokens_abs=600, tool_schema_tokens_abs=600, recall_ratio=0.0)`
**Then**：两者均抛 `BehaviorConfigError`，异常信息含 `conversation` 与 `≤ 0`（或对应残差值）与 context_window / system / tool 数值。
- 无副作用（构造失败，无对象产生）。

#### 用例 CWB-5：abs 槽位非法值 fail-fast

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R2 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：`_value is not None and (not isinstance(int) or <= 0)` → raise |
| Oracle | golden（异常类型） |
| Mock | 否 |

**等价类划分**：`0`、负数 `-1`、非 int `2.5`、字符串 `"24000"`（各 1 代表值）。

**When**：分别以 `system_prompt_tokens_abs ∈ {0, -1, 2.5, "24000"}` 构造（其余参数合法）。
**Then**：均抛 `BehaviorConfigError`，信息含字段名 `system_prompt_tokens_abs`。
- 无副作用。

#### 用例 CWB-6：effective_ratios 校验——abs 接管后 conversation_ratio 不参与 sum，tool 仍参与

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R2 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：`effective_ratios` 中 sys/tool/conversation 的零化分支 |
| Oracle | golden（不抛 vs 抛） |
| Mock | 否 |

**等价类划分**：
- (a) 双 abs + `conversation_ratio=0.9`（若未忽略会 sum=0.9 或更高，但被零化 → 不抛）
- (b) 单 sys abs + `tool_schema_ratio=0.9, recall_ratio=0.2`（tool 未零化 → sum=1.1 → 抛）

**When**：
- (a) `ContextWindowBudget(context_window=100_000, system_prompt_tokens_abs=24_000, tool_schema_tokens_abs=8_000, conversation_ratio=0.9, recall_ratio=0.0)` → 不抛；`conversation_tokens == 68_000`
- (b) `ContextWindowBudget(context_window=100_000, system_prompt_tokens_abs=24_000, tool_schema_ratio=0.9, recall_ratio=0.2)` → 抛 `BehaviorConfigError`（sum 1.1 > 1.0）
**Then**：如上。
- 无副作用。

#### 用例 CWB-7：槽位守恒（属性测试入口）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 **[property]** |
| 测试层级 | unit |
| 覆盖准则 | N/A（属性覆盖全参数空间） |
| Oracle | property |
| Mock | 否 |

**等价类划分**：参数空间 = CW∈{1..2_000_000} 抽样 × sys_abs∈{None, 合法} × tool_abs∈{None, 合法} × 各 ratio∈[0,1] 抽样（含 abs 混合）。

**When**：属性测试随机生成合法参数 → 构造 `ContextWindowBudget` → 读 `build_slot_snapshot()`。
**Then**：任意不抛错的结果满足 `sys+tool+conv+recall == context_window`（inv-2）；任意 abs 模式满足 `conv == CW − sys − tool − recall`。
- 无副作用。

---

### 6.3 Builder 装配（`pandaren/builder.py`）

#### 用例 BLD-1：透传 abs 并派生 threshold/keep_window/tool_cap（1M 档 golden）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R1 [P0] + inv-1 + inv-4 |
| 测试层级 | component(fake)（Memory 为内存对象，无 I/O） |
| 覆盖准则 | branch：`_context_window_budget is not None` → 派生三分支 |
| Oracle | golden（563_000 / 67_560 / 253_350 / 30_000） |
| Mock | FakeTokenEstimator 可选注入 |

**等价类划分**：1M huge 档 CW=600_000，sys_abs=24_000，tool_abs=8_000 → conv=568_000。

**When**：
```python
b = AgentBuilder().context_budget(context_window=600_000,
    system_prompt_tokens_abs=24_000, tool_schema_tokens_abs=8_000, recall_ratio=0.0)
mem = b._build_memory_factory()()
```
**Then**：
- `mem._compact_threshold == 563_000`（== `derive_compact_threshold(568_000)`）
- `mem._short_term._compaction_policy` 为 `WindowedKeepPolicy`，且 `min==67_560`、`max==253_350`
- `mem._micro_compactor._single_result_max_tokens == 30_000`
- `b._context_window_budget.get_slot_tokens("conversation") == 568_000`

#### 用例 BLD-2：用户显式设置优先于派生

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R3 [P1]（派生不得覆盖显式配置） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_compaction_policy is None` / `_microcompact_single_result_max_tokens is None` 两守卫 |
| Oracle | golden（显式值原样） |
| Mock | 否 |

**等价类划分**：显式 `compaction_policy`、显式 `microcompact_single_result_max_tokens`。

**When**：
- `builder.memory(compaction_policy=custom_policy).context_budget(context_window=600_000)` → materialize
- `builder.memory(microcompact_single_result_max_tokens=12_345).context_budget(context_window=600_000)` → materialize
**Then**：
- 前者 `mem._short_term._compaction_policy is custom_policy`（不派生覆盖）；`_compact_threshold` 仍派生
- 后者 `mem._micro_compactor._single_result_max_tokens == 12_345`

#### 用例 BLD-3：token_estimator 贯穿注入（同一把尺子）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 [P1] + R11/R14 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`self._token_estimator is not None` 各透传点 |
| Oracle | golden（对象同一性 `is`） |
| Mock | 否（FakeTokenEstimator 实例） |

**When**：`est = FakeTokenEstimator(); b = AgentBuilder().memory(token_estimator=est).context_budget(context_window=100_000, system_prompt_tokens_abs=24_000); mem = b._build_memory_factory()()`。
**Then**：
- `mem._token_estimator is est`
- `mem._short_term._compaction_policy._token_estimator is est`
- `mem._micro_compactor._token_estimator is est`
- `b._build_tool_layer(...)` 产出的 `ToolBudget._token_estimator is est`（或经 `_build_tool_layer` 注入点验证）

#### 用例 BLD-4：子 Agent 继承 abs 槽位 + token_estimator

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R12 [P1] + inv-1/inv-8 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_build_sub_agent_from_blueprint` 中 `self._context_window_budget is not None` 与 `self._token_estimator is not None` 两继承分支 |
| Oracle | golden（子 builder 的 budget 四字段 == 父级） |
| Mock | Fake LLMClient / 空 tools_pool / 空 skills_pool / Fake AuditLog |

**等价类划分**：父级双 abs（sys=24_000, tool=8_000）+ 注入 est；子蓝图 tools=() 最小权限。

**When**：构造父 builder（context_budget 双 abs + memory(token_estimator=est)）→ 调 `_build_sub_agent_from_blueprint(bp, fake_llm, [], [], fake_audit)` 得到子 builder。
**Then**：
- 子 builder 的 `_context_window_budget.system_prompt_tokens_abs == 24_000`、`tool_schema_tokens_abs == 8_000`、`context_window == 600_000`
- 子 builder 的 `_token_estimator is est`
- 子 builder materialize 后的 `_compact_threshold == 563_000`（与父级一致，无阈值漂移）

---

### 6.4 Memory（`pandaren/memory/memory.py`）

> 注：以下 MEM 用例默认注入 `FakeTokenEstimator` 以便精确控制各段估算值；用 `caplog` 捕获 warning。

#### 用例 MEM-1：构造期 30% 告警

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R7 [P1] + inv-5 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_fixed_after_compact > compact_threshold * 0.3` |
| Oracle | golden（是否出现 warning） |
| Mock | FakeDropSummarizer / FakePostCompactSource |

**等价类划分**：固定占用 > 0.3T（超）与 ≤ 0.3T（不超）。

**When**：
- (a) `Memory(system_prompt="", compact_threshold=1_000, drop_summarizer=Fake(), post_compact_sources=[Fake()], post_compact_token_budget=8_000, token_estimator=est)` → `reserved=512 + reinject=8000 = 8512 > 300` → warning
- (b) `Memory(compact_threshold=100_000)`（无 summarizer、无 sources → 固定占用 0）→ 无 30% warning
**Then**：(a) `caplog` 含"超过阈值的 30%"与数值；(b) 无该 warning。
- 构造成功（不拒绝启动，E4）。

#### 用例 MEM-2：init_from_restore 用真实阈值

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R13 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：档位 2/3（STM 空 → `load_for_restore`） |
| Oracle | golden（`token_budget == self._compact_threshold`） |
| Mock | monkeypatch `mem._long_term.load_for_restore` 记录 token_budget 返回 [] |

**When**：`mem = Memory(compact_threshold=563_000, token_estimator=est)`；monkeypatch 后 `await` 或同步调 `mem.init_from_restore("t", "sess")`。
**Then**：captured `token_budget == 563_000 == mem._compact_threshold`（R8 回归：曾用模块级默认 64_000）。

#### 用例 MEM-3：estimate_text 与压缩判据同一把尺子

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 [P1] |
| 测试层级 | unit（注入 FakeEstimator） |
| 覆盖准则 | branch：`if not text: return 0` |
| Oracle | golden（FakeEstimator 的映射值） |
| Mock | FakeTokenEstimator |

**等价类划分**：空串、非空串。

**When**：`mem.estimate_text("")`、`mem.estimate_text("hello")`。
**Then**：`0`；`FakeTokenEstimator.estimate([{"role":"user","content":"hello"}])` 的返回值（不落回 chars/4）。

#### 用例 MEM-4：truncate_text_to_tokens 二分正确性

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R14 [P2] + inv-8（截断与判据同尺子） |
| 测试层级 | unit |
| 覆盖准则 | branch：`not text or max_tokens<=0`、`estimate≤max` 早退、二分循环 |
| Oracle | 蜕变关系（截断后 estimate ≤ max）+ golden（空/≤0 → ""） |
| Mock | FakeTokenEstimator（按前缀长度单调） |

**等价类划分**：空文本、`max_tokens<=0`、已 ≤max、超限需截断（英文/中文/代码各 1 代表）。

**When**：
- `truncate_text_to_tokens("", 10)` → `""`；`truncate_text_to_tokens("abc", 0)` → `""`
- `truncate_text_to_tokens("短文本", 1000)` → 原样
- `truncate_text_to_tokens(超限文本, cap)` → 截断结果
**Then**：
- 前两者 `""`
- 已 ≤max 原样返回（`is` 同引用）
- 超限场景：`mem.estimate_text(result) ≤ cap`（蜕变断言，不硬编码截断点）；`result` 为 `text` 的前缀
- 无副作用。

#### 用例 MEM-5：compact_if_needed 未超阈值 → None（基线）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R6 基线 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`current_tokens <= threshold` 早退 |
| Oracle | golden（None） |
| Mock | FakeTokenEstimator（estimate_tokens 返回 ≤ threshold） |

**When**：构造 `threshold=1000` 且 `estimate_tokens() <= 1000` → `await mem.compact_if_needed()`。
**Then**：返回 `None`；`split_with` 未被调用（monkeypatch 探针计数 0）；不写 boundary。

#### 用例 MEM-6：target_tokens 显式扣减 RESERVED + REINJECT

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R6 [P0] + inv-5 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：Layer 2 target 计算（`- system_overhead - attachment_overhead - fixed_after_compact`） |
| Oracle | golden（公式独立推导：`int(T×0.70) − sys − att − (512 + 8000)`） |
| Mock | monkeypatch `estimate_tokens` 返回超阈值；monkeypatch `_short_term.split_with` 记录 target；FakeDropSummarizer + FakePostCompactSource |

**等价类划分**：threshold=100_000；system_overhead=100（Fake 控制）；attachment_overhead=0；reserved=512、reinject=8000。

**When**：构造带 summarizer + 1 个 post_compact_source（token_budget=8000）的 Memory（threshold=100_000）→ 令 `estimate_tokens() > 100_000` → `await compact_if_needed()`。
**Then**：`split_with` 收到的 `target_tokens == int(100_000×0.70) − 100 − 0 − (512 + 8000) == 61_388`。
- 关键：target 必须同时扣掉 `reserved` 与 `reinject`（回归：曾只扣旧 attachments，漏掉即将回注的量）。

#### 用例 MEM-7：target_tokens ≤ 0 → 放弃压缩返回 current_tokens

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R6 [P0] + inv-5（system+attachments+固定占用自超） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`if target_tokens <= 0` |
| Oracle | golden（返回 `current_tokens` int；`split_with` 不调用） |
| Mock | FakeDropSummarizer + FakePostCompactSource（大固定占用） |

**等价类划分**：threshold=1_000，summarizer（reserved=512）+ sources（reinject=8000）→ 固定 8512 已超 `int(1000×0.70)=700`。

**When**：构造上述 Memory → `estimate_tokens()` 返回 >1000 → `await compact_if_needed()`。
**Then**：
- 返回 `current_tokens`（int，非 None）
- `split_with` 未被调用；`drop_summarizer.summarize` 未被调用；无 boundary 写入
- `caplog` 含"跳过压缩"warning

#### 用例 MEM-8：压缩后 overflow 归因（回注推超 vs 压缩不足）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R6 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`overflow` 后 `cause` 二分（`without_attachments <= threshold`） |
| Oracle | golden（cause 字符串 + 返回 int） |
| Mock | FakeTokenEstimator 控制 kept/final/attachment 估值；FakePostCompactSource 返回大 attachment |

**等价类划分**：
- (a) 压缩后 `without_attachments ≤ threshold` 但含 attachments 超 → cause=`post-compact reinjection pushed over threshold`
- (b) `without_attachments > threshold` → cause=`compaction itself insufficient`

**When**：两场景分别构造 → `await compact_if_needed()`。
**Then**：均返回 `final_total`（int）；`caplog` 的 warning 含对应 cause 字符串。
- 无副作用（返回 int 即"最后一层防线"信号）。

---

### 6.5 MicroCompact 截断（`pandaren/memory/compaction/micro_compact.py`）

#### 用例 MIC-1：中文长结果二分收敛 ≤ cap（G10 核心回归）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R10 [P0] + inv-8 |
| 测试层级 | component(fake)（注入按"1 汉字≈1 token"的 FakeEstimator） |
| 覆盖准则 | branch：`tokens > cap` → `_estimate_text(text) <= cap` 早退分支 → 二分 |
| Oracle | 蜕变关系（`estimate(截断后) ≤ cap` 且 `len(截断后) ≤ len(原文)`），**禁止 golden 抄实现** |
| Mock | FakeTokenEstimator（`estimate` 返回 `len(汉字数)`，模拟中文 1 字 1 token） |

**等价类划分**：原文 64_000 汉字（> cap=20_000）。

**When**：`MicroCompactor(single_result_max_tokens=20_000, token_estimator=中文Fake).truncate_single_result_if_needed("汉"*64_000)`。
**Then**：
- 返回 str；`est(结果) ≤ 20_000`（原 bug 是 64_000 → 64_015 越截越大）
- `len(结果) < 64_000`（确实截了）
- 结果以 `MICROCOMPACT_TRUNCATED_SUFFIX` 结尾
- 无副作用（纯函数）。

#### 用例 MIC-2：英文/代码不同字符-token 比例均 ≤ cap

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R10 [P0]（parametrize） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：二分循环对不同密度文本 |
| Oracle | 蜕变关系（`estimate(结果) ≤ cap`） |
| Mock | FakeTokenEstimator 分别模拟 英文≈4字符/token、代码≈2.5字符/token |

**等价类划分**：英文（4 chars/token）、代码（2.5 chars/token）各 1 代表，cap=20_000，原文远超 cap。

**When**：`truncate_single_result_if_needed(英文/代码文本)`。
**Then**：两种密度下 `est(结果) ≤ 20_000`；后缀存在。
- 覆盖原 audit §1.6 的"代码 32,527 ❌ 1.6x"。

#### 用例 MIC-3：suffix token 预留（body_cap = cap − suffix_tokens）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R10 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：`body_cap = max(1, cap − suffix_tokens)` |
| Oracle | golden（`body_cap == cap − est(suffix)`） |
| Mock | FakeTokenEstimator（suffix 返回已知 token 数） |

**When**：注入 suffix 估算 = 3 的 Fake，cap=20_000 → 触发截断，用探针记录 `body_cap`（或直接断言最终 `est(body+suffix) ≤ cap` 且 body 部分 ≤ cap−3）。
**Then**：`body_cap == 19_997`；最终结果 `est(body) + est(suffix) ≤ cap`。

#### 用例 MIC-4：_converge_prefix_length 二分极端与早退

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R10 [P1]（二分正确性） |
| 测试层级 | unit |
| 覆盖准则 | branch：`estimate(text) <= body_cap` 早退；`body_cap` 极小 |
| Oracle | golden（body_cap=1 → 前缀长度 0 或最小可表示） |
| Mock | FakeTokenEstimator |

**When**：`_converge_prefix_length("abc", 1)`、`_converge_prefix_length("abc", 1000)`。
**Then**：前者返回满足 `est(prefix) ≤ 1` 的最大前缀长度（对 1 字 1 token 的 Fake 为 1 或 0）；后者返回 `3`（早退，全文 ≤cap）。

#### 用例 MIC-5：未超限原样返回 + list content 处理

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R10 基线 [P2] |
| 测试层级 | unit |
| 覆盖准则 | branch：`tokens <= cap` 早退；`_content_to_text` list 分支 |
| Oracle | golden（引用不变 / list→text 转换） |
| Mock | FakeTokenEstimator |

**When**：
- `truncate_single_result_if_needed(短文本)`（≤cap）→ 原引用返回
- `truncate_single_result_if_needed([{"type":"text","text":"..."}, {"type":"image"}])`（超限 list）→ 返回 str（`_content_to_text` 提取 text + 占位）
**Then**：前者 `is` 原对象；后者返回 str 且 ≤cap。

---

### 6.6 Loop（`pandaren/engine/loop.py`）

#### 用例 LOP-1：static_context 截断改用 estimator（中文超配额正确截断）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R14 [P2] + inv-8（修 `chars/4` 低估 1.85x） |
| 测试层级 | component(fake)（Fake memory / Fake registry / Fake CWB） |
| 覆盖准则 | branch：`available_for_static <= 0`（丢弃）、`static_context_tokens > available`（截断）、正常 |
| Oracle | golden（截断后 `estimate_text ≤ available`） |
| Mock | FakeTokenEstimator；FakeMemory（`estimate_text` 委托 Fake）；monkeypatch registry 摘要为中文文本 |

**等价类划分**：
- (a) system_prompt 本身超配额 → `available_for_static ≤ 0` → static_context 完全丢弃（返回 None）
- (b) system 不超但 static_context 超 → 截断到 ≤ available
**When**：构造 `AgentLoop`（或直接调 `_build_static_context`），注入中文 system/static 文本 + CWB(system_prompt_tokens=24_000)。
**Then**：
- (a) 返回 None + warning
- (b) 返回截断后文本，`estimate_text(截断) ≤ available_for_static`（同一把尺子，非 chars/4）

#### 用例 LOP-2：skill/agent 摘要预算补传 context_window

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R14 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_ctx_window is not None` → 传参分支 vs 缺省分支 |
| Oracle | golden（`build_skill_summaries` / `build_agent_summaries` 收到 `context_window == budget.context_window`） |
| Mock | FakeSkillRegistry / FakeAgentRegistry（记录收到的 context_window） |

**When**：构造 loop，`_context_window_budget.context_window = 600_000` → `_build_static_context()`。
**Then**：两 registry 均收到 `context_window=600_000`（回归：曾不传参 → 恒用默认 128_000）。

---

### 6.7 Context Breakdown（`pandaren/engine/run_core.py`）

#### 用例 BRK-1：四段之和 == real_tokens（history 残差）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R8 [P0] + inv-6 **[property]** |
| 测试层级 | unit |
| 覆盖准则 | branch：`messages[0].role=="system"` / `tools_for_llm` 非空 / attachments 循环 / `max(0, ...)` |
| Oracle | property（sum == real_tokens）+ golden（各段 == Fake 返回值） |
| Mock | FakeMemory（`estimate_text` 按 content 返回固定值；`post_compact_attachments` 返回固定列表） |

**等价类划分**：含 system 首条 + 2 个 tools + 1 个 attachment；real_tokens=1000；各 est 合计 < 1000。

**When**：`estimate_context_breakdown(fake_memory, messages, tools_for_llm, 1000)`。
**Then**：
- 返回 dict 四键；`system+tools+attachments+history == 1000`
- `history == 1000 − system_est − tools_est − attach_est`
- 属性测试：对任意 `real_tokens > 0` 与任意 est 组合，sum 恒等于 real_tokens。

#### 用例 BRK-2：real_tokens ≤ 0 → None

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R8 [P0] + inv-6 |
| 测试层级 | unit |
| 覆盖准则 | branch：`if real_tokens <= 0: return None` |
| Oracle | golden（None） |
| Mock | FakeMemory |

**When**：`estimate_context_breakdown(mem, [], None, 0)`、`(mem, [], None, -1)`。
**Then**：均返回 `None`；`estimate_text` 未被调用（探针计数 0）。

#### 用例 BRK-3：estimator 抛异常 → Fail-Safe None

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R8 [P0]（观测数据绝不炸断 run） |
| 测试层级 | unit |
| 覆盖准则 | branch：`except Exception → return None` |
| Oracle | golden（None） |
| Mock | FakeMemory.estimate_text 抛 RuntimeError |

**When**：`estimate_context_breakdown(mem, [{...system...}], [], 1000)`（`estimate_text` 抛异常）。
**Then**：返回 `None`（不向上抛）；`caplog` 含 debug 记录。
- 关键断言：**不抛异常**。

#### 用例 BRK-4：缺省段为 0，history 不取负

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6（残差吸收框架开销） |
| 测试层级 | unit |
| 覆盖准则 | branch：无 system 首条 / 无 tools / 无 attachments / `max(0, ...)` |
| Oracle | golden（对应段 == 0） |
| Mock | FakeMemory（post_compact_attachments=()） |

**等价类划分**：messages 首条为 user（无 system）、tools_for_llm=None、attachments 空；real_tokens 小于 est 合计（制造残差为负 → 应 clamp 0）。

**When**：`estimate_context_breakdown(mem, [{"role":"user","content":"x"}], None, 1000)`。
**Then**：`system==0`、`tools==0`、`attachments==0`、`history==1000`。
- 另：est 合计 > real_tokens 时 `history == 0`（不取负）。

> StepUsage.context_breakdown 字段（`step_guard.py`）为纯数据字段（`dict[str,int] | None = None`），无独立逻辑，**豁免独立用例**；其透传由 run_core.py 的 `StepUsage(..., context_breakdown=_ctx_breakdown)` 构造点覆盖（见 BRK-1~4 产出值即可）。

---

### 6.8 ToolBudget（`pandaren/tool/exposure/budget.py`）

#### 用例 TB-1：注入 estimator 后走注入口径

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R11 [P1] + inv-8 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`self._token_estimator is not None` |
| Oracle | golden（== Fake 返回值，非 bytes/4） |
| Mock | FakeTokenEstimator（对给定 schema 返回已知值） |

**When**：`ToolBudget(token_estimator=est)._estimate_tokens(schema)`。
**Then**：返回 `est.estimate([{"role":"user","content":json}])` 的结果（`max(1, ...)`），与 bytes/4 结果不同（构造一个 bytes/4 ≠ est 的 schema 证明走的是注入口径）。

#### 用例 TB-2：未注入回落 bytes/4

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R11 [P1]（无 tokenizer 场景兜底） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`else: byte_len // 4` |
| Oracle | golden（`len(json.encode("utf-8")) // 4`，`max(1, ...)`） |
| Mock | 否 |

**When**：`ToolBudget()._estimate_tokens(schema)`（不传 estimator）。
**Then**：`== max(1, len(json.dumps(...).encode("utf-8")) // 4)`。

#### 用例 TB-3：estimator 抛异常 → 回落 100 + WARNING

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R11 [P1]（计费类兜底留痕） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`except Exception → return _FALLBACK_TOKEN_ESTIMATE` |
| Oracle | golden（100） |
| Mock | FakeTokenEstimator 抛异常 |

**When**：`ToolBudget(token_estimator=抛异常Fake)._estimate_tokens(schema)`。
**Then**：返回 `100`；`caplog` 含 warning（计费类不静默）。

---

### 6.9 Resolver（`pandapal/config/llm/context_window_resolver.py`）

> 所有 RES 用例 fixture：`monkeypatch.delenv("PANDAPAL_MODEL_MAX_CONTEXT", raising=False)` + `resolver._TABLE = None`（teardown 还原）；注入 crafted table 用 `monkeypatch.setattr(resolver, "_load_table", lambda: crafted)` 或直接设 `_TABLE`。

#### 用例 RES-1：env 覆盖最高优先级（正整数）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R4 [P0] |
| 测试层级 | unit（env monkeypatch） |
| 覆盖准则 | branch：`env_raw` 非空 + `int() > 0` 成功分支 |
| Oracle | golden（`(1_000_000, "env:PANDAPAL_MODEL_MAX_CONTEXT", False)`） |
| Mock | 否（monkeypatch env） |

**When**：`monkeypatch.setenv("PANDAPAL_MODEL_MAX_CONTEXT", "1000000")` → `resolve_model_max_context("any-model")`。
**Then**：返回 `(1_000_000, "env:PANDAPAL_MODEL_MAX_CONTEXT", False)`；`caplog` 含"使用环境变量"warning（显式留痕）。

#### 用例 RES-2：非法 env 忽略后回落查表

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R4 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch：`int()` 失败 / `<= 0` → `except ValueError` |
| Oracle | golden（忽略 env，返回查表结果） |
| Mock | monkeypatch env 为 `"abc"`、`"-1"`、`"0"`（parametrize） |

**When**：分别设 env 非法值 → `resolve_model_max_context("claude-sonnet-x")`（命中 pattern 200000）。
**Then**：均返回 `(200_000, "pattern:claude-sonnet*", False)`（env 被忽略）；`caplog` 含"不是正整数，已忽略"。

#### 用例 RES-3：[models] 精确 > [patterns] 通配

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R4 [P0]（优先级链） |
| 测试层级 | unit |
| 覆盖准则 | branch：`mid in models` 优先于 pattern 循环 |
| Oracle | golden（精确值） |
| Mock | crafted table（`models={"deepseek-v4-chat": 1_000_000}`，`patterns={"deepseek-v4*": 131_072}`） |

**When**：`resolve_model_max_context("deepseek-v4-chat")`。
**Then**：返回 `(1_000_000, "exact:deepseek-v4-chat", False)`（精确优先于通配，即便通配也命中）。

#### 用例 RES-4：[patterns] fnmatch 通配（大小写不敏感）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R4 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch：`fnmatch.fnmatch(mid.lower(), pattern.lower())` |
| Oracle | golden（首个命中 pattern 值） |
| Mock | crafted table（`patterns={"*1m*": 1_000_000}`） |

**When**：`resolve_model_max_context("VENDOR-1M-FLAGSHIP")`（大写）。
**Then**：返回 `(1_000_000, "pattern:*1m*", False)`（大小写不敏感命中）。

#### 用例 RES-5：[default] 兜底 + fell_back=True + WARNING

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R4 [P0]（未命中不静默降级） |
| 测试层级 | unit |
| 覆盖准则 | branch：models/patterns 均未命中 → default |
| Oracle | golden（`(128_000, "default", True)`） |
| Mock | crafted table（default=128_000，models/patterns 空） |

**When**：`resolve_model_max_context("unlisted-model")`。
**Then**：返回 `(128_000, "default", True)`；`caplog` 含"回落默认上限"warning。

#### 用例 RES-6：model_id 空 → default

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R4 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：`mid` 为空 → 跳过精确/通配，直达 default |
| Oracle | golden（`(128_000, "default", True)`） |
| Mock | crafted table |

**When**：`resolve_model_max_context("")`、`resolve_model_max_context(None)`。
**Then**：均返回 `(128_000, "default", True)`。

#### 用例 RES-7：_load_table 缓存 + fallback table（toml 不可用）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R5 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch：`_TABLE is not None` 缓存早退 / `except Exception` fallback |
| Oracle | golden（同一对象引用 / fallback tier 生效） |
| Mock | monkeypatch `_TOML_PATH.open` 抛 OSError |

**When**：
- (a) 首次 `_load_table()` 后再次调用 → 返回同一对象（`is`），不再读文件
- (b) 重置 `_TABLE=None` + 令 `open` 抛异常 → `_load_table()` 返回含 `tiers=[_FALLBACK_TIER]` 的表，`caplog` 含"回落内置默认档位"
**Then**：如上。

#### 用例 RES-8：tier 区间四档（含边界）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R4/R5 [P0/P1]（档位落入正确区间） |
| 测试层级 | unit |
| 覆盖准则 | branch：`_resolve_tier` 的 `m <= tier.max_context` 首个命中 / 越界取末档 |
| Oracle | golden（四档手算值） |
| Mock | crafted table（四档与 toml 一致） |

**等价类划分**：M=128_000（small）、200_000（medium）、400_000（large）、1_000_000（huge）；边界 M=160_000（small）/160_001（medium）/600_000（large）/600_001（huge）。

**When**：`resolve_budget(model_id)`（各 model_id 经 table 解析到对应 M）。
**Then**：
- M=128_000 → `(tier="small", CW=102_400, sys=22_016, tool=9_984, conv=70_400)`
- M=200_000 → `(medium, 160_000, 22_000, 10_000, 128_000)`
- M=400_000 → `(large, 320_000, 24_000, 8_000, 288_000)`
- M=1_000_000 → `(huge, 600_000, 24_000, 8_000, 568_000)`
- 边界：M=160_000 → small；M=160_001 → medium；M=600_000 → large；M=600_001 → huge

#### 用例 RES-9：I1 校验（CW + reserved > 0.9×M → WARNING，不拒绝）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R5 [P1]（I1：`CW + max_tokens ≤ 0.9×M`） |
| 测试层级 | unit |
| 覆盖准则 | branch：`cw + reserved_output_tokens > m * 0.9` |
| Oracle | golden（是否 warning） |
| Mock | crafted table |

**等价类划分**：超限（reserved 巨大）与 不超限（默认 8000 + 1M）。

**When**：
- `resolve_budget(model→M=128_000, reserved_output_tokens=200_000)` → `102_400+200_000=302_400 > 115_200` → warning
- `resolve_budget(model→M=1_000_000, reserved_output_tokens=8_000)` → `600_000+8_000=608_000 ≤ 900_000` → 无 warning
**Then**：前者 `caplog` 含"I1 校验失败"；后者无。两者均正常返回 `ResolvedBudget`（不拒绝启动）。

#### 用例 RES-10：防御性回落分支（固定槽位过大 → 按 CW 比例回落）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R5 [P1]（E4/E5 不拒绝启动，显式留痕） |
| 测试层级 | unit |
| 覆盖准则 | branch：`sys_t + tool_t + recall_t + min_conv > cw` |
| Oracle | golden（回落 sys=floor(cw×0.15)、tool=floor(cw×0.10)、recall=0） |
| Mock | crafted 病态 tier（`system_prompt_ratio=0.9, tool_schema_ratio=0.5, floor=0`，M=128_000） |

**When**：`resolve_budget(model→病态 tier)` → sys=115_200, tool=64_000 超 CW=102_400 → 触发回落。
**Then**：
- `system_prompt_tokens == 15_360`（floor(102_400×0.15)）、`tool_schema_tokens == 10_240`、`recall_tokens == 0`
- `conversation_tokens == 76_800`（>0）
- `caplog` 含"已按 CW 比例回落"warning

#### 用例 RES-11：to_builder_kwargs 契约（供 context_budget 直接消费）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R1 [P0] + inv-1（跨层一致性的前提） |
| 测试层级 | unit |
| 覆盖准则 | N/A（无分支） |
| Oracle | golden（kwargs 四键） |
| Mock | 否 |

**When**：`resolve_budget(model→M=1_000_000).to_builder_kwargs()`。
**Then**：
- `== {"context_window": 600_000, "system_prompt_tokens_abs": 24_000, "tool_schema_tokens_abs": 8_000, "recall_ratio": 0.0}`
- 直接传给 `AgentBuilder.context_budget(**kwargs)` 不抛错（见 XTH-1）。

#### 用例 RES-12：describe_table smoke（4 档渲染）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R5 [P2]（CLI/文档展示） |
| 测试层级 | unit |
| 覆盖准则 | N/A |
| Oracle | golden（含 small/medium/large/huge 与 header） |
| Mock | 否 |

**When**：`describe_table()`。
**Then**：字符串含 `small`、`medium`、`large`、`huge` 四行 + header 首行。

---

### 6.10 跨层一致性（P0 头条）

#### 用例 XTH-1：builder 阈值 == resolver footer 阈值

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R1 [P0] + inv-1 **[property]** |
| 测试层级 | component(fake)（无真实 I/O，跨 pandapal↔pandaren 两模块） |
| 覆盖准则 | N/A（一致性恒等式） |
| Oracle | property（两侧相等）+ golden（1M 档 == 563_000） |
| Mock | 否（纯对象构造） |

**等价类划分**：对四档（M=128_000/200_000/400_000/1_000_000）parametrize。

**When**：
```python
budget = resolve_budget(model_id)                    # pandapal resolver 侧
b = AgentBuilder().context_budget(**budget.to_builder_kwargs())  # pandaren builder 侧
t_footer = derive_compact_threshold(budget.conversation_tokens)
t_builder = derive_compact_threshold(b.get_slot_tokens("conversation"))
mem = b._build_memory_factory()()
```
**Then**：
- `t_footer == t_builder == mem._compact_threshold`
- 1M 档三者均 == `563_000`
- 属性测试：任意合法 `(CW, sys_abs, tool_abs, recall)` 下，`resolver.conversation_tokens == CWB(**to_builder_kwargs()).conversation_tokens`（从而 derive 两侧恒等）
- 无副作用。

---

### 6.11 CostBudgetGuard（`pandapal/config/budget/guard.py`）

#### 用例 GRD-1：last_input_tokens 覆盖式记账（非累加）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P1]（进度条分子口径） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`acc.last_input_tokens = max(0, usage.input_tokens)` |
| Oracle | golden（== 最后一次 input） |
| Mock | Fake ledger 无；cost_of_call 可用默认（或用 monkeypatch 固定） |

**When**：两次 `should_halt(run_id, StepUsage(...input_tokens=100))` 再 `(...input_tokens=250)` → `summary(run_id)`。
**Then**：`summary().input_tokens == 350`（跨步累加）但 `summary().last_input_tokens == 250`（覆盖）。

#### 用例 GRD-2：step_count 累加

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`acc.step_count += 1` |
| Oracle | golden（== 调用次数） |
| Mock | 同上 |

**When**：调用 `should_halt` 3 次 → `summary()`。
**Then**：`step_count == 3`。

#### 用例 GRD-3：context_breakdown 保留上次（None 不覆盖）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`if usage.context_breakdown:`（None/空 dict 不覆盖） |
| Oracle | golden（保留上次值） |
| Mock | 同上 |

**When**：
- 第 1 次 `should_halt(usage.context_breakdown={"system":1,...})`
- 第 2 次 `should_halt(usage.context_breakdown=None)`
- `summary()`
**Then**：`summary().context_breakdown == {"system":1,...}`（保留上次可用值）。
- 反向：第 2 次给非空 dict → 覆盖为第 2 次值。

#### 用例 GRD-4：summary 透传 context_window/compact_threshold/context_quotas

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R1/R9 [P0/P1]（footer 分母/标记线透传） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：summary 构造字段 |
| Oracle | golden（== 注入值） |
| Mock | 同上 |

**When**：`CostBudgetGuard(context_window=1_000_000, compact_threshold=563_000, context_quotas={"system_prompt":24000,"tool_schema":8000})` → 一次 `should_halt` → `summary()`。
**Then**：`summary().context_window == 1_000_000`、`compact_threshold == 563_000`、`context_quotas == {"system_prompt":24000,"tool_schema":8000}`。
- 未注入时 `context_quotas is None`。

#### 用例 GRD-5：计价异常 Fail-Safe（不停机）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | O3 基线（守卫绝不炸断 run） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`except Exception → GuardDecision(False)` |
| Oracle | golden（`GuardDecision(False)`） |
| Mock | monkeypatch `cost_of_call` 抛异常 |

**When**：`guard.should_halt(run_id, usage)`（cost_of_call 抛异常）。
**Then**：返回 `GuardDecision(halt=False)`；`caplog` 含 exception 记录。

---

### 6.12 前端进度条（`pandapal_desktop/src/components/ChatArea/MessageBubble.tsx`）

> 测试框架：**vitest + @testing-library/react**。UsageFooter 未导出 → 通过渲染 `MessageBubble`（AI 消息带 `usage`）断言 DOM，或用 `vi.mock` 导出辅助。所有用例构造纯 `ReplyUsage` 值对象（无网络/无 Rust）。

#### 用例 FE-1：context_window=0 不渲染进度条

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P1] + inv-7 |
| 测试层级 | component(fake)（无 I/O） |
| 覆盖准则 | branch：`u.context_window > 0 && (...)` 外层守卫 |
| Oracle | golden（无进度条 DOM） |
| Mock | 否 |

**When**：渲染 `MessageBubble`（AI 消息，`usage.context_window=0`，其余合法）。
**Then**：DOM 不含上下文进度条节点（无 `contextLabel`、无 `last/total (%)` 文本）。

#### 用例 FE-2：overLine 红色（last_input_tokens ≥ compact_threshold）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`overLine = compact_threshold>0 && last >= compact_threshold` |
| Oracle | golden（文本色 danger；单段 bar 背景 danger） |
| Mock | 否 |

**等价类划分**：`last_input_tokens == compact_threshold`（恰好等于）与 `>`（超过）。

**When**：`usage={context_window:1000, compact_threshold:500, last_input_tokens:500, context_breakdown:null}` 渲染。
**Then**：占比文本用 danger 色（`var(--danger)`）；单段 fallback bar 背景 danger。

#### 用例 FE-3：未超线不红

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：overLine 为假 |
| Oracle | golden（accent 色） |
| Mock | 否 |

**When**：`last_input_tokens=499 < compact_threshold=500`。
**Then**：文本为 tertiary 色、单段 bar 为 accent 色。

#### 用例 FE-4：ctxPct / markPct 上限 100

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P1] + inv-7 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`Math.min(100, ...)` |
| Oracle | golden（`(100%)`） |
| Mock | 否 |

**When**：`context_window=1000, last_input_tokens=1500, compact_threshold=1500`（分子/标记均超分母）。
**Then**：占比文本显示 `(100%)`；markPct 上限 100（此时 `markPct < 100` 为假 → 不画标记线，见 FE-8）。

#### 用例 FE-5：parts 全零退化单段

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P1] + inv-7 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`parts.filter(v>0)` → `parts.length===0` → 单段 fallback |
| Oracle | golden（单段 bar 用 ctxPct 宽度） |
| Mock | 否 |

**When**：`context_breakdown={system:0,tools:0,attachments:0,history:0}`（bd 非 null 但全零），`context_window=1000, last_input_tokens=300`。
**Then**：渲染单段 bar（宽度 30%），无四段 legend（`● system` 等不出现）。

#### 用例 FE-6：多段渲染 + v>0 过滤 + 配额显示

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`parts.map` 各段宽度 + `p.q ? "/配额"` |
| Oracle | golden（段宽 = v/context_window×100；system/tools 段带配额文本） |
| Mock | 否 |

**等价类划分**：`context_breakdown={system:24000, tools:3374, attachments:0, history:500000}`（attachments=0 应被过滤），`context_quotas={system_prompt:24000, tool_schema:8000}`。

**When**：渲染。
**Then**：
- 出现 system/tools/history 三段，**attachments 段不出现**（v=0 被过滤）
- system 段宽 == `24%`、tools 段宽 == `0.3374%`（或按实际计算）、history 段宽 == `50%`
- system legend 显示 `24000 / 24000`（配额）、tools legend 显示 `3374 / 8000`；history 无配额后缀

#### 用例 FE-7：context_breakdown 缺省（null/undefined）→ 单段 fallback

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P1] + inv-7 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`bd ? ... : []` |
| Oracle | golden（单段 fallback，无 legend） |
| Mock | 否 |

**When**：`context_breakdown=null`（或字段缺失），`context_window=1000, last_input_tokens=300`。
**Then**：单段 bar（30%）；无 `● system` 等 legend。

#### 用例 FE-8：标记线绘制条件（0 < markPct < 100）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`markPct > 0 && markPct < 100` |
| Oracle | golden（是否出现标记线） |
| Mock | 否 |

**等价类划分**：
- markPct=0（`compact_threshold=0`）
- 0 < markPct < 100（`compact_threshold=500, context_window=1000`）
- markPct=100（`compact_threshold≥context_window`）

**When**：三组 usage 渲染。
**Then**：仅第 2 组出现标记线（absolute 竖线，`left: 50%`）；第 1、3 组不出现。

---

## 7. Known-Gap 清单

| 用例 | 期望行为 | 当前实现现状 | 差距原因 |
|------|---------|-------------|---------|
| （无） | — | — | 本轮从源码核对，实现与设计契约一致，未发现需 `[known-gap]` 标记的期望/实现背离 |

> 说明：若下游 test-coder 在实现时发现某条断言与实现不符（如 `describe_table` 的展示值取 `typical_context` 而非上界、`_resolve_tier` 越界取末档等），**不要按实现现状改断言**，应回填本表并在用例属性表标注 `[known-gap]`。

---

## 8. 测试落点与执行

| 分组 | 落点文件（建议） | 框架 |
|------|----------------|------|
| DER / CWB / BLD / MEM / MIC / LOP / BRK / TB / XTH | `pandaren/memory/tests/test_compact_budget.py`（可再按模块拆分） | pytest |
| RES | `pandapal/config/tests/test_context_window_resolver.py`（扩展） | pytest |
| GRD | `pandapal/config/tests/test_budget_guard.py`（扩展） | pytest |
| FE | `pandapal_desktop/src/components/__tests__/MessageBubbleUsage.test.tsx`（扩展） | vitest + @testing-library/react |

```bash
# Python
.venv/bin/python -m pytest pandaren/memory/tests pandaren/behavior/tests pandapal/config/tests -q
# 前端
cd pandapal_desktop && npm test -- --run MessageBubbleUsage
```

---

## 9. 设计取舍说明

1. **golden value 仅用于公式可手算的确定输出**（1M 档 563_000 / 67_560 / 253_350 / 30_000、四档 resolver 表）；micro_compact 截断、compact target 等"跑一遍才知道"的输出一律改用**蜕变关系/公式恒等式**，避免自指 oracle 退化成 change-detector。
2. **inv-1 / inv-2 / inv-4 / inv-6 标注 [property]**：这些是对一整类输入恒成立的不变式，下游 test-coder 应落地为属性测试（Hypothesis）而非 3 个手写样例。
3. **阈值一致性（XTH-1）单独成节**：它是 P0 头条，跨 pandapal↔pandaren 两模块，宁可多用一条 component 用例把"两侧恒等"钉死，也不埋在 RES 系列里。
4. **StepUsage.context_breakdown / RunUsageSummary 进度条字段** 为纯数据字段，不单独编造逻辑用例；其正确性由 BRK-1~4（产出）与 GRD-1~4（透传）间接证明。
