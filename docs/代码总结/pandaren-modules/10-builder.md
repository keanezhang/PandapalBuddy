# 10. pandaren/builder — AgentBuilder 链式构建 API

> 文件：`pandaren/builder.py`（1307 行）| 独立测试文件：无（覆盖散布于 pandaren/test/ 各域）
> 定位：**SDK 唯一构建入口**——16 个 fluent 方法 + 2 个构建出口（`build()` / `build_blueprint()`），把「要不要、用什么」的配置声明组装为可运行的 Agent 全栈。

---

## 1. 模块概览

**一句话**：配置收集器 + 六阶段组装器。Builder 只收集配置（全字段默认安全），`build_blueprint()` 一次性按拓扑顺序组装出 `AgentBlueprint`（含 ToolRegistry / SkillRegistry / SubAgentRegistry / HarnessExecutor / AuditLog / Memory 工厂 / Hooks 模板），`materialize()` 时才构造 AgentLoop + Agent。

**两个构建出口**：

```python
build()            = build_blueprint().materialize()   # 向后兼容：单 Agent 场景
build_blueprint()  = 6 阶段组装，返回 AgentBlueprint   # 多 session 场景（每 session 独立 Memory/Hooks）
```

**16 个 fluent 方法**：

| 域 | 方法 |
|----|------|
| Identity | `identity()`（4 字段全必填，无默认——E4 失败安全） |
| LLM | `llm()`（用哪个模型）+ `llm_settings()`（怎么调用，16 个可选调参） |
| Tools | `tools()` |
| Skills | `skills()` / `skills_from_dir()`（递归扫 SKILL.md，默认 PROJECT source） |
| Plan Mode | `plan_mode(plan_dir=)`（默认 `{cwd}/.pandaren/plans/`） |
| Sub-Agent | `sub_agents()` / `sub_agents_from_dir()` / `with_default_sub_agents()`（幂等） |
| Prompt | `system_prompt()` |
| Behavior | `behavior()` / `context_budget()` |
| Memory | `memory()`（11 个可选参数） |
| Observability | `observability()` / `hooks()`（等价） |

---

## 2. 核心设计

### 2.1 Observability 显式四态（builder.py:5-15）

| 状态 | 语义 | build 时解析 |
|------|------|-------------|
| `_UNSET`（初始） | 未调用 .observability() | log/tracer/metrics → **False（关闭）**；audit → None（静默 InMemory） |
| `False` | 显式关闭 | audit 除外（HC4）：**False → InMemory + WARNING** |
| `"mem"` | SDK 内置 InMemory 后端 | 原样传 |
| Backend 实例 | 应用层自定义后端 | 原样传 |

设计意图：**零配置零噪音**（测试/CI 不开观测）；audit 永不关闭（HC4，关它只降级为 InMemory 并告警）。

### 2.2 build_blueprint 六阶段组装（builder.py:804-877）

```
0. 前置校验        identity/llm_client 缺失即 ValueError（fail-fast）
1. 基础设施        _build_behavior_defaults + _build_observability_and_hooks（零依赖最先）
2. 工具层          ToolRegistry（预算）+ 内置工具 + 用户工具 + HarnessExecutor（hooks 先注入）
3. Skill 注册      _resolve_skill_registry（空 skill 列表 → None，不建 registry）
4. 子 Agent 注册   with_default_sub_agents()（幂等）+ _resolve_agent_registry
5. Memory 工厂     _build_memory_factory → 闭包（materialize 时每 session 独立实例）
6. 打包 Blueprint  除 AgentLoop/Agent 外的全部组件
```

**关键顺序约束**：hooks 在工具注册**之前**就绪 → `on_tool_register` 事件可被审计；SkillRegistry 的 SkillToolFactory / SubAgentRegistry 的 AgentToolFactory 在各自解析后统一注册进工具层。

### 2.3 Memory 工厂闭包（_build_memory_factory，976-1060）

- **raw_log_backend 与 db_path 互斥**（.memory() 入口 ValueError 校验）；只解析一次，多次 materialize **共享同一个 backend 实例**（后端内部按 session_id 分片）。
- `compact_threshold` 优先取 `context_window_budget.get_slot_tokens("conversation")`——**ContextWindowBudget 是上下文配额单一真相源**。
- PostCompact 的 ActiveSkillsSource 依赖 `skill_registry` 引用。

### 2.4 子 Agent 继承链（_build_sub_agent_from_blueprint，1181-1307）

| 维度 | 行为 |
|------|------|
| 工具 | 按 bp.tools 名过滤（空=无工具 / `("*",)`=全继承 / 名单=按名过滤，缺名 warning）；**工具池取已注册的完整 tool_registry**（修复了用 self._tool_list 快照时基础工具缺失的 bug） |
| Skill | 与工具对称过滤 |
| 停机守卫 step_guard | **继承**父级 |
| tool_feedback_providers | **继承**（子 Agent 写文件也受同一把尺子约束） |
| context_budget / token_estimator | **继承**（阈值与尺子一致，否则压缩触发判据退回 chars/4.0 量纲） |
| llm_settings | 父级作底 → 蓝图字段逐字段覆盖（非 None 才覆盖）→ bp.model 仅覆盖 target_model |
| hooks | **不继承**（注释明确：否则 provider 的 on_run_end 会因子 Agent 收尾触发，父 run 熔断计数被提前清掉） |
| observability | 继承父级配置（audit 特判 `_UNSET→None`，其余 `_UNSET→False`） |

### 2.5 观测底座组合（_build_observability_and_hooks，891-913）

```
CompositeAgentHooks
├── ObservabilityHooksAdapter   # 底座（先 add → 先观测）
└── 用户 hooks                  # 顶层（后 add → 后执行业务）
```

---

## 3. 与周边模块契约

| 契约点 | 内容 | 违约后果 |
|--------|------|---------|
| `build_blueprint()` 前置校验 | identity / llm_client 缺失 → ValueError | 配置遗漏被静默掩盖 |
| `with_default_sub_agents` 模块级 `_default_agents_loaded` | 同一进程只加载一次内置蓝图（pandaren/agents/） | 多 Builder 场景重复注册 |
| Memory 工厂 | raw_log_backend 与 db_path 互斥 ValueError | 语义冲突双落盘 |
| Observability 四态 | `_UNSET`/`False`/`"mem"`/实例，audit HC4 特判 | 关掉 audit = 静默失审计 |
| ContextWindowBudget | compact_threshold 单一真相源（conversation 配额） | 压缩阈值与工具预算不同量纲 |
| 子 Agent 不继承 hooks | 父 run 熔断计数不被子 Agent 收尾清掉 | 熔断提前复位 |

---

## 4. 失败模式与风险

| # | 风险 | 状态 | 说明 |
|---|------|------|------|
| 1 | **内置子 Agent 默认全量启用** | ⚠️ 观察点 | build_blueprint 步骤 4 无条件 `with_default_sub_agents()`：只要第一次调用，所有 Agent 都挂上 pandaren/agents/ 内置子 Agent 池（注册进 SubAgentRegistry）。对小模型场景是**不必要的上下文/委派面**；应用层无法通过 builder 关闭（只能依赖「已加载过」跳过，或自定义加载流程） |
| 2 | `with_default_sub_agents` 失败降级 | ⏸ 已处理 | 加载异常 → warning + return self（不阻断 build）——但意味着该 Agent 静默无内置子 Agent |
| 3 | 子 Agent 注册失败逐跳 | ⏸ 已处理 | `_resolve_agent_registry` 对每个失败蓝图 warning 后跳过，不阻断整体 build |
| 4 | Skill 注册失败逐跳 | ⏸ 已处理 | `_resolve_skill_registry` 同样 warning + 跳过（一个坏 Skill 不拖垮 Agent） |
| 5 | 独立测试缺失 | ⚠️ 观察点 | 无 test_builder.py；builder 覆盖散落在 pandaren/test/ 各域集成测试中，**组装顺序/继承链无专门的单元回归网** |

---

## 5. 关键结论

1. **Builder 是配置的「收集器」，不是运行时组件**——所有逻辑在 build 时执行一次，产物是 Blueprint；运行时（materialize 后）Builder 不再参与。
2. **显式语义贯穿全模块**：observability 四态、identity 必填、memory 互斥——配置缺失 fail-fast，不为静默掩盖留口子。
3. **单一路径**：`build()` 走 `build_blueprint().materialize()`，SDK 只维护一条组装路径，避免双实现漂移。
4. **两个观察点**：内置子 Agent 默认全量启用（无关闭开关）；builder 自身无独立测试文件。
