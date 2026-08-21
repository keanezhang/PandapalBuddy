# 测试用例设计：PlanManager 状态机 + plan/files.py 文件层

> 被测源码：`pandaren/plan/manager.py`（PlanManager）、`pandaren/plan/files.py`
> 测试框架：pytest（`pyproject.toml` `[tool.pytest.ini_options]`，`asyncio_mode=auto`，`testpaths` 含 pandaren）
> 文档定位：本文件为**测试用例设计文档**，不包含可执行代码；下游 test-coder 据此生成 `pandaren/plan/tests/test_*.py`

---

## 0. 任务描述 vs 实际 API（必须先读，避免下游按不存在的接口写测试）

任务描述中的以下 API **在当前代码中不存在**，语义映射如下；所有用例按**实际公开 API** 设计：

| 任务描述 | 实际情况 | 处理 |
|---|---|---|
| `PlanManager.advance()`（校验 planning 才可调用） | 无此方法。等价语义由 `exit(approved=True)`（→executing）承担，且 **exit/reenter 均无阶段守卫** | 用例 4/5/6 按实际行为设计；阶段守卫需求见 Known-Gap KG-1 |
| `PlanManager.approve()/request_changes()` | 不存在。三向决策在 `run_core._handle_plan_action`（值域 approve/refine/abandon），见 `plan_action_test_design.md` | 本文件不覆盖决策编排，仅覆盖 PlanManager 原子方法 |
| `PlanManager.update_plan_content()` | 不存在。写文件走 `files.write_plan_content`，由 run_core 直接调用 | 文件层用例见本文件 §4 |
| `PlanManager.get_state()` | 不存在。等价查询接口：`phase` / `turns` / `is_planning()` / `is_executing()` / `get_plan_file_path()` / `is_reentry` / `context_reminder` | 用例 1-9 全部经公开查询接口断言 |
| `pandaren/plan/state.py` 三向决策模块 | 文件不存在，决策逻辑内联在 run_core | 见 `pandaren/engine/tests/plan_action_test_design.md` |
| `filter_tools`：planning 只留 enter/exit/ask_user；executing 过滤掉 enter 与 exit | 实际：`filter_tools` 保留 `PLAN_MODE_ALLOWED_BUILTIN = {enter_plan_mode, write_plan, exit_plan_mode, ask_user}` + 所有 `read_only` 工具；**阶段无关**（不读 phase）。executing 阶段额外过滤 exit_plan_mode 的逻辑在 run_core，且 **保留 enter_plan_mode**（允许执行中随时重进 plan mode，属设计意图） | 用例 26/27 按实际实现设计 |

---

## 1. 分层判定依据

| 层级 | 判据 |
|---|---|
| unit | 纯内存状态机 / 纯字符串处理，无真实 I/O（tmp_path 之外） |
| integration | 触达真实文件系统（tmp_path 真实目录/文件） |

确定性控制：状态机用例**每个用例用全新 PlanManager 实例**（pytest fixture，避免用例间状态泄漏）；文件层用例全部走 pytest `tmp_path`（隔离目录），不使用仓库内路径；无时间/随机数参与。

---

## 2. 不变式清单

```
inv-1  phase 二元性：phase ∈ {planning, executing}；is_planning() ⟺ phase=="planning"
inv-2  实例隔离：enter()/restore_from_session_meta() 只改变目标实例状态，
       不污染其它 PlanManager 实例（类级无可变状态——修复点回归锚）
inv-3  exit 语义：exit(approved=True) 保留 context_reminder；exit(approved=False) 清空 context_reminder
inv-4  reminder 注入语义：executing 恒 ""；turn 0 恰一次 FULL/methodology；SPARSE 每 5 轮至多一次；
       re-entry 标志被消费一次后即清除（同一轮第二次调用不得重复注入 REFINE）
inv-5  enter() 全量重置：turns/turns_since_reminder/is_reentry/methodology/context_reminder 全部归位
inv-6  restore 默认值：缺 plan_phase → "executing"；turns 恒重置为 0；缺 plan_file_path → None
inv-7  路径安全：生成/校验的路径不包含原始名称中的路径分隔符（防穿越）
inv-8  文件层契约：plan_exists ⟺ 文件存在且非空；read_plan 缺失 → None 不抛；
       写失败（OSError）显式抛出，由调用方处理，不静默吞
```

## 3. 风险清单（按 严重度×可能性 排序）

| 编号 | 风险 | 级别 | 说明 |
|---|---|---|---|
| R1 | 实例状态污染：`restore_from_session_meta` 用 `cls()` 重建实例，若字段被改回类级可变默认值，restore 一个实例会污染所有实例 | **[P0]** | 本任务是修复点，用例 7 为回归锚 |
| R2 | 放弃计划后 context_reminder 残留 → AI 继续执行已放弃的计划 | [P0] | 用例 5 |
| R3 | reminder 注入频率失控（每轮注入 / 从不注入）→ 上下文膨胀或用户无引导 | [P1] | 用例 9/10/13 |
| R4 | re-entry 标志未被消费 → 每轮重复注入 REFINE_REMINDER（卡死提示） | [P1] | 用例 12 |
| R5 | filter_tools 泄漏写工具（write/edit/delete/bash）→ 规划阶段 LLM 直接改代码 | [P1] | 用例 26/27 |
| R6 | plan_name 含 `/` `\` 造成路径穿越，计划文件写出 plans 目录 | [P1] | 用例 19 |
| R7 | `validate_plan_file_path` 放行相对路径（docstring 要求绝对路径，实现未校验 isabs） | [P1] | 用例 21，Known-Gap KG-2 |
| R8 | slug 生成空串/全特殊字符 → 非法文件名 | [P2] | 用例 18 |
| R9 | 读文件缺失/解码失败抛异常（应为 None/False） | [P2] | 用例 24/25 |
| R10 | 写计划文件失败被静默吞掉 → 用户以为已保存 | [P2] | 用例 22/23 |
| R11 | enter() 重复调用残留旧状态（reentry flag / methodology / reminder） | [P2] | 用例 3 |
| R12 | compute_plan_hash 非确定性或非标准格式（写进 session_meta 的 plan_summary 依赖它） | [P3] | 用例 16 |

---

## 4. 用例 × 风险覆盖矩阵

| 用例 | inv | R1 | R2 | R3 | R4 | R5 | R6 | R7 | R8 | R9 | R10 | R11 | R12 |
|---|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| 1.初始状态 | inv-1 | | | | | | | | | | | | |
| 2.enter 重置 | inv-5 | | | | | | | | | | | ✅ | |
| 3.enter 幂等 | inv-5 | | | | | | | | | | | ✅ | |
| 4.exit(True) 保留 reminder | inv-3 | | ✅ | | | | | | | | | | |
| 5.exit(False) 清空 reminder | inv-3 | | ✅ | | | | | | | | | | |
| 6.reenter 状态 | inv-1 | | | | ✅ | | | | | | | | |
| 7.restore 实例隔离 | inv-2 | ✅ | | | | | | | | | | | |
| 8.restore 默认值矩阵 | inv-6 | | | | | | | | | | | | |
| 9.get_reminder executing→"" | inv-4 | | | ✅ | | | | | | | | | |
| 10.get_reminder turn0 FULL | inv-4 | | | ✅ | | | | | | | | | |
| 11.get_reminder methodology | inv-4 | | | ✅ | | | | | | | | | |
| 12.get_reminder re-entry 单次消费 | inv-4 | | | | ✅ | | | | | | | | |
| 13.get_reminder SPARSE 节奏 | inv-4 | | | ✅ | | | | | | | | | |
| 14.handle_tool_result 提交成功 | — | | | | | | | | | | | | |
| 15.handle_tool_result 其它/失败 | — | | | | | | | | | | | | |
| 16.compute_plan_hash | — | | | | | | | | | | | | ✅ |
| 17.read_plan_content 委托 | inv-8 | | | | | | | | | ✅ | | | |
| 18.generate_word_slug | inv-7 | | | | | | | | ✅ | | | | |
| 19.generate_plan_file_path | inv-7 | | | | | | ✅ | | | | | | |
| 20.validate 拒绝矩阵 | inv-7 | | | | | | | ✅ | | | | | |
| 21.validate 合法/相对路径 | inv-7 | | | | | | | ✅ | | | | | |
| 22.write_initial_plan_file | inv-8 | | | | | | | | | | ✅ | | |
| 23.write_plan_content 覆盖 | inv-8 | | | | | | | | | | ✅ | | |
| 24.read_plan 缺失→None | inv-8 | | | | | | | | | ✅ | | | |
| 25.plan_exists 三态 | inv-8 | | | | | | | | | ✅ | | | |
| 26.filter_tools 白名单 | — | | | | | ✅ | | | | | | | |
| 27.filter_tools 去重 | — | | | | | ✅ | | | | | | | |

---

## 5. 用例详情

### 用例 1：初始状态（默认 executing）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 phase 二元性 |
| 测试层级 | unit |
| 覆盖准则 | N/A（构造路径） |
| Oracle | golden（公开查询接口直接观测） |
| Mock | 否 — 纯状态对象 |

**等价类划分**：新实例 → 代表值 = `PlanManager()`

**Given**：无

**When**：构造 `pm = PlanManager()`

**Then**：
- `pm.phase == "executing"`、`pm.is_executing() is True`、`pm.is_planning() is False`
- `pm.turns == 0`、`pm.get_plan_file_path() is None`
- `pm.is_reentry is False`、`pm.context_reminder is None`

---

### 用例 2：enter() 全量重置并置位

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 enter 全量重置 + R11 [P2] |
| 测试层级 | unit |
| 覆盖准则 | N/A |
| Oracle | golden |
| Mock | 否 |

**等价类划分**：enter 输入 → 代表值 = `file_path="p.md", methodology="我的方法论"`

**Given**：`pm = PlanManager()`；先 `pm.exit(approved=False)` 并 `pm.set_context_reminder("残留")` 制造脏状态

**When**：`pm.enter("p.md", methodology="我的方法论")`

**Then**：
- `pm.phase == "planning"`、`pm.is_planning() is True`
- `pm.get_plan_file_path() == "p.md"`
- `pm.turns == 0`、`pm.is_reentry is False`
- `pm.context_reminder is None`（脏 reminder 被清）
- 副作用：`logger` 有 `[plan-manager] entered` 记录（可选用 caplog 断言）

---

### 用例 3：enter() 重复调用幂等

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 + R11 [P2]（任务明确要求"enter 同一文件重复调用幂等"） |
| 测试层级 | unit |
| 覆盖准则 | N/A |
| Oracle | golden |
| Mock | 否 |

**等价类划分**：重复 enter 场景 → 代表值 = 两次 enter + 中间污染

**Given**：`pm = PlanManager()`；`pm.enter("a.md")`；`pm.increment_turn()`（turns=1）；`pm.reenter()`（is_reentry=True、phase=planning）

**When**：再次 `pm.enter("a.md")`

**Then**：
- `pm.turns == 0`（轮次重置）
- `pm.is_reentry is False`（reentry flag 重置）
- `pm.phase == "planning"`、`pm.get_plan_file_path() == "a.md"`

---

### 用例 4：exit(approved=True) 保留 context_reminder

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 + R2 反向（批准后 reminder 必须保留，执行阶段 AI 依赖它）[P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: `exit` 的 approved=True 分支 |
| Oracle | golden |
| Mock | 否 |

**Given**：`pm = PlanManager()`；`pm.enter("p.md")`；`pm.set_context_reminder("实施指引")`

**When**：`pm.exit(approved=True)`

**Then**：
- `pm.phase == "executing"`、`pm.is_executing() is True`
- `pm.context_reminder == "实施指引"`（**未被清空**——批准后执行阶段要继续注入）

---

### 用例 5：exit(approved=False) 清空 context_reminder

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 + R2 放弃计划后 reminder 残留 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: `exit` 的 approved=False 分支 |
| Oracle | golden |
| Mock | 否 |

**Given**：`pm = PlanManager()`；`pm.enter("p.md")`；`pm.set_context_reminder("实施指引")`

**When**：`pm.exit(approved=False)`

**Then**：
- `pm.phase == "executing"`
- `pm.context_reminder is None`（放弃后实施指引必须清空，否则 AI 继续执行已放弃计划）

---

### 用例 6：reenter() 状态

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 + R4（reenter 必须置 planning + reentry flag）[P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: `reenter` |
| Oracle | golden |
| Mock | 否 |

**Given**：`pm = PlanManager()`；`pm.enter("p.md")`；`pm.increment_turn()`（turns=1）；`pm.increment_turn()`（turns=2，构造非首轮）

**When**：`pm.reenter()`

**Then**：
- `pm.phase == "planning"`、`pm.is_planning() is True`
- `pm.is_reentry is True`（下一轮 get_reminder 注入 REFINE）
- `pm.turns == 2`（轮次**不**重置——reenter 是继续同一轮次上下文）

---

### 用例 7：restore_from_session_meta 实例隔离（修复点回归锚）[P0]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 实例隔离 + R1 [P0]（本任务是修复点，此用例是回归锚） |
| 测试层级 | unit |
| 覆盖准则 | N/A（回归锚：锁死"类级无共享可变状态"） |
| Oracle | golden（三实例交叉断言） |
| Mock | 否 |

**设计意图**：`restore_from_session_meta` 通过 `cls()` 重建实例。若未来有人把 `_plan_file_path` 等字段提为类级可变默认值，本用例立即失败（restore 写入会污染类属性，进而污染所有实例）。

**等价类划分**：恢复动作对其它实例的污染 → 代表值 = 三个互不相干的实例

**Given**：
- `pm_a = PlanManager()`；`pm_a.enter("A.md")`（planning 态）
- `pm_b = PlanManager()`（保持初始 executing 态，**从不 enter**）

**When**：`restored = PlanManager.restore_from_session_meta({"plan_file_path": "B.md", "plan_phase": "planning"})`

**Then**：
- `restored is not pm_a and restored is not pm_b`（**新实例**，而非复用）
- `restored.get_plan_file_path() == "B.md"`、`restored.is_planning() is True`
- `pm_a.phase == "planning"`、`pm_a.get_plan_file_path() == "A.md"`（**A 不受影响**）
- `pm_b.phase == "executing"`、`pm_b.get_plan_file_path() is None`、`pm_b.is_reentry is False`（**B 保持初始态**——若字段变类级，此处 B 会被污染为 B.md/planning，用例失败）

---

### 用例 8：restore 默认值矩阵（含非法 phase）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 restore 默认值 [P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: `meta.get("plan_phase", "executing")` 的缺省分支 |
| Oracle | golden |
| Mock | 否 |

**等价类划分**：meta 内容维度 → 代表值 = `{}` / 带非法 phase 的 dict（parametrize）

**Given / When / Then**（parametrize 两行）：

| 输入 meta | 期望 |
|---|---|
| `{}` | `phase == "executing"`、`get_plan_file_path() is None`、`turns == 0`、`is_reentry is False` |
| `{"plan_phase": "submitted", "plan_file_path": "x.md"}` | `phase == "submitted"`、`is_planning() is False`、`is_executing() is False`（**当前行为：不做值域校验，双 False 卡死态**——见 Known-Gap KG-3 改进建议） |

---

### 用例 9：get_reminder 在 executing 阶段恒返回 ""

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 + R3 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: `get_reminder` 首行 phase 守卫 |
| Oracle | golden |
| Mock | 否 |

**Given**：`pm = PlanManager()`；`pm.enter("p.md")`；`pm.exit(approved=True)`（executing 态）

**When**：`r = pm.get_reminder()`

**Then**：
- `r == ""`（执行阶段绝不注入 planning reminder）
- 副作用：`pm.turns_since_reminder` 相关状态不变（`turns == 0`）

---

### 用例 10：get_reminder 首轮返回 FULL_PLANNING_REMINDER 且不推进轮次

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 + R3 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: `get_reminder` 的 turn==0 无 methodology 分支 |
| Oracle | golden（与 `pandaren.plan.prompt.FULL_PLANNING_REMINDER` 常量比较） |
| Mock | 否 |

**Given**：`pm = PlanManager()`；`pm.enter("p.md")`（turns=0）

**When**：`r = pm.get_reminder()`

**Then**：
- `r == FULL_PLANNING_REMINDER`（常量同一性）
- 副作用：`pm.turns == 0`（**get_reminder 不推进轮次**——轮次只在 increment_turn 推进，双计数会破坏 5 轮节奏）

---

### 用例 11：get_reminder 首轮有 methodology 时返回方法论

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 + R3 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: turn==0 有 methodology 分支 |
| Oracle | golden |
| Mock | 否 |

**Given**：`pm = PlanManager()`；`pm.enter("p.md", methodology="先访谈再设计")`

**When**：`r = pm.get_reminder()`

**Then**：`r == "先访谈再设计"`（方法论优先于 FULL，且仅首轮）

---

### 用例 12：get_reminder re-entry 优先级最高且标志单次消费

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 + R4 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: `_plan_mode_is_reentry` 分支 + 消费副作用 |
| Oracle | golden |
| Mock | 否 |

**Given**：`pm = PlanManager()`；`pm.enter("p.md")`；`pm.increment_turn()` ×2（turns=2）；`pm.reenter()`（is_reentry=True）

**When**：
1. `r1 = pm.get_reminder()`
2. `r2 = pm.get_reminder()`（同轮第二次调用）

**Then**：
- `r1 == PLAN_MODE_REFINE_REMINDER`（re-entry 优先级高于 turn0/SPARSE）
- `r2 == ""`（**标志已被消费**——同一轮第二次调用不重复注入；turns=2 非 0，turns_since_reminder=0 < 5）
- 副作用：`pm.is_reentry is False`（标志清除）

---

### 用例 13：get_reminder SPARSE 每 5 轮节奏

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 + R3 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: `turns_since_reminder >= TURNS_BETWEEN_REMINDERS` 分支 + 计数器重置副作用 |
| Oracle | golden |
| Mock | 否 |

**Given**：`pm = PlanManager()`；`pm.enter("p.md")`；先调用一次 `get_reminder()` 消费 turn0（FULL）

**When**（按序）：
1. `increment_turn()` ×5 → `r5 = get_reminder()`
2. `increment_turn()` ×4 → `r9 = get_reminder()`（turns_since=4）
3. `increment_turn()` ×1 → `r10 = get_reminder()`（turns_since=5）

**Then**：
- `r5 == SPARSE_PLANNING_REMINDER`（第 5 轮注入，计数器归零）
- `r9 == ""`（第 9 轮：距上次 4 轮，不注入）
- `r10 == SPARSE_PLANNING_REMINDER`（第 10 轮：又满 5 轮，节奏稳定为每 5 轮一次，不漂移）
- 副作用：每次 SPARSE 注入后 `turns_since_reminder == 0`

---

### 用例 14：handle_tool_result 捕获 exit_plan_mode 成功结果

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | 计划提交后必须被消费并回写审批等待消息（否则 run_core 后续分支误处理）[P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: `tool_name=="exit_plan_mode" and success` 分支 |
| Oracle | golden |
| Mock | 否（构造 `SimpleNamespace(success=True, data={...})` 纯值结果） |

**等价类划分**：result.data 形态 → 代表值 = dict / 非 dict（parametrize）

**Given**：`pm = PlanManager()`

**When**：`consumed, msg = pm.handle_tool_result("exit_plan_mode", SimpleNamespace(success=True, data={"plan_path": "p.md", "plan_content": "# 计划"}))`

**Then**：
- `consumed is True`
- `msg` 包含 `"计划已提交，等待用户批准"` 与 `"计划文件: p.md"`（golden 子串）
- 数据形态边界：`data` 非 dict（如 `SimpleNamespace(success=True, data=None)`）→ 不抛异常，`consumed is True`，消息中 `计划文件:` 后为空串

---

### 用例 15：handle_tool_result 不消费其它工具/失败结果

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | 非 exit_plan_mode 结果不得被 PlanManager 消费（防止吞掉其它工具回执）[P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: 两个否定分支 |
| Oracle | golden |
| Mock | 否 |

**等价类划分**：tool_name × success 维度 → 代表值 = (非 exit 工具, 成功) / (exit_plan_mode, 失败)（parametrize）

**Given**：`pm = PlanManager()`

**When**：
1. `pm.handle_tool_result("write_plan", SimpleNamespace(success=True, data={}))`
2. `pm.handle_tool_result("exit_plan_mode", SimpleNamespace(success=False, data={}))`

**Then**：两次均返回 `(False, "")`（不消费、无消息）

---

### 用例 16：compute_plan_hash 蜕变关系

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R12 [P3]（plan_summary 写入 session_meta 依赖它） |
| 测试层级 | unit |
| 覆盖准则 | N/A（纯函数无分支） |
| Oracle | **蜕变关系**（SHA-256 输出无法人工手算，禁止硬编码抄来的值） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：输入内容维度 → 代表值 = `"# 计划\n步骤1"` / 同一内容 / 不同内容

**Given**：无

**When**：`h1 = pm.compute_plan_hash("# 计划\n步骤1")`；`h2 = pm.compute_plan_hash("# 计划\n步骤1")`；`h3 = pm.compute_plan_hash("# 计划\n步骤2")`

**Then**（断言"关系"而非绝对值）：
- 确定性：`h1 == h2`（inv: 同输入同输出）
- 格式：`len(h1) == 64` 且 `h1` 只含 `[0-9a-f]`（SHA-256 hex）
- 抗碰撞：`h1 != h3`

---

### 用例 17：read_plan_content 委托 files.read_plan

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 + R9（无 path 时返回 None 不抛；有 path 时读取真实文件）[P2] |
| 测试层级 | integration（tmp_path 真实文件） |
| 覆盖准则 | branch: `_plan_file_path` 为空 / 非空 |
| Oracle | golden |
| Mock | 否 — 真实 tmp_path 文件 |

**Given**：
- `pm = PlanManager()`（未 enter，path=None）
- `p = tmp_path / "plans" / "p.md"`；`p.parent.mkdir()`；`p.write_text("# 计划", encoding="utf-8")`；`pm.enter(str(p))`

**When**：
1. `r_none = pm.read_plan_content()`（未 enter 的 manager）
2. `r = pm.read_plan_content()`（已 enter 指向 p）

**Then**：
- `r_none is None`
- `r == "# 计划"`

---

### 用例 18：generate_word_slug 等价类

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 + R8 [P2] |
| 测试层级 | unit（纯字符串处理） |
| 覆盖准则 | N/A |
| Oracle | golden（可人工推导） |
| Mock | 否 |

**等价类划分**：名称内容维度 → 代表值 = `"JWT 认证服务"` / `"a/b\\c"` / `""` / 超长多词

**Given / When / Then**（parametrize）：

| 输入 name | 期望 |
|---|---|
| `"JWT 认证服务"` | `"JWT-认证服务"`（中文保留、空格转连字符） |
| `"a/b\\c"` | `"a-b-c"`（分隔符类字符转连字符，无残留分隔符） |
| `""` | `"plan"`（空串兜底） |
| `"one two three four five six"`（max_words=5） | `"one-two-three-four-five"`（截断到 5 词） |

---

### 用例 19：generate_plan_file_path 穿越防护 + mkdir 副作用 + 目录优先级

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 + R6 [P1] |
| 测试层级 | integration（真实 mkdir + 路径生成） |
| 覆盖准则 | branch: plan_dir / config_home 两条路径 |
| Oracle | golden |
| Mock | 否 |

**等价类划分**：目录来源维度 → 代表值 = plan_dir 显式 / config_home 显式（parametrize）

**Given**：`plan_dir = tmp_path / "custom"`；`config_home = tmp_path / "cfg"`（均不存在）

**When**：
1. `r1 = generate_plan_file_path("../../evil", plan_dir=str(plan_dir))`
2. `r2 = generate_plan_file_path("JWT 认证", config_home=str(config_home))`

**Then**：
- `r1 == str(plan_dir / ".._.._evil.md")`——`/` 被替换为 `_`，**文件名不含路径分隔符**（`os.sep` 不在文件名中），防穿越生效
- `plan_dir` 目录被创建（`plan_dir.is_dir() is True`，mkdir 副作用）
- `r2 == str(config_home / "plans" / "JWT 认证.md")`；`config_home / "plans"` 已创建
- 优先级：传入 plan_dir 时忽略 config_home（不因两者同时传入而歧义）

---

### 用例 20：validate_plan_file_path 拒绝矩阵

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 + R7 [P1] |
| 测试层级 | unit（三个拒绝分支均在触达文件系统前返回，纯字符串/Path 逻辑） |
| 覆盖准则 | branch: `".." in file_path` / `suffix != .md` / `parent 不存在` 三个拒绝分支全覆盖 |
| Oracle | golden |
| Mock | 否 |

**等价类划分**：非法输入维度 → 代表值 = 穿越路径 / 非 .md / 父目录不存在（parametrize）

**Given / When / Then**：

| 输入 file_path | 期望 |
|---|---|
| `str(tmp_path / ".." / "x.md")`（含 `..`） | `None`（拒绝路径穿越） |
| `str(tmp_path / "plan.txt")` | `None`（非 .md 后缀） |
| `str(tmp_path / "no_such_dir" / "p.md")`（父目录不存在） | `None` |

- 副作用：均返回 `None` 且不抛异常

---

### 用例 21：validate_plan_file_path 合法路径通过 + 相对路径行为 [known-gap]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 + R7 [P1] + **[known-gap KG-2]** |
| 测试层级 | 合法路径：integration（父目录真实存在）；相对路径：unit |
| 覆盖准则 | branch: 全检查通过分支 |
| Oracle | golden |
| Mock | 否 |

**Given**：`d = tmp_path / "sub"`；`d.mkdir()`；`p = d / "p.md"`

**When**：
1. `r = validate_plan_file_path(str(p))`
2. `r_rel = validate_plan_file_path("sub/p.md")`（相对路径，cwd 有 sub 时父目录存在）

**Then**：
- `r == str(Path(str(p)).resolve())`（合法绝对路径 → 返回 resolve 后路径）
- `r_rel`：**[known-gap]** docstring 声明"必须是绝对路径"，但实现未校验 `os.path.isabs`，相对路径经 resolve 后放行返回绝对路径。**期望行为：相对路径应返回 `None`**；当前实现放行 → 下游 test-coder 落 `pytest.xfail`，修复后"意外通过"即报警

---

### 用例 22：write_initial_plan_file 写入初始内容（golden）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 + R10 [P2]（初始需求必须完整落盘） |
| 测试层级 | integration（tmp_path 真实写入） |
| 覆盖准则 | N/A |
| Oracle | golden（内容模板可人工推导） |
| Mock | 否 |

**Given**：`p = tmp_path / "plans" / "p.md"`

**When**：`write_initial_plan_file(str(p), "请实现 JWT 认证")`

**Then**：
- 文件存在，内容 == `"# 用户原始需求\n\n请实现 JWT 认证\n"`（模板逐字节 golden）
- 副作用：父目录 `tmp_path / "plans"` 自动创建

---

### 用例 23：write_plan_content 全量覆盖

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 + R10 [P2]（write_plan 语义=覆盖，非追加） |
| 测试层级 | integration（tmp_path 真实写入） |
| 覆盖准则 | N/A |
| Oracle | golden |
| Mock | 否 |

**Given**：`p = tmp_path / "p.md"`；先 `write_initial_plan_file(str(p), "旧需求")`

**When**：`write_plan_content(str(p), "# 新计划")`

**Then**：
- 文件内容 == `"# 新计划"`（**旧内容被全量覆盖**，无残留"旧需求"）
- 副作用：父目录不存在时自动创建（`write_plan_content(str(tmp_path/"a"/"b.md"), "x")` 不抛）

---

### 用例 24：read_plan 文件不存在返回 None

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 + R9 [P2] |
| 测试层级 | integration（真实 FS 查询） |
| 覆盖准则 | branch: `plan_file.exists()` 为 False |
| Oracle | golden |
| Mock | 否 |

**Given**：`p = tmp_path / "missing.md"`（不存在）

**When**：`r = read_plan(str(p))`

**Then**：`r is None`（不抛异常）

---

### 用例 25：plan_exists 三态

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 + R9 [P2] |
| 测试层级 | integration（真实 FS 查询） |
| 覆盖准则 | branch: 不存在 / 存在空 / 存在非空 |
| Oracle | golden |
| Mock | 否 |

**Given**：`p = tmp_path / "p.md"`；`p2 = tmp_path / "empty.md"`；`p3 = tmp_path / "nonempty.md"`；`p2.write_text("  ", encoding="utf-8")`；`p3.write_text("# 计划", encoding="utf-8")`

**When**：`plan_exists(str(p))` / `plan_exists(str(p2))` / `plan_exists(str(p3))`

**Then**：`False` / `False`（空白内容视为空） / `True`

---

### 用例 26：filter_tools 白名单（保留 read_only + 内置，丢弃写工具）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R5 [P1]（规划阶段泄漏写工具 → LLM 直接改代码） |
| 测试层级 | unit |
| 覆盖准则 | branch: 规则1 内置保留 / 规则2 read_only 保留 / 规则3 其余过滤 |
| Oracle | golden（按名称断言） |
| Mock | 否（构造 `SimpleNamespace(name=..., policy=SimpleNamespace(read_only=...))` 纯值工具桩） |

**等价类划分**：工具策略维度 → 代表值 = 内置 / read_only / 写工具（parametrize 输入列表）

**Given**：`tools = [
  SimpleNamespace(name="enter_plan_mode", policy=None),          # 内置
  SimpleNamespace(name="write_plan", policy=None),               # 内置（写计划文件必须保留）
  SimpleNamespace(name="exit_plan_mode", policy=None),           # 内置
  SimpleNamespace(name="ask_user", policy=None),                 # 内置
  SimpleNamespace(name="read_file", policy=SimpleNamespace(read_only=True)),   # 只读
  SimpleNamespace(name="search_tools", policy=SimpleNamespace(read_only=True)),# 只读
  SimpleNamespace(name="write_file", policy=SimpleNamespace(read_only=False)), # 写
  SimpleNamespace(name="edit_file", policy=SimpleNamespace(read_only=False)),  # 写
  SimpleNamespace(name="delete_file", policy=SimpleNamespace(read_only=False)),# 写
  SimpleNamespace(name="bash", policy=SimpleNamespace(read_only=False)),       # 写
]`；`pm = PlanManager()`

**When**：`out = pm.filter_tools(tools)`

**Then**：
- `{t.name for t in out} == {"enter_plan_mode","write_plan","exit_plan_mode","ask_user","read_file","search_tools"}`（精确白名单）
- `write_file/edit_file/delete_file/bash` **不在**结果中
- 阶段无关性：`pm.enter("p.md")` 后再次 `filter_tools` 结果一致（filter_tools 不读 phase；executing 阶段差异由 run_core 处理——见 §0）

---

### 用例 27：filter_tools 去重（同名工具只保留一个）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R5 关联（重复工具导致 LLM 看到冗余 schema，且可能绕过过滤）[P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: `name in filtered` 去重分支 |
| Oracle | golden |
| Mock | 否 |

**Given**：`tools = [SimpleNamespace(name="read_file", policy=SimpleNamespace(read_only=True))] * 3`（同名重复）

**When**：`out = pm.filter_tools(tools)`

**Then**：`len(out) == 1`（按 name 去重，保留首个）

---

## 6. Mock / Fake 决策汇总

| 依赖 | 决策 | 理由 |
|---|---|---|
| PlanManager 内部状态 | 真实现 | 被测对象本体，零 mock |
| files.py 文件 IO | 真实现 + tmp_path | 文件层行为本身就是被测目标，tmp_path 提供隔离真实 FS |
| prompt.py 常量 | 真实现（直接 import 比较） | 常量断言防误改 |
| 其它 | 不适用 | 本文件用例无外部依赖（无网络/DB/MQ） |

## 7. 已知差距（Known-Gap）清单

| 编号 | 关联用例 | 期望行为 | 实际现状 | 差距原因 |
|---|---|---|---|---|
| KG-1 | 用例 4/5/6 | 任务描述要求 `advance()` 具备"planning 阶段才可调用"守卫 | `exit()`/`reenter()` **无阶段守卫**（任何 phase 下调用都直接生效）。当前调用方仅 `run_core._handle_plan_action`（内部契约），外部无暴露面 | 任务描述的 API 不存在，守卫语义无对应物；若需守卫需新增校验并测试 |
| KG-2 | 用例 21 | 相对路径应被 `validate_plan_file_path` 拒绝（docstring 声明"必须是绝对路径"） | 相对路径经 `Path.resolve()` 后**放行**返回绝对路径 | 实现遗漏 `os.path.isabs` 校验——安全相关，建议修复 |
| KG-3 | 用例 8 | `restore_from_session_meta` 对非法 `plan_phase` 值应拒绝或归一到合法值域 | 透传任意字符串 → `is_planning()/is_executing()` 双 False 卡死态 | 无值域校验；低概率（session_meta 由内部写入），建议加校验 |

> 注：§0 中"executing 过滤掉 enter_plan_mode"等任务描述与实现的方向性差异**不属于缺陷**（实现保留 enter_plan_mode 是允许执行中重进 plan mode 的设计意图），不设 known-gap 用例。

## 8. 修订记录

| 日期 | 变更 | 理由 |
|---|---|---|
| 本次 | 初版设计 | 依据 `manager.py` / `files.py` 实际源码白盒分析；任务描述的 `advance/approve/request_changes/update_plan_content/get_state` 与 `state.py` 均不存在，按实际公开 API 设计并标注差异（§0） |
