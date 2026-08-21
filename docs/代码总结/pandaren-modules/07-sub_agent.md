# 07 — pandaren/sub_agent（子 Agent 委派基础设施）

> 模块总结 · 以代码为准（不依赖外部设计文档）· 锚点均为本次核实的 file:line
> 生成时点：2026-08-18 @ git 09b92ff

## 0. 元信息

> 模块：`pandaren/sub_agent` | 生成：2026-08-18 @ git 09b92ff | 锚点以生成时点代码为准
> 变更历史见 §11（git log 仅 2 条：Initial commit + 工厂制并发隔离改造）

## 1. 模块定位与职责（存在的意义）

**一句话**：pandaren 的「多 Agent 委派」能力——让一个 Agent 能把自己的任务拆出去给**专业子 Agent** 做，同时用**信任校验、循环检测、深度限制、审计留痕**保证"委托谁、怎么委托"全程受控。

它是三件套对称设计的第三件（`sub_agent/__init__.py:3-6`）：

| 件 | 一句话 | 回答的问题 |
|----|--------|-----------|
| Tool | 我能做什么（原子操作） | 我能直接执行什么动作 |
| Skill | 我能知道什么（知识注入） | 我该带什么知识进上下文 |
| **Agent（本模块）** | 我能委托谁（任务委派） | 什么活该交给专门的人干 |

**不建它会怎样**：所有能力挤在主 Agent 一个循环里——上下文膨胀、工具面过宽、单点 prompt 越来越复杂、无法按专业拆分。产品上是"单核 CPU vs 多核协处理器"的差别。

**角色分工**（三层职责边界）：
- `loader.py` —— 把 `.md` 蓝图（YAML Frontmatter + Markdown 正文）解析成纯数据 `SubAgentBlueprint`
- `models.py` —— 蓝图/摘要/结果/来源 4 个数据模型（全部 frozen 不可变）
- `registry.py` —— ★ 运行时核心：注册、健康、摘要注入、委派执行（信任/循环/深度/审计硬编码）
- `builder.py`（本模块外）—— 把蓝图加工成可执行的 `AgentBlueprint`（工具/Skill 过滤 + LLM merge），registry 只持有 materialize 工厂

覆盖文件与测试清单：

| 文件 | 行数 | 角色 |
|------|------|------|
| `pandaren/sub_agent/__init__.py` | 34 | 对外导出面（6 类 + 2 函数 + 1 异常） |
| `pandaren/sub_agent/models.py` | 114 | 4 个 frozen 数据模型：Summary / DelegateResult / Blueprint / Source |
| `pandaren/sub_agent/loader.py` | 353 | Markdown 蓝图加载：Frontmatter 解析 + 全字段校验 |
| `pandaren/sub_agent/registry.py` | 712 | ★ SubAgentRegistry：注册/注销/状态/摘要/委派/健康刷新 |
| `pandaren/sub_agent/exceptions.py` | 5 | SubAgentRegistrationError（注册失败专用异常） |
| `tests/test_isolation.py` | 277 | 4 tests：工厂语义 / 并发隔离 / 契约收紧 / 注册表行为回归 |
| `tests/test_llm_config.py` | 236 | 15 tests：loader 解析 + builder merge（LLM 配置） |
| `tests/real_delegate_integration.py` | 181 | 真实 LLM 集成：主 Agent 不受影响 + 委派并发隔离 |
| `tests/real_llm_config_integration.py` | 283 | 真实 LLM 集成：settings 继承/覆盖 + model 路由 |

---

## 2. 方案总览（产品视角）

### 2a. 在什么场景下解决什么问题（场景穷举）

| 场景 | 已有/缺失 | 该场景下的问题（业务语言） |
|------|-----------|---------------------------|
| 主 Agent 要派活给专业助手（代码探索/审查/测试） | 已有 | 蓝图声明 + 摘要注入 system prompt，LLM 自己判断选谁，`call_agent(agent_name, task)` 一键委派 |
| 想让子 Agent 有**自己的上下文**，不污染主对话 | 已有 | 每次委派产出全新实例（独立 Memory），run 完即弃——主 Agent 的对话历史完全不受影响 |
| 多个用户/会话并发委派同一个子 Agent | 已有 | materialize 工厂制 → 每次委派新实例，天然物理隔离（F2 并发测试验证） |
| 防止子 Agent 滥用权限（访问不该访问的工具/技能/再委派） | 已有 | 蓝图 `tools/skills/sub_agents` 三层最小权限声明 + 委派时信任等级校验（AG-S1） |
| 防止 Agent 无限互相委派（A 委派 B，B 又委派 A） | 已有 | contextvars 委派调用栈 + 循环检测（AG-S3）+ 默认最大深度 1（仅一层） |
| 想审计"谁委派了谁、为什么、结果如何" | 已有 | 6 类审计事件硬编码在关键路径（AG-S4），拒绝路径也留痕 |
| 想给子 Agent 单独指定模型/参数（贵的活用便宜的模型） | 已有 | 蓝图顶层 `model` + LLM 参数白名单字段，构建时三层 merge（父级为底 → 蓝图覆盖 → model 映射） |
| 父 Agent 被用户取消，子 Agent 还在跑 | 已有 | 父 cancel_token 经 metadata 透传，子 Agent run 入口 link 父子取消链（级联取消） |
| 想把子 Agent 定义成文件、随项目分发 | 已有 | `.md` 蓝图 + `sub_agents_from_dir()` 目录批量加载，`with_default_sub_agents()` 内置蓝图 |
| 子 Agent 之间互相委派（两层以上） | **缺失** | 默认 `max_delegate_depth=1`（registry.py:48），仅一层；更深层次是显式配置上限，代码注释明说"仅一层" |
| 蓝图声明嵌套构建子 Agent | **缺失** | models.py:93 明说"仅一层，不做递归嵌套构建"——bp.sub_agents 仅是权限声明，运行时由 registry 校验 |
| 健康检查用真实心跳/ping | **缺失** | `refresh_health()` 当前仅蓝图存在性检测（registry.py:547-569），注释标注"未来可扩展：心跳 / ping / 状态回调" |

### 2b. 总体方案思路

| 关键思路 | 回答的问题 | 核心机制 |
|---------|-----------|---------|
| 蓝图 ≠ 实例（materialize 工厂制） | "子 Agent 实例放哪，多会话并发怎么办？" | 注册时只存 materialize 工厂，委派时产全新实例用后即弃（§5-1） |
| 声明式最小权限 | "怎么保证子 Agent 只有该有的能力？" | 蓝图三层资源声明，空/`("*",)`/名单三种语义，Fail-Safe 默认空（§5-2） |
| 委派四道闸门硬编码 | "信任、循环、深度、审计怎么保证不绕过？" | `_execute_delegate` 内 Step1-8 顺序执行，全部硬编码不可配置（§5-3/4/5） |
| 摘要受 1% 上下文预算约束 | "一堆子 Agent 塞进 system prompt 会不会爆上下文？" | `build_agent_summaries()` 按 token 估算裁剪 + 仅 HEALTHY + 排除调用方自身（§5-6） |
| 取消链级联 | "父被取消子还在跑怎么办？" | 父 cancel_token 经 metadata 透传，子 run 入口 link 父子链（§5-7） |
| LLM 配置三层 merge | "子 Agent 的模型/参数怎么定？" | 父级 settings 为底 → 蓝图字段逐字段覆盖 → model 只映射 target_model（§5-8） |

---

## 3. 产品视角

### 3a. 使用场景与用户旅程

**谁**：应用开发者（通过 AgentBuilder 声明子 Agent）+ 最终用户（间接受益）。

**典型旅程**：开发者在 `.agent/` 目录写一个 `reviewer.md` 蓝图（声明身份、工具、信任等级）→ `AgentBuilder.sub_agents_from_dir(".agent")` 注册 → 用户给主 Agent 提需求"帮我审查这段代码" → 主 Agent 的 system prompt 里出现 `<available_agents>` 摘要，LLM 判断应委派 reviewer → 调用 `call_agent("代码审查专家", task)` → reviewer 以全新实例执行（独立上下文、受限工具）→ 返回结构化结果 → 主 Agent 汇总给用户。用户全程只看到"主 Agent 交给了专家处理，结果回来了"。

### 3b. 量化价值与反面案例

**反面案例（无此模块）**：
- 主 Agent 单循环承载所有专业能力 → 上下文窗口被工具 schema + 技能摘要 + 对话史占满，128K 窗口在复杂任务中频繁触发压缩（每次压缩有信息损失 + LLM 成本）。
- 多会话并发共享一个子 Agent 实例 → **实例级 Memory 互相覆盖**——这正是 test_isolation.py:3-7 记录的旧 bug：session A 的上下文被 session B 的委派 reset，产出答非所问的结果且**静默**（无报错）。
- 无信任/循环闸门 → 子 Agent 可随意委派回父级 → 无限递归（每次还花钱调 LLM），或 EXTERNAL 级调用方借子 Agent 升级权限。

**量化收益**：
- 委派隔离：每个子任务独立 Memory，主上下文只收结果（output），不吞中间过程——主对话 token 消耗 ≈ 结果大小而非任务过程。
- 并发安全：同一 registry 支撑任意 session 并发委派，零共享可变状态（工厂制）。
- 上下文预算：摘要默认 ≤1% 窗口（128K → 1280 tokens），几十个子 Agent 也塞得下。

### 3c. 产品地图定位

**能力域**：Agent 编排（多 Agent 委派）。
**上游依赖**：identity（TrustLevel/SensitivePermission 信任模型）、llm（ModelSettings）、agent（AgentStatus）、observability（AuditLog）、tool（ToolResult 契约）。
**下游服务**：engine（Phase 1 摘要注入 + ToolContext 注入 registry）、builder（装配入口）、应用层（pandapal 的 call_agent 内置 Tool）。

关系链：`identity 信任模型` → `sub_agent 委派闸门` → `engine 每轮消费摘要` → `LLM 决策委派`。

### 3d. 能力边界与承诺

**能承诺（代码强制保证）**：
1. 委派信任校验硬编码不可绕过（AG-S1）：EXTERNAL 一律不可委派（registry.py:612）
2. 循环委派被检测并拒绝 + 审计（AG-S3）
3. 委派深度有硬上限，默认仅 1 层
4. 所有委派/拒绝/完成路径写审计（AG-S4），audit_log 不可关闭（HC4）
5. 每次委派产出全新实例 → 实例间 Memory/Hooks 物理隔离
6. 蓝图 frozen 不可变，trust_level 缺失即报错不静默降级（E4）

**明确不做**：
1. 不做两层以上委派（默认深度 1；注册时也禁传 Agent 实例，registry.py:143-148 TypeError）
2. 不做递归嵌套构建（bp.sub_agents 仅是权限声明）
3. 不防提示词注入——子 Agent 收到的 task 是父 Agent LLM 转述的文本，信任边界靠 trust_level 而非内容过滤
4. 不做运行时热插拔蓝图（注册表构建期固定；`with_default_sub_agents()` 进程内幂等一次）

### 3e. 用户视角的失败体验

| 技术风险 | 用户看到 |
|---------|---------|
| 子 Agent 委派被信任校验拒绝（AGENT_DELEGATE_DENIED） | 主 Agent 回复"该助手不可委派（信任等级不足）"，任务未完成但**不崩溃**——可感知的错误而非静默 |
| 循环委派/深度超限 | 同上：显式报错 + 审计留痕，主 Agent 可换方案继续 |
| 子 Agent 蓝图加载失败（缺 when_to_use/正文） | 该蓝图被跳过（loader.py:186-192 单文件 fail-safe），其余子 Agent 正常；用户少一个可选助手 |
| 委派执行抛异常 | 包装成 `ToolResult(success=False)`（registry.py:501-510），不向上抛，主 Agent 收到"❌ Agent 执行失败" |
| 预算裁剪导致摘要不全 | 部分子 Agent 不进 system prompt，LLM 不知道它们存在 → 用户看到"主 Agent 没用上某个助手"，属配置取舍 |

### 3f. 成熟度与演进路线

**当前状态**：演进中（v0.2 刚完成工厂制并发隔离 + LLM 配置改造，f879aff 提交）。

**已知演进方向**（来自代码注释/预留位）：
- `refresh_health()` 预留"心跳 / ping / 状态回调"（registry.py:554）
- engine 的 `NextStep.HANDOFF`（Agent 间移交）为 P2 预留（engine/types.py，本模块不涉及）
- `get_agent()` deprecated stub 保留返回 None 防误用（registry.py:240-247）
- 注释掉的 logger.info（loader.py:194-197、registry.py:174-177、513-515）——曾经的信息现在静默

---

## 4. 模块整体框架

```
                       ┌─────────────────────────────────────────────┐
                       │  builder.py（模块外 · 装配层）                │
                       │  sub_agents() / sub_agents_from_dir() /      │
                       │  with_default_sub_agents()                   │
                       │  → _build_sub_agent_from_blueprint()         │
                       │    （工具/Skill 最小权限过滤 + LLM 三层 merge） │
                       └───────────────┬─────────────────────────────┘
                                       │ 产出 AgentBlueprint（materialize 工厂）
                                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        pandaren/sub_agent/                           │
│                                                                      │
│  loader.py                    models.py                              │
│  load_agent_from_file  ──►   SubAgentBlueprint (frozen 纯数据)        │
│  load_agents_from_dir ──►    SubAgentSummary / DelegateResult /      │
│  （YAML Frontmatter +        SubAgentSource                          │
│   Markdown body）                                                    │
│                               │                                      │
│                               ▼                                      │
│  registry.py ★ SubAgentRegistry                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ 注册表（A类）       运行时状态（B类）                          │   │
│  │ _factories 工厂dict  _status 健康dict                         │   │
│  │ _identities 元数据  _delegate_stack (contextvars，每 task)    │   │
│  ├──────────────────────────────────────────────────────────────┤   │
│  │ register/unregister → 审计 AGENT_REGISTERED/UNREGISTERED     │   │
│  │ build_agent_summaries → 1% 预算 + HEALTHY-only + 排除自身    │   │
│  │ call_agent(agent_name) → _execute_delegate(agent_id)         │   │
│  │   Step1 查工厂 → Step2 健康 → Step3 信任(AG-S1)              │   │
│  │   → Step4 循环(AG-S3) → Step5 深度 → Step6 审计              │   │
│  │   → Step7 materialize+run（cancel_token 级联）→ finally pop  │   │
│  │   → Step8 审计完成 → Step9 包 ToolResult                     │   │
│  │ refresh_health → 蓝图存在性检测                               │   │
│  └──────────────────────────────────────────────────────────────┘   │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ 注入 / 消费
        ┌───────────────────────┼───────────────────────────┐
        ▼                       ▼                           ▼
  engine/loop.py            engine/run_core.py          engine/message_builder.py
  Phase 1:                 ToolContext.metadata 注入     <available_agents> 摘要序列化
  build_agent_summaries    agent_registry (2402-2404)    (124-131)
  (201)
```

**读图要点**：
1. **单向装配链**：loader 产纯数据 → builder 加工成蓝图（过滤+merge）→ registry 只存工厂。registry 不解析文件、builder 不管理运行时，职责不重叠。
2. **双通道**：摘要走「system prompt 注入」（LLM 决策通道，只给 agent_name+when_to_use），执行走「内部 agent_id 精确查找」（代码通道，LLM 不感知 id）——`SubAgentSummary` 明说"agent_id 不暴露给 LLM"（models.py:43）。
3. **engine 只碰两层**：读摘要（build_agent_summaries）与注入 registry 引用（ToolContext.metadata），委派执行细节全在 registry 内部，engine 不感知信任/循环逻辑。

---

## 5. 核心机制详解

### 5-1. materialize 工厂制：委派即产新实例，用后即弃

**痛点**：子 Agent 若持有常驻实例（含实例级 Memory），多会话并发委派同一 Agent → 上下文互相 reset/覆盖——test_isolation.py:3-7 明确记录这是**真实发生过的 bug**，且表现为静默错误输出。

**机制**：`register()` 只接受有 `materialize()` 的蓝图（registry.py:143-148，传 Agent 实例直接 TypeError），把工厂存进 `_factories`。每次 `_execute_delegate` 调 `factory()` 产出**全新 Agent 实例**（独立 Memory/Hooks），run 完即弃由 GC 回收（共享 llm_client 不关）。`get_agent()` 统一返回 None 防止误用共享实例（registry.py:240-247）。

**收益**：并发隔离从"靠约定"变成"靠结构"——没有任何共享可变实例可串扰；F2 测试用 asyncio.gather 双 session 并发验证。

**代码事实**：registry.py:143-148（TypeError 契约）、registry.py:495（`target_agent = factory()`）、registry.py:16-17（设计注释）。

### 5-2. 三层资源声明：空 / `("*",)` / 名单三分语义

**痛点**：子 Agent 该有多少能力？静默继承父级全部 → 权限过宽；全禁 → 不实用。

**机制**：蓝图 `tools` / `skills` / `sub_agents` 三个字段共用一套三分语义（models.py:86-93）：
- `()` 空 → **Fail-Safe 默认**：不用工具 / 不继承 Skill / 不可委派
- `("*",)` → 继承全部（工具池 / Skill 池 / 可委派全部）
- `(name,...)` → 按名从父级池过滤，未命中的警告并忽略（builder.py:1210-1215）
`sub_agents` 特殊：它只是**权限声明**（该 Agent 可委派谁），运行时由 registry 校验，不做递归构建（models.py:93）。

**收益**：最小权限开箱即得（不写 = 没有），显式继承用星号，精确控制用名单——权限语义无歧义。

**代码事实**：models.py:111-114、builder.py:1203-1231（工具/Skill 过滤实现）。

### 5-3. 信任等级矩阵（AG-S1 硬编码）

**痛点**：委派是"能力传递"——子 Agent 能用父的工具。若 EXTERNAL 调用方也能委派高信任 Agent，等于借壳升级权限。

**机制**：`_check_trust()`（registry.py:597-629）三行规则：
- 调用方 `EXTERNAL` → **一律拒绝**（"信任等级不足"）
- 调用方 `ORCHESTRATOR` → 可委派任何 Agent
- 调用方 `SUB_AGENT` → 只能委派 `trust_level ≤ 自己`（不可向上委派）

拒绝路径**也写审计**（AGENT_DELEGATE_DENIED，registry.py:409-415），不是静默失败。

**收益**：信任单向流动——权限只能平级或向下传递，子 Agent 永远无法反向指挥更高信任级的 Agent。

**代码事实**：registry.py:612-629、审计 registry.py:409-415。

### 5-4. 循环委派检测 + 深度上限（AG-S3，contextvars 栈）

**痛点**：A 委派 B、B 又委派 A → 无限递归，每次递归都调 LLM 烧钱，直到超时。

**机制**：类级 `contextvars.ContextVar` 存委派调用栈（registry.py:68-70），每个异步 task 独立（asyncio 并发安全）。委派前查 `agent_id in stack`（registry.py:428）→ 循环即拒；`len(stack) >= max_delegate_depth`（registry.py:446，默认 1）→ 超深即拒。执行后 **finally 保证 pop**（registry.py:511-517），异常也不留残栈。拒绝路径都写审计（CYCLE / DEPTH_EXCEEDED）。

**收益**：并发下各 task 栈互不污染 + 任何异常路径栈都干净；深度默认 1 意味着子 Agent 天然无法再委派（防递归 + 防级联）。

**代码事实**：registry.py:68-70（ContextVar）、428（循环检测）、446（深度）、511-517（finally pop）。

### 5-5. 审计硬编码（AG-S4，不可绕过）

**痛点**：委派是敏感操作——谁委派谁、是否被拒、耗时多少，若走可选 hook 可能被配置关闭。

**机制**：`_write_audit_event()`（registry.py:667-703）在 6 个固定节点调用：注册 / 注销 / 状态变更 / 委派发起 / 委派完成 / 委派拒绝 / 循环 / 深度超限，全走 `AuditLog.write_sync`。audit_log 不可关闭（HC4），写入失败仅 warning 不中断主流程（registry.py:702-703，观测 Fail-Safe）。

**收益**：委派全生命周期可审计追溯，安全边界不是"配置项"而是"代码路径"。

**代码事实**：registry.py:167/193/216/464/522/409/433/451（8 个审计点）。

### 5-6. 摘要注入：1% 上下文预算 + HEALTHY-only + 排除自身

**痛点**：子 Agent 数量多时，全部塞进 system prompt 会挤占对话预算；不该暴露的（非健康、调用方自己）混入会让 LLM 选错或自委派。

**机制**：`build_agent_summaries()`（registry.py:265-316）：
- 预算 = `context_window × 1%`（默认 1280 tokens）
- 每个摘要 token 估算 = `(id+name+desc长度) / 4.0 + 5`（CHARS_PER_TOKEN=4.0，constants.py:12）
- 超出预算 → **跳过**（按 agent_id 排序后依序填充）
- 只含 `HEALTHY`；`exclude_agent_id` 排除调用方自身

**收益**：上下文开销有硬上限；LLM 只能看到"当前可用、可委派"的 Agent。

**代码事实**：registry.py:282（预算）、301（估算）、292（HEALTHY-only）、288（exclude）。

### 5-7. 取消链级联（父取消 → 子取消）

**痛点**：用户点"停止生成"，父 Agent 停了但已委派的子 Agent 还在后台跑（浪费额度 + 结果无人消费）。

**机制**：委派时从 `context.metadata["cancel_token"]` 取父 token（registry.py:478-486，run_core.py:2395 注入），经 `metadata={"parent_cancel_token": ...}` 传给子 Agent run 入口（registry.py:499），子 run 内 link 父子取消链——父取消 → 子级联取消，多层委派天然递归（代码注释 registry.py:475-477）。

**收益**：取消语义从"只停当前循环"升级为"停整棵委派树"。

**代码事实**：registry.py:478-499、run_core.py:552-553（注释印证）。

### 5-8. LLM 配置三层 merge（父级为底 → 蓝图覆盖 → model 映射）

**痛点**：子 Agent 想用不同模型/参数（贵的分析用强模型、快的探索用便宜模型），但又不该把父级全部 settings 重写一遍。

**机制**：构建时（builder.py:1291-1304）三层叠加：
1. 父级 `_llm_settings` 作底
2. 蓝图 `llm_settings` 非 None 字段**逐字段覆盖**（dataclasses.replace）
3. 顶层 `model` 字段只映射 `target_model`（避免 target_model 双入口，loader.py:28-45 白名单不含它）

loader 侧：frontmatter 顶层白名单 14 字段（_LLM_SETTINGS_FIELDS，loader.py:29-45）全部未写 → 返回 None（= 全继承父级）。

**收益**：继承默认开箱即用、覆盖按字段精确、模型路由单一入口——test_llm_config.py 15 个用例锁定语义。

**代码事实**：builder.py:1291-1304、loader.py:29-45 + 338-353。

---

## 6. 对外能力清单

### 6a. API 表（`__init__.py:20-26` 导出面）

| 符号 | 类型 | 签名要点 | 说明 |
|------|------|---------|------|
| `SubAgentSummary` | frozen dataclass | `agent_name`, `when_to_use` | 注入 system prompt 的摘要，**不含 agent_id**（models.py:43 不暴露） |
| `SubAgentDelegateResult` | frozen dataclass | `success/output/error/target_agent_id/target_run_id/duration_ms` | 委派执行结果，供 ToolResult.data（models.py:59-62） |
| `SubAgentBlueprint` | frozen dataclass | 必填 `agent_id/agent_name/when_to_use/system_prompt/trust_level`；可选 `sensitive_permissions/source/source_path/model/llm_settings/tools/skills/sub_agents` | 蓝图=纯数据，缺运行时依赖，需 Builder 加工（models.py:71-114） |
| `SubAgentSource` | IntEnum | `DIRECTORY=1 < PROGRAMMATIC=2` | 来源优先级，同 id 注册高覆盖低（models.py:24-31） |
| `SubAgentRegistry` | class | `register/unregister/set_status/drain/get_identity/get_agent/list_identities/agent_count/get_status/build_agent_summaries/call_agent/refresh_health` | 运行时核心管理器（registry.py:51） |
| `load_agent_from_file` | func | `(path, source=DIRECTORY) -> SubAgentBlueprint` | 单文件加载；缺 when_to_use/正文/trust_level 抛 ValueError（loader.py:48-155） |
| `load_agents_from_dir` | func | `(directory, source, pattern="*.md", recursive=True) -> list[Blueprint]` | 目录批量加载；单文件失败跳过（AR-FS1，loader.py:157-198） |
| `SubAgentRegistrationError` | Exception | — | 注册失败（id 重复、状态操作未注册） |

### 6b. 关键契约

1. **资源所有权**：registry 不持有常驻 Agent 实例，只持有 materialize 工厂（A 类数据）；实例用后即弃，GC 回收，共享 llm_client 不关（registry.py:13-16）。
2. **不可变**：Blueprint/Summary/DelegateResult 全部 frozen；蓝图加载后不可改（models.py:83）。
3. **Fail-Safe**：`register()` 遇无 materialize 的对象 → TypeError（registry.py:143-148）；蓝图加载失败 → 单文件跳过（loader.py:186-192）；委派异常 → ToolResult(success=False) 不向上抛（registry.py:501-510）。
4. **异常语义**：加载层 ValueError（配置错）；注册层 SubAgentRegistrationError（状态错）；委派执行层永不外抛（O3，全部转 ToolResult）。
5. **共享-独立二分**：`_identities`/`_status`/`_factories` 注册表共享只读（构造后仅 register/unregister 变更）；委派实例与委派栈（contextvars）每 task 独立。
6. **E4 无静默降级**：trust_level 缺失/非法 → ValueError（loader.py:260-277），不回落默认。

### 6c. 上下游模块清单（读 import 得出）

**上游依赖**（本模块 import 谁）：

| 依赖 | 用途 |
|------|------|
| `pandaren/identity/models` | TrustLevel（信任校验 + 蓝图必填）、SensitivePermission（permissions 解析） |
| `pandaren/llm/types` | ModelSettings（llm_settings 白名单解析） |
| `pandaren/agent` | AgentStatus（健康状态枚举，agent.py:29-32） |
| `pandaren/constants` | CHARS_PER_TOKEN（摘要 token 估算） |
| `pandaren/observability` | AuditLog + AuditEventType（审计写入） |
| `pandaren/tool/definition/tool_result` | ToolResult（委派结果包装，延迟 import） |

**下游消费**（谁 import 本模块）：

| 消费者 | 用途 |
|--------|------|
| `pandaren/builder.py` | sub_agents / sub_agents_from_dir / with_default_sub_agents / _resolve_agent_registry（418-516, 1130-1180, 1182-1308） |
| `pandaren/engine/loop.py` | Phase 1 调 `build_agent_summaries(exclude_agent_id=...)`（loop.py:201） |
| `pandaren/engine/message_builder.py` | `<available_agents>` XML 序列化（message_builder.py:124-131） |
| `pandaren/engine/run_core.py` | ToolContext.metadata 注入 agent_registry（run_core.py:2402-2404）+ cancel_token 透传（2389-2396） |
| `pandaren/tool/builtin`（AgentToolFactory） | call_agent 工具注册（builder.py:1177-1178，registry 不自注册） |

---

## 7. 关键代码与设计要点

### 7-1. `_execute_delegate` 的九步硬编码管线（registry.py:360-541）

委派不是"调一下 run"——是 9 步受控流程：查工厂 → 健康检查 → 信任校验 → 循环检测 → 深度检查 → 审计发起 → 执行（push/pop + cancel 透传）→ 审计完成 → 包装结果。每一步失败都走**显式 ToolResult + 审计**，绝不静默。这是"管得住"的集中体现——安全逻辑全部内聚在一个方法里，无散落绕过点。

### 7-2. contextvars 栈的 push/pop 对齐（registry.py:471-517）

委派栈不是普通 list——是 `ContextVar`（每 asyncio task 独立）+ **finally 中 pop 且校验栈顶是自己**（registry.py:514）。异常路径（502）也会走到 finally，保证栈不被污染。`stack_final[-1] == caller_agent_id` 的防御性校验防止栈错位。

### 7-3. 审计事件名 → AuditEventType 映射表（registry.py:681-693）

事件名与枚举用**字符串映射表**关联（8 个字符串键），而非直接传枚举。注意 `AGENT_DELEGATE_DEPTH_EXCEEDED → AGENT_DELEGATE_CYCLE`（registry.py:689）——深度超限复用了循环事件类型（见 §9 P2-2）。

### 7-4. `_check_trust` 返回 str|None 而非 bool（registry.py:597-629）

拒绝原因以**字符串**返回（None=放行），调用方直接把原因写进审计 detail + ToolResult.error。比 bool 多传一层"为什么拒绝"——审计和用户错误信息复用同一份原因，不重复维护文案。

### 7-5. `register()` 只认 materialize（registry.py:143-148）

契约收紧到"只接受蓝图"：`hasattr(blueprint, "materialize")` 检查 + TypeError 报错文案直接指导用户改用 `build_blueprint()`。engine/tests/test_cancel_resume_mock.py:57 甚至断言 `register_builtin_tools` 是死代码不得复活——历史包袱被测试钉死。

### 7-6. builder 的资源过滤：缺名警告、不静默（builder.py:1203-1231）

工具/Skill 按名过滤时，声明了但父池没有 → `logger.warning` 明确列出缺失名单（builder.py:1210-1215）。符合"降级必留痕"——过滤是预期行为，但"声明了却没给到"必须让开发者看见。

---

## 8. 数据流

### 8a. 注册链路（声明 → 可委派）

```
用户 .agent/*.md ──► load_agents_from_dir() ──► SubAgentBlueprint（纯数据）
  （Frontmatter: id/name/when_to_use/trust_level/tools/skills/sub_agents/model/参数）
                        │
                        ▼
AgentBuilder._build_sub_agent_from_blueprint()
  · 工具过滤：() 空 / ("*",) 全继承 / (name,) 按名过滤（builder.py:1203-1215）
  · Skill 过滤：对称三态（builder.py:1218-1231）
  · LLM merge：父级 settings 为底 → 蓝图字段覆盖 → model 映射 target_model（1291-1304）
  · 继承：step_guard / tool_feedback_providers / context_budget / token_estimator / observability
                        │
                        ▼
registry.register(AgentBlueprint)   ← 只存 materialize 工厂 + identity 元数据
                        │
                        ▼
engine 每轮：build_agent_summaries() → <available_agents> → LLM 感知可委派清单
```

### 8b. 委派链路（LLM 决策 → 结果回主 Agent）

```
LLM 看到 <available_agents> 摘要（只有 agent_name + when_to_use，无 agent_id）
        │ 调用 call_agent(agent_name, task)
        ▼
ToolContext.metadata["agent_registry"]（run_core.py:2402-2404 注入）
        │
        ▼
call_agent() → _find_agent_id_by_name()（大小写不敏感，仅 HEALTHY）
        │
        ▼
_execute_delegate(agent_id)：
  Step1-6 四道闸门（健康/信任/循环/深度）+ 审计   ← 失败 → ToolResult(success=False)
  Step7  factory() 产新实例 → run(task, session_id, metadata={parent_cancel_token})
           · session_id 透传父会话（registry.py:498）
           · parent_cancel_token 级联取消
           · finally 弹出委派栈
  Step8  审计 AGENT_DELEGATE_COMPLETED（success + duration）
  Step9  SubAgentDelegateResult → ToolResult(data="✅/❌ ...")
        │
        ▼
engine 工具结果写回主 Agent memory → 主 Agent 感知结果继续决策
```

### 8c. 多路径对比

| 路径 | 差异 |
|------|------|
| `run()` vs `run_stream()`（子 Agent） | 委派统一走 `target_agent.run()`（registry.py:496），流式事件不回传——委派是"黑盒调用"，主 Agent 只拿最终结果 |
| 内置蓝图 vs 用户蓝图 | 内置走 `with_default_sub_agents()`（进程内幂等一次，builder.py:483-485）；用户走 `sub_agents_from_dir()`，两者都汇入同一 `_sub_agent_blueprints` 列表 |
| 编程 API vs 目录加载 | `.sub_agents([bp])` 直接传蓝图列表；目录加载先经 `load_agents_from_dir`（loader 层）——殊途同归到 `_build_sub_agent_from_blueprint` |

---

## 9. 架构问题与风险

### P0（破坏性/数据丢失）

**无。**

### P1（高严重度）

| # | 位置 | 影响 | 建议 |
|---|------|------|------|
| P1-1 | `registry.py:498`：`session_id=getattr(context, "session_id", None) or "delegate"` | 违反 SESSION_ID 契约「0 容忍空值 / 中间层绝不创建」——若 context 缺 session_id，会以魔数 `"delegate"` 作为子 Agent 会话归属，跨会话污染且**静默**。当前链路 run_core 总会注入 session_id 故实际难触发，但代码层存在兜底分支，属定时炸弹 | 去掉 `or "delegate"`，改为 `session_id_mod.require(...)` 显式报错；或从 ToolContext 强类型字段取 |

### P2（值得改进）

| # | 位置 | 影响 | 建议 |
|---|------|------|------|
| P2-1 | `registry.py:689`：`AGENT_DELEGATE_DEPTH_EXCEEDED` 映射到 `AuditEventType.AGENT_DELEGATE_CYCLE` | 深度超限审计语义被错误归类为"循环"，排障时混淆两类拒绝原因 | 在 observability/types.py 增补 `AGENT_DELEGATE_DEPTH_EXCEEDED` 枚举成员 |
| P2-2 | `registry.py:301`：token 估算 `len(...) // 4.0 + 5` 为粗估 | 摘要预算不是精确 token 数，边界场景可能超预算或浪费 | 接入注入的 TokenEstimator（builder 已支持 token_estimator），或明示估算误差 |
| P2-3 | `loader.py:296`：非 str/list 的 tools 值"静默退回空" | 违反「降级必留痕」——写错的 YAML 类型（如 `tools: 123`）无声变无工具，难排查 | 改 logger.warning 留痕 |
| P2-4 | `registry.py:587-595`：`_find_agent_id_by_name` 同名 agent 取第一个 | 两个蓝图同名（agent_id 不同）时，LLM 感知名字一样，委派结果不确定（依赖 dict 顺序） | 注册时校验 agent_name 唯一性，或摘要中携带可区分信息 |
| P2-5 | `registry.py:155`：重复注册抛异常，无"覆盖注册"能力 | `SubAgentSource` 定义了优先级（PROGRAMMATIC > DIRECTORY）但 register 从不利用——来源枚举成了装饰品 | 按 source 优先级实现同 id 覆盖，或移除枚举降低误导 |

### P3（记录在案）

| # | 位置 | 影响 | 建议 |
|---|------|------|------|
| P3-1 | `registry.py:240-247`：`get_agent()` deprecated stub 返回 None | 调用方若误用会拿到 None 而非报错 | 保持现状（防误用设计），但文档标注清楚 |
| P3-2 | `loader.py:194-197` / `registry.py:174-177,513-515`：注释掉的 logger.info | 信息丢失，加载过程无成功日志（只留 warning） | 如需观测可恢复为 debug 级 |
| P3-3 | `loader.py:215`：frontmatter 正则要求文件**首行**即 `---` | 带 BOM 或前导空白的 .md 无法解析 frontmatter（退回整体当 body） | 解析前 strip BOM / 允许前导空行 |

---

## 10. 课程案例素材提炼

| 教学点 | 代码事实 | 一句话讲法 |
|--------|---------|-----------|
| 工厂制替代单例（并发隔离） | registry.py:143-148 + 495，test_isolation.py F1/F2 | "共享可变实例是并发 bug 之源——注册工厂、用时产出、用后即弃，隔离从结构上保证" |
| 最小权限三分语义 | models.py:111-114，builder.py:1203-1231 | "空=没有、星号=全给、名单=按名给——权限默认 Fail-Safe，显式才放开" |
| 信任单向流动 | registry.py:597-629 | "EXTERNAL 一律不给委派权，SUB_AGENT 只能平级/向下——权限只能递减不能递增" |
| ContextVar 做 per-task 状态 | registry.py:68-70 + 471-517 | "async 并发下不能用实例变量存调用栈——ContextVar 让每个 task 有独立栈" |
| 安全审计硬编码 vs 可配置 | registry.py:409-469（8 个审计点） | "敏感操作的安全记录不放在可选 hook 里——放在主路径代码上，配置关不掉" |
| 预算裁剪注入 | registry.py:265-316 | "注入 system prompt 的内容要有硬预算（1% 窗口）——防上下文被清单撑爆" |
| 取消级联透传 | registry.py:478-499 | "父级取消令牌经 metadata 传子 Agent——取消语义递归整棵委派树" |
| 三层配置 merge | builder.py:1291-1304 | "继承为底、逐字段覆盖、单字段映射——配置合并要显式三态，不静默" |

---

## 11. 验证信息与沿革

### 测试覆盖

| 测试文件 | 性质 | 覆盖 |
|---------|------|------|
| `tests/test_isolation.py`（4 tests） | 单测（假组件） | 工厂语义 / 并发隔离 / 契约收紧（TypeError）/ 注册表行为回归（唯一性、unregister 幂等、status、refresh_health、summaries 预算） |
| `tests/test_llm_config.py`（15 tests） | 单测（假组件） | loader 解析（model + 14 白名单字段）/ builder merge 语义（父底、字段覆盖、model 优先级、双来源） |
| `tests/real_delegate_integration.py` | 真实 LLM 集成 | 主 Agent 不受影响 + 双 session 并发委派真实隔离（需 DEEPSEEK/OPENAI_API_KEY） |
| `tests/real_llm_config_integration.py` | 真实 LLM 集成 | settings 继承 / 覆盖 / model 路由到真实 client（RecordingClient 探针） |

> 单测命令：`python -m pytest pandaren/sub_agent/tests/test_isolation.py pandaren/sub_agent/tests/test_llm_config.py -q`
> 集成命令见各文件 docstring（需真实 API Key，非 CI 默认）。

### 与上下篇的印证关系

- **上篇 06-engine**：engine/loop.py:201 消费 `build_agent_summaries`；run_core.py:2402-2404 注入 agent_registry；message_builder.py:124-131 序列化 `<available_agents>`——与本文 §6c 下游清单互相印证。
- **上篇 04-tool**：`ToolResult` 契约（tool/definition/tool_result.py）被 registry.py:341 延迟 import，委派结果以 ToolResult 形态回 engine。
- **上篇 01-identity**：TrustLevel/SensitivePermission（identity/models.py）是本模块信任校验与 permissions 解析的上游地基。

### 变更历史

```
f879aff feat: 子 Agent 支持显式 model/llm_settings 配置 + registry 工厂制并发隔离
c7d5e9f Initial commit
```

> 代码有变则本节可能过期——重新生成时以最新 git log 与代码为准。
