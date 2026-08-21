# 04 — pandaren/tool（工具子系统）

> 模块总结 · 以代码为准（不依赖外部设计文档）· 锚点均为本次核实的 file:line
> 生成时点：2026-08-18 @ git 09b92ff（本次复验：锚点抽查全部命中，测试结论复跑一致）

## 1. 模块定位与职责

**一句话**：Agent 的「手脚」——所有 Agent 能执行的外部操作（网页搜索、读写文件、执行代码、调用 API、委派子 Agent）都经工具子系统**接入、暴露给 LLM、被安全执行**。LLM 本身不直接执行操作，它只"声明要调用哪个工具、传什么参数"，由工具子系统负责放行与执行并返回结果。

承载 LLM 工具调用链路的三件核心事务：

1. **定义**：`@tool.function` 装饰器 / Markdown 文件加载器 → `Tool` 对象
2. **暴露**：每轮对话从全量注册表 → 过滤 → 裁剪 → 生成发给 LLM 的 `ToolSchema` 列表
3. **执行**：LLM 的 tool_call → 门控 → 校验 → 执行 → `ToolResult`

覆盖文件（32 个源码文件 + 2 个测试文件；每层均含 `__init__.py` 导出面）：

```
pandaren/tool/
├── types.py              枚举（ToolTier / SensitivityLevel / CircuitState / CircuitBreakerConfig）
├── exceptions.py         注册异常（ToolRegistrationError / ToolValidationWarning）
├── definition/           ① 纯数据模型层（仅依赖 identity/memory 类型）
│   ├── tool.py           Tool（核心，frozen + kw_only）
│   ├── tool_policy.py    ToolPolicy（静态安全策略）
│   ├── tool_lifecycle.py ToolLifecycle（动态阶段钩子）
│   ├── tool_result.py    ToolResult / ToolFeedback / DiscoveredToolEntry
│   ├── tool_schema.py    ToolSchema / ToolSearchResult
│   └── context.py        ToolContext（只读执行快照）
├── registry/             ② 注册中心（纯存储，无过滤/执行逻辑）
│   ├── store.py          ToolStore（CRUD + 名称唯一性 + safe_name 反向索引）
│   ├── validator.py      注册校验（必填字段 + 矛盾检测/自动修正）
│   ├── discovery.py      DiscoveryManager（DEFERRED 工具发现状态唯一管理者）
│   └── __init__.py       ★ __getattr__ 延迟导入 ToolRegistry（破循环依赖）
├── exposure/             ③ 暴露策略层
│   ├── gate_chain.py     GateChain（4 道过滤门：LLM「看到」什么）
│   ├── schema_builder.py SchemaBuilder（三段式排列 + search_tools enum 生成）
│   └── budget.py         ToolBudget（token 预算裁剪）
├── execution/            ④ 执行层
│   ├── executor.py       ToolExecutor（执行器）
│   └── guard_chain.py    GuardChain（4 道执行门：LLM「调用」什么）
├── builtin/              ⑤ 内置工具工厂（无状态，不依赖 Registry）
│   ├── protocol.py       BuiltinToolFactory Protocol
│   ├── search.py         SearchToolFactory（search_tools）
│   ├── agent.py          AgentToolFactory（call_agent）
│   ├── skill.py          SkillToolFactory（search_skills）
│   └── plan.py           PlanToolFactory（委托 pandaren.plan.tools）
├── facade.py             ⑥ Facade：ToolRegistry（组合以上组件）
├── decorator.py          @tool.function 装饰器
├── loader.py             Markdown 文件加载器
├── schema_inference.py   统一 schema 推导（签名+类型+hints+docstring）
├── safe_name.py          非 ASCII 工具名 → ASCII 安全名（MD5 哈希）
└── tests/                双轨测试：test_tool.py（real）+ test_tool_mock.py（mock）
```

**依赖方向严格单向**：`facade → {registry, exposure, execution, builtin} → definition → types/identity`。对外依赖 `identity.models`（TrustLevel / SensitivePermission）、`memory.protocols`（WorkingMemoryAccessor）与横切层 `hook.AgentHooks`（facade 注入观测钩子，hook 不反向依赖 tool），**不依赖** agent/engine/behavior 层。

---

## 2. 方案总览（产品视角）

> 本节面向非技术读者（产品经理 / 新人 / 评审人），讲清楚"这个模块为什么存在、怎么解决业务问题"。

### 2a. 在什么场景下解决什么问题（场景穷举）

| 场景 | 已有/缺失 | 该场景下的问题（业务语言） |
|------|-----------|---------------------------|
| 开发者接入新能力（搜索/读文件/跑代码/调 API） | 已有 | 几十种外部能力怎么让 LLM"认识"并正确使用？每种都要讲清"是什么、何时用、参数长什么样" |
| Agent 每轮对话暴露工具给 LLM | 已有 | 工具上百个时全量发说明书 = token 爆炸（成本线性涨）；说明书互相干扰 → LLM 选错工具概率上升 |
| 危险工具调用（删文件/执行命令/发请求） | 已有 | 提示词注入 / 幻觉可能引导调危险工具——安全靠"提示词写一句别乱来"还是代码强制？ |
| LLM 传错参数（多传幻觉参数、`20`→`"20"`、调不存在的工具） | 已有 | 一个参数错误就中断整个对话吗？ |
| 中文/emoji 工具命名 | 已有 | 部分 LLM 平台只收 ASCII，中文工具名直接 API 400 |
| 工具执行崩溃 / 同步工具卡住 | 已有 | 崩溃中断 Agent 循环（前端"死透"）；阻塞卡死事件循环，取消/超时失效 |
| 提示词注入检测（输入侧安全） | 缺失 | 工具层边界外——只防"调用名伪造与越权调用"，不审查内容；由 router / 内容安全层负责 |
| MCP / 远程工具接入 | 缺失 | decorator.py 已预留 `tool.mcp/remote` 扩展位，尚未落地 |
| 工具间编排（DAG / 组合流水线） | 缺失 | execute_tool 为单工具入口，未见编排层；当前靠 LLM 逐轮自行调度 |

### 2b. 总体方案思路

**四个字：分层解耦。** 把"工具"这件事拆成 4 个独立环节，每层只回答一个问题：

| 层 | 回答的问题 | 核心组件 |
|----|-----------|---------|
| definition 数据模型 | 工具**是什么**？ | `Tool`（身份 + 策略 + 行为三层组合） |
| registry 注册中心 | 工具**放哪、怎么管**？ | `ToolStore` / `Validator` / `DiscoveryManager` |
| exposure 暴露层 | LLM 这轮**能看到**什么？ | `GateChain` / `SchemaBuilder` / `ToolBudget` |
| execution 执行层 | 调用时**能否放行、怎么执行**？ | `GuardChain` / `ToolExecutor` |

五个关键思路（对应五个核心机制，详见 §5）：

1. **成本用"延迟加载"压**：工具分 ALWAYS（常驻 ≤15 个）与 DEFERRED（按需），未发现的 DEFERRED 只给 LLM 看一行摘要，LLM 想要时调 `search_tools` 声明，下一轮才完整暴露（机制 1）
2. **安全用"双层门控"代码强制**：暴露层 4 道门决定"看到"，执行层 4 道 Guard 决定"调用"，两层独立——"看到 ≠ 能调用"（机制 3）
3. **命名用"安全名"保兼容**：中文/emoji 工具名自动转 ASCII 安全名，规避部分 LLM 平台的 ASCII-only 限制（机制 2）
4. **可靠用"永不抛异常 + 容错"**：任何工具异常转 `ToolResult` 结果而不是中断循环；LLM 传错参数自动修正并回喂学习提示（机制 5、6）
5. **状态用"单一写入点"防漂移**：注册、发现、矛盾修正各有一个唯一写入者，冲突在注册期解决而非运行期事故（机制 7）

**收益**：每轮 token 成本恒定可控（≤15 全量 + 摘要）、安全兜底在代码不在约定、Agent 循环永不因工具崩溃中断、状态变迁全程可追溯。

---

## 3. 产品视角补充

> 面向产品经理的 6 个必答问题（汇报 / 规划 / 承诺三个工作场景），内容均有代码事实支撑。

### 3a. 使用场景与用户旅程

两类角色、三种触发时机：

| 角色 | 时机 | 做什么 |
|------|------|--------|
| 开发者 | 接入新能力 | 用 `@tool.function` 装饰器定义工具（decorator.py），`AgentBuilder.tools()` 注册，之后 Agent 每轮对话自动发现并使用 |
| 最终用户 | 对话中（无感知） | Agent 需要搜索/读文件/执行代码时自动触发工具，用户只看到"正在使用 XX 工具"的进度提示，零操作 |
| 开发者 | 排查行为异常 | 通过审计事件（on_tool_register / 执行记录）查"Agent 到底调了什么工具、传了什么参数" |

典型旅程：开发者 5 分钟定义一个工具 → 首次对话 Agent 自动暴露 → 用户提问触发执行 → 门控校验 → 结果带审计记录回流对话。

### 3b. 量化价值与反面案例

- **反面案例（没有延迟加载）**：100 个工具全量暴露，每轮 tool schema 约 100 × 300~500 token ≈ 3万~5万 token；本方案 ALWAYS ≤15 全量 + DEFERRED 摘要（一行字），每轮有硬上界（≤15 × 400 ≈ 6000），成本相差 5~8 倍；且说明书互相干扰时 LLM 选错工具概率上升（幻觉参数修正提示回喂可佐证）
- **反面案例（没有双层门控）**：LLM 幻觉 / 注入直接构造 tool_call 调用未暴露的危险工具 → 删文件、执行命令无拦截；现在 GuardChain 第 4 道 DiscoveryGuard 拦截并提示先调 search_tools（guard_chain.py）
- **量化收益**：每轮 token 有上界且稳定（deferred 摘要稳定 → system prompt 前缀缓存命中，成本再降一档）；工具崩溃不中断对话（executor.py:95-101 异常转结果）——用户免于"对话卡死重来一遍"的体验损失
- **参数容错**：LLM 传错参数自动修正并回喂提示（executor.py:129-184），用户无感，无需重新提问

### 3c. 产品地图定位

- **能力域**：Agent 能力层四件套之一（工具 / 技能 / 子 Agent / 规划）
- **上游依赖**：identity（TrustLevel 信任分级、权限）、skill（技能激活期白名单联动）、llm（平台兼容性要求，ASCII-only 限制催生 safe_name）
- **下游服务**：engine/run_core（每轮 build_tool_schemas + execute_tool）、pandapal 应用层全部 Agent 行为
- 关系链：`identity 授权 → tool 能力接入与执行 → engine 每轮消费 → 最终用户可见行为`

### 3d. 能力边界与承诺

**能承诺（代码强制保证）**：
- 危险操作被代码强制拦截（双层门控），不依赖提示词约定
- 工具执行永不中断对话（异常转 `ToolResult`，O3）
- 工具名跨 LLM 平台兼容（safe_name 转 ASCII）
- 工具调用全程留痕可审计（hooks.on_tool_register / 执行记录）

**明确不做（边界）**：
- 不检测提示词注入 / 恶意输入——输入侧安全在 router / 内容安全层；工具层只防"调用名伪造"与"越权调用"
- 不保证 LLM 一定选对工具——暴露合理 ≠ 使用正确，选错靠参数修正反馈渐进改善
- 不拦截工具内部的合法操作——bash 工具本身能删文件，安全靠权限配置（trust_level_required）而非内容审查

### 3e. 用户视角的失败体验

- **GuardChain 拦截**（工具未发现 / 越权）→ 用户看到 Agent 收到错误提示并自动改道（如先调 search_tools），对话不中断
- **工具执行崩溃** → 用户看到格式化后的错误消息（error_formatter），不是对话卡死
- **参数被修正** → 用户无感（修正提示只在 LLM 上下文，前端不展示）
- **未配置门控** → 全部工具可用（Fail-Safe Default：None 即透明）——风险是"配置缺失导致放行过宽"，运营侧需靠审计兜底

### 3f. 成熟度与演进路线

- **状态**：稳定核心——双层门控 / 延迟加载 / 单一写入点均已落地并有双轨测试（real + mock）
- **演进方向（代码线索）**：
  - decorator.py 预留 `tool.mcp/remote` 扩展位 → MCP / 远程工具接入
  - DEFERRED 发现机制 + DiscoveryManager LRU 淘汰（上限 20）→ 高频工具按使用频率自动"转正"为 ALWAYS 的雏形
  - builtin/ 工厂模式（无状态 + ctx 注入）→ 更多内置工具扩展位（web 搜索、办公套件等）

---

## 4. 模块整体框架

```
                 ┌──────────────────────────────────────────┐
                 │     入口（三种方式定义工具）                │
                 │  @tool.function │ Markdown 加载器 │ 内置工厂 │
                 │  (decorator.py) (loader.py)     (builtin/) │
                 └──────────────────┬───────────────────────┘
                                    ▼
        ┌───────────────────────────────────────────────────────┐
        │   ① definition/ 数据模型层（不可变 · 说清"工具是什么"）   │
        │   Tool（身份+策略+行为三层组合）                          │
        │   ToolPolicy(静态声明) ToolLifecycle(动态钩子)           │
        │   ToolContext(只读快照) ToolResult(永不异常结果)          │
        │   ToolSchema(发给LLM) ToolFeedback(多源反馈)             │
        └────────────────────────┬──────────────────────────────┘
                                 ▼
        ┌───────────────────────────────────────────────────────┐
        │   ② registry/ 注册中心（纯存储 · 不越界做过滤/执行）       │
        │   ToolStore  注册/注销/查询 + 唯一性 + safe_name 反向索引  │
        │   Validator  必填校验 + 矛盾检测 + 自动修正               │
        │   DiscoveryManager  DEFERRED 发现状态 · 单一写入点 + LRU  │
        └───────────────┬───────────────────────┬───────────────┘
                        ▼                       ▼
   ┌──────────────────────────────┐   ┌──────────────────────────────┐
   │   ③ exposure/ 暴露层          │   │   ④ execution/ 执行层          │
   │   LLM 这轮"看到"什么           │   │   这次调用"能否放行、怎么执行"   │
   │   GateChain   4 道过滤门(AND)  │   │   GuardChain 4 道 Guard(AND)   │
   │   SchemaBuilder 三段式排列      │   │   ToolExecutor 4 阶段生命周期   │
   │   ToolBudget  token 预算裁剪   │   │   参数清洗 + 线程池 + 异常转结果 │
   └───────────────┬──────────────┘   └───────────────┬──────────────┘
                   ▼                                  ▼
        ┌───────────────────────────────────────────────────────┐
        │   ⑤ facade.py ToolRegistry（组合门面 · 对外唯一入口）     │
        │   register_tool / build_tool_schemas / execute_tool    │
        │   update_enabled_tools / promote_to_discovered         │
        └───────────────┬───────────────────────┬───────────────┘
                        ▼                       ▼
        [engine/run_core 每轮调用]       [LLM tool_call 回调]
        → 暴露 schemas → 发给 LLM      → 门控+执行 → ToolResult
                                           → 回到 AgentLoop
```

**读图要点**：数据流是**单向的**——入口定义 → ①建模 → ②注册 → ③/④两侧双通道（暴露给 LLM / 执行 LLM 回调）→ ⑤门面统一对外；engine 只碰 ⑤，不碰内部任何组件。安全与成本分别由 ③④ 两个通道各自把关，互不依赖。

---

## 5. 核心机制详解

> 本模块最值得讲透的 7 个特殊机制，每个按「痛点 → 机制 → 收益 → 代码事实」四段式展开。

### 机制 1：DEFERRED 延迟加载 + search_tools 发现闭环（成本控制核心）

- **痛点**：工具数量增长后，每轮把全部工具 schema 发给 LLM 的 token 成本线性爆炸；几十份说明书互相干扰，LLM 选错工具概率上升。
- **机制**：工具分两档——**ALWAYS（常驻）**：≤15 个核心工具（search_tools、call_agent 等）每轮全量暴露；**DEFERRED（按需）**：其余工具初始只暴露「名称 + when_to_use 摘要」（一行字，token 极小），并出现在内置 `search_tools` 工具的**动态 enum** 里。LLM 想用时先调 `search_tools` 声明需要，发现成功后**下一轮**才以完整 schema 注入，之后可被直接调用。
- **收益**：每轮 token 成本恒定可控；高频工具随使用逐渐"转正"为完整暴露；deferred 摘要保持稳定 → system prompt 缓存命中率高（成本再降一档）。
- **代码事实**：`tier` 定义 types.py:13-20；三段式构建 schema_builder.py:66-161；search_tools 动态 enum schema_builder.py:174-195；发现状态 DiscoveryManager discovery.py:26-41。

### 机制 2：safe_name 安全命名（跨平台兼容）

- **痛点**：部分 LLM 平台（如 DeepSeek）工具名只接受 ASCII，中文/emoji 工具名直接 API 400；而中文场景开发者天然想用自然语言命名工具（`skill_天气预报`）。
- **机制**：注册时非 ASCII 名自动映射为 `namespace_<MD5 前 8 位>`（确定性、可逆）；发给 LLM 的 schema 永远 ASCII；LLM 用安全名回调时，注册中心靠 `_safe_name_index` 反向索引还原真实名。
- **收益**：命名自由 + 协议兼容兼得，零心智负担。
- **代码事实**：safe_name.py 哈希规则；`ToolStore._safe_name_index` store.py:22-27 / 反向解析 store.py:81-95。

### 机制 3：双层门控（看到 ≠ 能调用）

- **痛点**：单层过滤有漏洞——即使未暴露给 LLM 的工具，恶意/幻觉输入也可能直接构造 tool_call 名；且暴露层的过滤需要每轮动态变化（Skill 激活期、Agent 白名单）。
- **机制**：
  - **暴露层 GateChain 4 道门**（决定 LLM「看到」什么，AND 交集）：AllowList（Agent 级白名单）→ Enabled（动态开关）→ AgentWhitelist（工具级反白名单）→ SkillWhitelist（技能激活期临时约束，轮次结束自动恢复）
  - **执行层 GuardChain 4 道 Guard**（决定 LLM「调用」什么，AND 交集）：Enabled → AgentWhitelist → TrustLevel（信任等级门槛）→ Discovery（DEFERRED 未发现即拦截，提示先调 search_tools）
  - 两层独立可单测；**未配置 = 全放行**（None 透明），交集为空不会误杀全部工具
- **收益**：安全兜底从"提示词约定"变成"代码强制"；暴露层省成本、执行层保安全，职责分明。
- **代码事实**：四门实现 gate_chain.py:80-131 / filter 逻辑 gate_chain.py:148-180；GuardChain 见 guard_chain.py。

### 机制 4：静态策略 / 动态钩子分离（ToolPolicy / ToolLifecycle）

- **痛点**：安全规则如果允许写任意代码（Callable），就没法审计、没法序列化、没法复制——"这条工具的安全策略到底是什么？"无人能答。
- **机制**：`Tool` 拆成两个维度——
  - **ToolPolicy**：纯数据（敏感度/审计/可逆/白名单/熔断/只读……**零 Callable**），安全声明一目了然、可序列化、可打印
  - **ToolLifecycle**：动态钩子（is_enabled / validate_input / format_result_for_llm / error_formatter）
- **收益**：安全审计只看 Policy 一张表；行为扩展不动数据结构；Policy 还能被 validator 做静态矛盾检测。
- **代码事实**：tool_policy.py:18-59（sensitivity 无默认值强制声明 tool_policy.py:33）；tool_lifecycle.py:20-76；Tool 三层组合 tool.py:33-184。

### 机制 5：参数容错 + 学习反馈

- **痛点**：LLM 调用工具常传错——多传幻觉参数名、把 `20` 传成 `"20"`、把 `true` 传成 `"yes"`；直接报错会打断流程且 LLM 学不到正确用法。
- **机制**：执行前两步清洗——`_filter_extra_args` 剔除 schema 外的幻觉参数；`_coerce_args` 按 schema type 强转（`"20"`→20、`"true"`→True 等）；清洗后生成「[参数修正] 已忽略无效参数 x / 已把字符串转数字 y」提示**拼进结果返回**。
- **收益**：坏输入被修掉而不是中断流程；修正提示回喂给 LLM = 下一轮参数更规范（自学习闭环）。
- **代码事实**：过滤 executor.py:129-147；强转 executor.py:149-184（含 `_coerce_value` executor.py:186-217）；修正提示注入 executor.py:42-48 / 123-125。

### 机制 6：永不抛异常 + 同步工具丢线程池（O3）

- **痛点**：工具执行崩溃会中断整个 Agent 循环（前端"死透"）；同步工具（bash 等）卡住会阻塞事件循环，流式输出 / STOP 取消 / step_timeout 兜底全部失效。
- **机制**：`ToolExecutor.execute` 全程 try/except，任何异常 → `ToolResult(success=False)`，错误信息经 `error_formatter` 钩子格式化（executor.py:95-101）；同步 executor 用 `loop.run_in_executor` 丢线程池执行（executor.py:70-76）。
- **收益**：Agent 循环永不因工具崩溃中断；阻塞调用不拖垮并发，取消/超时能力保持在线。
- **代码事实**：4 阶段生命周期 docstring executor.py:3-10 / 实现 executor.py:30-127；错误格式化 executor.py:219-227。

### 机制 7：单一写入点（防状态漂移）

- **痛点**：注册、发现、状态若多处可写，会状态漂移——"谁改的？哪个版本是对的？"（DEFERRED 发现状态历史上曾有三处写入）。
- **机制**：`ToolStore` 独占注册写入；`DiscoveryManager` 独占发现状态（discover / undiscover / LRU 淘汰，上限 20）；`Validator` 集中做矛盾检测并**自动修正**（不可逆工具却声明低敏感 → 自动升 HIGH；CRITICAL → 强制 audit_required=True）。
- **收益**：状态变迁可追溯；冲突在注册期解决而非运行期事故。
- **代码事实**：discovery.py:13-93 独占；validator.py:91-108 自动修正；store 注册链路 store.py:29-75。

---

## 6. 对外能力清单（API 表）

**核心 API 表**（`ToolRegistry`，facade.py:42-395）：

| 成员 | 签名要点 | 说明 |
|------|---------|------|
| `register_tool` | `(tool: Tool, *, skip_if_exists: bool = False) -> None`（facade.py:118） | 校验+矛盾修正+唯一性，成功后 hooks.on_tool_register；注册即重建 enabled 缓存 |
| `unregister_tool` | `(tool_name: str) -> bool`（facade.py:130） | 支持 full_name / safe_name；同步清理 enabled 缓存 + discovery 状态 |
| `register_builtin_factories` | `(factories: list[BuiltinToolFactory]) -> None`（facade.py:157） | 批量注册内置工厂（Search / Plan / Skill / Agent） |
| `build_tool_schemas` | `(agent_id=None, agent_allowed_tools=None, messages=None, skill_allowed_tools=None, *, tool_schema_tokens=None) -> list[ToolSchema]`（facade.py:172） | 每轮暴露入口：ExposureContext 组装 → SchemaBuilder（GateChain 4 道门 → 三段式 → Budget 裁剪） |
| `get_deferred_summaries` | `() -> list[dict]`（facade.py:196） | 最近一次 build 缓存的未发现 DEFERRED 摘要（进 system prompt，缓存友好） |
| `get_deferred_tool_catalog` | `() -> list[dict]`（facade.py:200） | 全量 DEFERRED 目录（safe_name + when_to_use，对已发现免疫，按 safe_name 排序） |
| `promote_to_discovered` | `(tool_name: str, step_n: int) -> None`（facade.py:218） | 将 DEFERRED 标记为已发现（search_tools / skill 激活触发）；ALWAYS 静默跳过；同一轮次重复发现自动去重 |
| `execute_tool` | `(tool_name, args, context) -> ToolResult`（facade.py:234） | GuardChain → 参数清洗 → jsonschema 校验 → executor.execute；成功且 DEFERRED → discovery.discover |
| `update_enabled_tools` | `(context: ToolContext \| None = None, *, is_circuit_tripped: Any \| None = None) -> None`（facade.py:298） | 每轮并发重算所有工具 is_enabled（asyncio.gather），重建 _enabled_cache |
| `set_hooks` | `(hooks: AgentHooks) -> None`（facade.py:107） | 注入观测 hooks，只允许一次（二次抛 RuntimeError） |
| `list_tools / list_tool_names / get_tool` | `-> list[Tool] / list[str] / Tool \| None`（facade.py:343-350） | 只读查询（list_tools 返回内部副本） |
| `version / always_tools_count` | 属性（facade.py:92 / 97） | 注册版本号（脏检查用）/ ALWAYS 工具数（不含 search_tools） |

**关键契约**：

- **永不抛异常**：`execute_tool` 永远返回 `ToolResult`（success=True/False），O3 原则
- **Fail-Safe Default**：GateChain / GuardChain 未配置 = 全放行（None 即透明）；执行异常 → error 结果
- **名称唯一性**：同 namespace 内 name 唯一，重复注册抛 `ToolRegistrationError`
- **只读工具不可逆**：`read_only=True` 且 `is_reversible=False` 在 `Tool.__post_init__` 直接拒绝（tool.py:103-108）
- **非 ASCII 名必须安全化**：schema 中工具名用 safe_name（ASCII），否则 LLM API 可能 400

---

## 7. 关键代码与设计要点

### 7.1 definition/ — 纯数据模型

**`Tool`**（tool.py:33-184，frozen + kw_only）：
- 三层组合：身份层（name/description/executor）+ 规则层（ToolPolicy）+ 行为层（ToolLifecycle）
- 必填 6 项：`name, description, executor, policy, input_schema, when_to_use`；`tier` 默认 DEFERRED（tool.py:49-55）
- `__post_init__` 三件事（tool.py:80-108）：① `llm_guide` 自动追加到 description 尾部（tool.py:80-89）；② input/output schema 深拷贝为 `MappingProxyType` 防外部篡改（tool.py:91-96）；③ read_only 矛盾检查（tool.py:103-108）
- `full_name` 属性：`namespace + "_" + name`（tool.py:110-115）
- 一组 policy/lifecycle 只读快捷属性（sensitivity、agent_whitelist、is_enabled 等，tool.py:119-184）

**ToolPolicy vs ToolLifecycle 分工**（本包最重要的设计决策）：

| | ToolPolicy（静态声明，tool_policy.py:18-59） | ToolLifecycle（动态行为，tool_lifecycle.py:20-76） |
|---|---|---|
| 性质 | 纯数据（enum/bool/int/None，零 Callable） | 可调用钩子 |
| 内容 | `sensitivity`（无默认值，强制声明，tool_policy.py:33）、audit_required、is_reversible、is_idempotent、trust_level_required、agent_whitelist、sensitive_permission、max_calls_per_turn、max_output_bytes、circuit_breaker、halt_on_failure、read_only、default_result_limit、supports_offset_pagination、requires_user_interaction | `is_enabled(ctx)`（tool_lifecycle.py:38）、`validate_input(args, ctx)`（:52）、`format_result_for_llm(data, name)`（:65）、`error_formatter(exc, name)`（:76） |
| 时机 | 贯穿全程 | 暴露前/执行前/执行后/出错 4 阶段 |

**ToolResult**（tool_result.py:98-135，frozen，`success` 必填）：`data/error/halt/deduplicated/truncated/feedback/_discovered_tools/plan_complete/plan_path`。配套 `ToolFeedback`（多源反馈合并，`llm_visible` 控制「只上 UI 不进 LLM 上下文」的零打扰通道，tool_result.py:95）与 `DiscoveredToolEntry`（search_tools 的 sentinel 标记，tool_result.py:36-44）。

**ToolContext**（context.py:12-35，frozen）：`run_id/step_n/agent_id/session_id/permissions/trust_level/namespace/metadata/working_memory`——工具可读不可改。

### 7.2 registry/ — 注册中心

- **ToolStore**（store.py:19-158）：`_tools: dict[full_name → Tool]` + `_safe_name_index`（safe_name → full_name，LLM 用安全名回调时反向解析，store.py:81-95）。register 流程：必填校验 → 矛盾检测 → 唯一性 → 写入 → 索引维护（store.py:29-75）。`version` 递增供脏检查（store.py:77-79）。
- **validator**（validator.py:18-149）：必填字段缺一即抛（validator.py:18-71）；矛盾检测含**自动修正**：`is_reversible=False 且 sensitivity<HIGH` → 自动升级 HIGH（validator.py:91-99）；CRITICAL → 强制 audit_required=True（validator.py:101-108）；其余 WARNING 只警告（validator.py:112-144）。
- **DiscoveryManager**（discovery.py:13-93）：DEFERRED 工具发现状态**唯一写入点**（消除三写问题），`discover(name, step_n)` + LRU 淘汰（上限 20）+ snapshot/restore 序列化（discovery.py:26-61）。

### 7.3 exposure/ — 暴露策略层

**GateChain 4 道门**（gate_chain.py:138-190，AND 交集）：

```
AllowListGate（Agent 级白名单，gate_chain.py:80-90）
  → EnabledGate（动态开关，gate_chain.py:93-103）
  → AgentWhitelistGate（工具级反白名单，gate_chain.py:106-118）
  → SkillWhitelistGate（Skill 激活期白名单，gate_chain.py:121-131）
```

核心原则：**「未配置 = 全放行」**——门对应 context 字段为 None 即透明，只有主动配置才生效，交集为空不会误杀全部工具。轮次结束 Skill 白名单自动恢复（临时 vs 持久约束二分）。

**SchemaBuilder**（schema_builder.py:44-206）三段式排列（schema_builder.py:66-161）：

```
① ALWAYS（不含 search_tools，完整 schema）
② search_tools（带动态 enum = 当前未发现 DEFERRED 工具的 safe_name 列表，schema_builder.py:174-195）
③ DEFERRED 已发现（完整 schema）
```

未发现的 DEFERRED 工具只进 `deferred_catalog`（name + when_to_use 摘要，进 system prompt 保持缓存命中）。**关键细节**：非 ASCII schema 名将导致 LLM API 400，构建后主动告警。

**ToolBudget**（budget.py:26-83）：按 token 预算裁剪，从尾部（DEFERRED 段）裁剪，保底 `max_always_count`（默认 15，budget.py:19-20）。估算用 4 字节/token，失败回落 100 token 并 warning 留痕（budget.py:68-83，降级不静默）。

### 7.4 execution/ — 执行层

**GuardChain 4 道 Guard**（guard_chain.py）：执行前检查，返回 `ToolResult` 拒绝或 None 通过：

```
EnabledGuard → AgentWhitelistGuard → TrustLevelGuard（trust_level_required 比较）→ DiscoveryGuard（DEFERRED 未发现拦截，提示先调 search_tools）
```

**ToolExecutor**（executor.py:27-238）4 阶段生命周期（executor.py:3-10）：

```
Phase 1 Pre-Validate  → ToolLifecycle.validate_input(args, ctx)        (executor.py:51-58)
Phase 2 Execute       → tool.executor(ctx, **args)                     (executor.py:60-101)
Phase 3 Format for LLM→ __tool_format_for_llm__ / lifecycle 钩子 / str (executor.py:103-113)
Phase 4 Truncation    → max_output_bytes 字节截断                      (executor.py:115-121)
```

要点：前置清洗（`_filter_extra_args` executor.py:129-147 + `_coerce_args` executor.py:149-184）；同步工具丢线程池（executor.py:70-76）；异常全转 ToolResult（executor.py:95-101, 219-227）。

### 7.5 builtin/ — 内置工具工厂（无状态）

`BuiltinToolFactory.create_tools() -> list[Tool]`。设计要点：**executor 不闭包捕获 Registry**（消除循环依赖），运行时依赖（store/discovery/agent_registry/skill_registry）通过 `ctx.metadata[...]` 注入。4 个工厂：

| 工厂 | 工具 | tier | 亮点 |
|---|---|---|---|
| SearchToolFactory | `search_tools` | ALWAYS | 无状态 + 经 ctx 拿依赖；成功时返回 `_discovered_tools` sentinel |
| AgentToolFactory | `call_agent` | ALWAYS | sensitivity=HIGH、audit_required=True、不可逆 |
| SkillToolFactory | `search_skills` | ALWAYS | 委托 skill_registry |
| PlanToolFactory | plan 系列 | — | 委托 `pandaren.plan.tools.build_plan_mode_tools()` |

### 7.6 顶层辅助

- **decorator.py**：`tool.function()` 从 docstring + type hints 自动推导 schema（复用 schema_inference），policy 优先、sensitivity 兜底（都缺则 ValueError）。`tool` 是命名空间对象（非函数），预留 `tool.mcp/remote` 扩展位
- **loader.py**：从 Markdown（YAML frontmatter + body）加载；executor 用 `"module:function"` 动态导入；批量加载 Fail-Safe（单文件失败跳过）；frontmatter 缺失时自动从 executor 函数推导 schema
- **schema_inference.py**：`get_type_hints` + 签名 → JSON Schema；自动跳过第一个 `ToolContext` 参数（优先类型判断、回退参数名 `ctx/context/self`）；`Optional[X]` 解包；无默认值 → required
- **safe_name.py**：非 ASCII 名 → `namespace + "_" + MD5(name)[:8]`，纯 ASCII 原样返回。确定性、可逆索引（store 维护反向映射）。解决 DeepSeek 等对 tool name 的 ASCII-only 限制

---

## 8. 数据流

**注册**：

```
@tool.function / load_tools_from_file → Tool
  → ToolStore.register（必填校验 → 矛盾检测+自动修正 → 唯一性 → namespace 写入 → safe_name 索引）
  → hooks.on_tool_register 观测
```

**暴露（每轮）**：

```
ToolStore.items()
  → GateChain.filter(ExposureContext)          # 4 道门 AND 交集
  → SchemaBuilder 三段式（ALWAYS → search_tools enum → DEFERRED 已发现）
  → ToolBudget.enforce（token 裁剪，保底 15）
  → list[ToolSchema] → LLM
  + deferred_catalog（未发现 DEFERRED 摘要）→ system prompt（保持缓存命中）
```

**执行**：

```
LLM tool_call(safe_name)
  → ToolStore.get 双向解析（full_name / safe_name 索引）
  → GuardChain.check_all（4 道 Guard）
  → _filter_extra_args + _coerce_args（facade 前置，facade.py:268-269）
  → jsonschema.validate（facade._validate_args）
  → ToolExecutor.execute（4 阶段生命周期）
  → ToolResult（success=True 且 DEFERRED → discovery.discover）
```

**发现闭环**（DEFERRED 工具从「摘要」到「可调用」）：

```
未发现 DEFERRED → 摘要进 system prompt + search_tools enum 出现其 safe_name
  → LLM 调 search_tools → 返回 _discovered_tools sentinel
  → facade.execute_tool 成功分支 → DiscoveryManager.discover（单一写入点）
  → 下一轮 SchemaBuilder 注入完整 schema → 之后可被直接调用
```

---

## 9. 架构问题与风险

| 级别 | 位置 | 问题 | 影响 | 建议 |
|------|------|------|------|------|
| P0 | — | 无 | — | 双层门控 + 永不抛异常 + 单一写入点，未发现破坏性缺口 |
| P1 | pandaren/tool/tests/ | 双轨测试为**重构前旧版**：平铺参数构造 Tool（`Tool(sensitivity=...)`）、`ToolContext` 不传 `session_id`、断言 `full_name == "ns.name"`（点号），与重构版源码（policy 组合 / session_id 必填 / 下划线）全面不匹配 | 回归防护缺失：实测 `pytest pandaren/tool/tests -q` **10 failed / 5 passed**，后续工具框架改动无测试兜底 | 按重构版 API 重写测试（policy 组合 / session_id 必填 / 下划线 full_name）；需真实 LLM key 的集成用例与单元用例分离并标 skip |
| P2 | gate_chain.py:182-190 | `default()` docstring 写「5 道门」，实际 4 道（文件头 docstring 自述「4 道门分两类」） | 文档误导后续维护者 | 统一为 4 道门注释 |
| P2 | safe_name.py | docstring 示例 `"skill.天气预报" → "skill.e4d7f2a1"` 用点号分隔，但 `Tool.full_name` 实际用下划线 `skill_天气预报` → `skill_e4d7f2a1` | 文档示例与实现失配，拷贝示例易出错 | 修正 docstring 为下划线示例 |
| P2 | facade.py:268-269 | `execute_tool` 直接调 `self._executor._filter_extra_args/_coerce_args`（跨对象私有方法访问） | 耦合略重，executor 内部 API 变化会破坏 facade | 收敛为公开方法或统一到执行链 |
| P2 | facade.py:298-337 | `update_enabled_tools` 的 `is_circuit_tripped` 参数类型为 `Any` | 类型不安全 | 收敛为 `Callable[[str], bool]` |
| P3 | executor.py:36-39 + facade.py:268-269 | 参数清洗存在两个入口（facade 前置 + executor 内部重复） | 语义上双保险，但执行了两次 | 确认是否设计如此；若是，在 executor 内去掉重复入口 |
| P3 | executor.py:95-101 | 所有 `Exception` 统一捕获（`SystemExit` 等 BaseException 仍会外抛） | 正常；但 SystemExit 等会穿过 | 评估是否在故障隔离点边界明确异常白名单 |
| P2 | store.py:119 | `unregister` 用 `split(".", 1)` 拆命名空间，但 `full_name` 实际用下划线拼接（tool.py:114 `namespace + "_" + name`） | 带 namespace 工具注销后命名空间清理恒不触发（保守方向，不产生错误，但 `_namespace_registry` 残留脏数据） | 统一分隔符为下划线 |
| P3 | discovery.py:47 | `update_step(name, step_n)` 定义但全仓库无调用方 | LRU 驱逐按「首次发现」轮次而非「最近使用」轮次，长循环中反复使用同一工具会提前被逐出 | 执行成功路径调用 update_step 刷新轮次 |
| P3 | tool_policy.py:53 | `default_result_limit` 声明但无消费方（仅 grep 等工具自行使用 `supports_offset_pagination`） | 声明性字段空转，维护者误以为已生效 | 确认是否由上层（behavior/agent）消费分页；否则移除或接入 |
| P3 | safe_name.py:38-47 | `rsplit("_", 1)` 按**下划线**猜 namespace：工具名本身含下划线（如 `ns_my_tool_天气`）会被误拆；且 namespace 含非 ASCII 时输出 `ns_天气_<hash>` 仍非 ASCII，达不到 ASCII-only 目的（当前仅 ASCII namespace + 无下划线工具名场景安全） | 名称含下划线的非 ASCII 工具映射失真（低概率，当前无此类工具） | 限制 namespace 必须 ASCII；按注册时 ToolStore 维护的 namespace 元数据拆分，而非字符串猜测 |
| P3 | executor.py:225-226 | `_format_error` 内 `except Exception: pass` 静默吞掉 formatter 异常，回落默认格式但无任何留痕 | 违反「中间层不静默吞异常」红线（影响面仅错误文案格式，非逻辑路径） | 改 `logger.warning` 留痕后再回落默认格式 |

---

## 10. 课程案例素材提炼

| 教学点 | 代码事实 | 讲法 |
|--------|---------|------|
| 双层过滤（看到 vs 调用） | GateChain（exposure/）与 GuardChain（execution/）分离 | 暴露层省 token，执行层保安全，两层独立可测——LLM 幻觉调用也会被拦 |
| 延迟加载控成本 | DEFERRED + search_tools 动态 enum（schema_builder.py:174-195） | 每轮只全量暴露 ≤15 个，其余按需发现；摘要进 system prompt 保持缓存命中 |
| 单一写入点 | DiscoveryManager 独占发现状态（discovery.py:13-93） | 「同一份数据多处独立维护 = 设计缺陷」的正面案例 |
| 静态策略 vs 动态钩子 | ToolPolicy（零 Callable）vs ToolLifecycle | 安全规则可审计可序列化，行为钩子集中管理 |
| 无状态工厂破循环依赖 | BuiltinToolFactory executor 不闭包 Registry，经 `ctx.metadata` 注入 | 工厂如何避免与注册中心互相引用 |
| 永不外抛（O3） | ToolExecutor.execute 全程 try → ToolResult（executor.py:30-127） | 执行器像 agent.run 一样把异常转成结果 |
| 同步工具丢线程池 | `loop.run_in_executor`（executor.py:70-76） | 阻塞调用如何保住事件循环的取消/超时能力 |
| 防 LLM 幻觉参数 | `_filter_extra_args` + `_coerce_args` + 修正提示（executor.py:129-184） | 把 LLM 的坏输入修掉，并回喂学习信号 |
| 不可变防篡改 | MappingProxyType 深拷贝（tool.py:91-96）、frozen 全链 | 下游改不了 schema，API 契约由语言保证 |
| 自动修正矛盾声明 | validator：不可逆低敏 → 自动升 HIGH（validator.py:91-99） | 注册期纠错优于运行期事故 |

---

## 11. 验证信息

- **测试（⚠️ 与源码脱节，见 §9 P1）**：`pandaren/tool/tests/test_tool.py` + `test_tool_mock.py` 为**重构前旧版测试**——仍用平铺参数构造 Tool（`Tool(sensitivity=...)`）、`ToolContext` 不传 `session_id`、断言 `full_name == "ns.name"`（点号）。实测 `python -m pytest pandaren/tool/tests -q`：**10 failed / 5 passed**（本次通读实测；复核复跑同结果），不能作为验证依据。另注意：PANDAPAL.md 所述 `pandaren/test/` 目录实际不存在，SDK 测试均在各模块 `tests/` 子目录下
- **消费方**：`AgentBuilder.tools()`、`engine/run_core.py`（每轮 build_tool_schemas + execute_tool）、`pandaren.plan.tools`（PlanToolFactory 反向依赖）
- **校验命令**：当前无可用测试，验证以本文档锚点 + 源码通读为准；修复路径见 §9 P1（按重构版 API 重写 `pandaren/tool/tests/`）

---

## 自检记录（红线落地检查）

- ✅ file:line 锚点抽查（本次复验全命中）：facade.py 13 个方法（set_hooks:107 / register_tool:118 / unregister_tool:130 / register_builtin_factories:157 / build_tool_schemas:172 / get_deferred_summaries:196 / get_deferred_tool_catalog:200 / promote_to_discovered:218 / execute_tool:234 / update_enabled_tools:298 / get_tool:343 / list_tools:346 / list_tool_names:349）、executor.py（run_in_executor:74 / except:95 / _filter_extra_args:129 / _coerce_args:149 / _coerce_value:187 / _format_error:219）、tool.py（__post_init__:80 / llm_guide 追加:82-88 / MappingProxyType:95,100 / read_only 矛盾:103-108 / full_name:111）、tool_policy.py:33（sensitivity 无默认）、discovery.py（class:13 / discover:26 / update_step:47 / snapshot:52 / restore:56）、schema_builder.py（build:66 / _build_search_tool_schema:174）、gate_chain.py:184（「5 道门」失配属实）、store.py:119（split 点号属实）、safe_name.py docstring 点号失配属实、decorator.py mcp/remote 扩展位属实
- ✅ P0/P1 状态明确声明：P0 无；唯一 P1 = 测试与重构版源码脱节（10 failed / 5 passed，已实测复验）；P2 四条 + P3 六条（本次新补 safe_name rsplit 缺陷、executor 静默吞异常两条）
- ✅ 11 节 + 自检记录全部写出，无缺失
- ✅ 测试实际跑过：`python -m pytest pandaren/tool/tests -q` → 10 failed / 5 passed，与文档 §9 P1 声明逐字一致（复验）
- ✅ 红线对照：双层门控（GateChain/GuardChain）= 安全靠代码强制非约定；DiscoveryManager 单一写入点防状态漂移；工具异常全转 ToolResult（O3）；is_enabled 异常 fail-closed、ToolBudget 估算失败 warning 留痕 = 降级不静默
