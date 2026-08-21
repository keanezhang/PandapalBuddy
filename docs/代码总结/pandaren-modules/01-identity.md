# 01 — pandaren/identity（身份与权限地基）

> 模块总结 · 以代码为准（不依赖外部设计文档）· 锚点均为本次核实的 file:line
> 生成时点：2026-08-18 @ git 09b92ff

## 1. 模块定位与职责

**一句话**：Agent 的「身份证 + 通行证 + 数据隔离键」——一个 Agent 是谁（agent_id）、能干什么（sensitive_permissions）、可信到什么程度（trust_level），在创建瞬间被永久定格，运行时不可篡改。

它是 pandaren 四层架构（engine → behavior → capability → identity）的**最底层地基**：上层所有安全机制（PermissionGuard 权限门控、trace/审计锚点、子 Agent 信任分级）都建立在 identity 提供的三个不可变事实上。不建它会怎样：权限判断退化为字符串比较（可被内容注入伪造）、审计日志失去关联键（查不到"哪个 Agent 干了什么"）、子 Agent 信任只能靠约定。

**角色分工**：identity 只做**声明与校验**，不做**判断**——"我有哪些权限"由 Identity 声明，"这次调用能不能放行"由 behavior 层的 PermissionGuard 判断（`pandaren/behavior/permission_guard.py:28`）。身份与裁决分离，是权限体系的第一原则。

覆盖文件与测试清单：

| 文件 | 行数 | 角色 |
|------|------|------|
| `pandaren/identity/models.py` | 329 | 核心：TrustLevel / SensitivePermission / PERMISSION_ALL / Identity |
| `pandaren/identity/__init__.py` | 5 | 导出面：Identity, SensitivePermission, PERMISSION_ALL, TrustLevel |
| `pandaren/identity/tests/test_identity.py` | 765 | 真实测试（4 section，133 断言，直接运行模式） |
| `pandaren/identity/tests/test_identity_mock.py` | 428 | Mock 测试（9 个场景，23 断言） |

> ⚠️ 测试框架为自定义 TestResult（非 pytest 断言）：**必须直接运行** `python test_identity.py` 才反映断言失败，pytest 收集只能看到"有无异常"。详见 §9 P2-2。

---

## 2. 方案总览（产品视角）

> 面向非技术读者（产品经理 / 新人 / 评审人）。

### 2a. 在什么场景下解决什么问题（场景穷举）

| 场景 | 已有/缺失 | 该场景下的问题（业务语言） |
|------|-----------|---------------------------|
| 新建一个 Agent（主 Agent / 子 Agent） | 已有 | 每个 Agent 需要一张"身份证"：叫什么、能碰哪些危险操作、可信几级——一次说清，之后不许改 |
| Agent 要调用高敏感工具（删文件/执行代码/发网络请求） | 已有 | 凭"身份证"上的权限集放行/拒绝，而不是靠提示词里写一句"别乱来" |
| 系统日志 / 审计追踪一次 Agent 行为 | 已有 | 每条日志、每个审计事件都要能对到"是哪个 agent_id 干的" |
| 编排层决定"这条指令信不信"（主 Agent vs 外部来源） | 已有 | TrustLevel 三级信任：外部来源的指令只当数据看，主 Agent 的指令可完整接受 |
| 子 Agent 权限边界 | 已有 | 子 Agent 权限**独立声明**，不继承父 Agent（避免"父给谁授权子就全有"的放大攻击面） |
| 开发者想传自由字符串权限（如 `"can:delete"`） | 缺失 | 明确拒绝——权限必须是封闭枚举 6 选 N，防止拼错/伪造产生"幽灵权限" |
| Agent 运行中动态升权（自我升级） | 缺失 | 物理禁止——`__setattr__` 拦截所有运行时写入，升权 = 重建一个 Identity |
| 序列化持久化 Identity | 缺失 | 无 to_dict/from_dict；子 Agent 由 markdown 加载器解析参数后重新构造（`sub_agent/loader.py`） |

### 2b. 总体方案思路

**三个词：声明、封闭、不可变。**

| 关键思路 | 回答的问题 | 核心机制 |
|---------|-----------|---------|
| 身份是一次性声明 | "你是谁、能干什么、多可信"——创建时全量必填，无默认值 | E4 必填校验（models.py:91） |
| 权限是封闭集合 | 权限类型不可自由扩展、不接受字符串，杜绝伪造 | S2 封闭枚举（models.py:58） |
| 信任是枚举等级 | 信任来源不可伪造，不接受裸 int/str | S4 TrustLevel 枚举（models.py:39） |
| 一切不可变 | 运行时无法自我升权、无法注入字段 | HC1 __slots__ + __setattr__ 拦截（models.py:192） |
| 无权限 = 拒绝 | 空权限集天然拒绝所有高敏感操作，Fail-Safe Default | 空 frozenset 语义（models.py:285） |

---

## 3. 产品视角

### 3a. 使用场景与用户旅程

**谁**：Agent 开发者 / 主 Agent 编排者。
**时机**：构建 Agent 时（`AgentBuilder().identity(...)`）、定义子 Agent 时（markdown 蓝图）。
**典型旅程**：开发者调用 `AgentBuilder().identity(agent_id="search-agent", sensitive_permissions={NETWORK_CALL}, trust_level=SUB_AGENT)` → 拿到一张"只准发网络请求、中等信任"的身份证 → Agent 运行时每轮工具调用都凭它过 PermissionGuard 门禁 → 每次越权尝试被拒并留日志 → 审计后台可凭 `search-agent` 查到它全部行为轨迹。

### 3b. 量化价值与反面案例

- **反面案例（无本模块）**：权限用开放字符串存储，`trust_level` 用裸 int。攻击者在提示词注入里写 `trust_level=3, permissions="*"` → 字符串匹配放行 → Agent 可删任意文件、执行任意命令——**单点注入 = 全线失守**，且审计日志查不到归属（agent_id 缺失）。
- **量化收益**：
  - 篡改拦截：任何运行时写操作立即抛 `PermissionError` + warning 留痕（models.py:238-255），攻击面收敛到"构造入口"单点；
  - 权限判断 O(1)：`perm in frozenset`（models.py:297），每轮工具调用零性能负担；
  - 审计可追溯：agent_id 透传至 ObservabilityProvider（测试 4.6 实证），事故复盘可从日志一键定位责任 Agent。

### 3c. 产品地图定位

能力域：**身份与安全（L1 不可变地基）**。上游依赖：无（仅标准库 logging/enum）。下游服务：behavior（PermissionGuard 消费权限集）、engine（AgentLoop 每轮持有 Identity）、observability（agent_id 锚点）、sub_agent（信任分级）、tool（工具声明所需权限）。关系链：`identity 声明 → behavior 裁决 → engine 执行 → observability 留痕`。

### 3d. 能力边界与承诺

**能承诺（代码强制保证的）**：
1. 创建后字段物理不可变（__slots__ + __setattr__/__delattr__，models.py:192-255）——运行时无法自我升权；
2. 权限类型封闭（SensitivePermission 枚举，models.py:127-137）——自由字符串一律拒绝；
3. 信任等级类型封闭（TrustLevel 枚举，models.py:141-150）——裸 int/str 一律拒绝；
4. 必填字段缺失/空白 → ValueError，绝不静默兜底（models.py:112-122）；
5. 空权限集 = 拒绝一切 HIGH/CRITICAL 工具（Fail-Safe Default，集成测试 4.2 实证）。

**明确不做什么（边界）**：
1. 不判断"这次调用是否放行"——裁决权在 PermissionGuard（`permission_guard.py:28`），identity 只提供事实；
2. 不防提示词注入内容——权限只防"调用名伪造与越权"，不审查工具输入内容（由上层内容安全负责）；
3. 不提供运行中修改身份的能力——要改权限必须重建 Identity（设计使然）；
4. 不提供序列化——子 Agent 由加载器解析 markdown 重新构造（`sub_agent/loader.py`）。

### 3e. 用户视角的失败体验

| 技术风险 | 用户看到 |
|---------|---------|
| 某 Agent 试图越权调用工具 | 工具调用被拒（PermissionGuard deny + warning 日志），对话继续，只是那个操作没执行——不是崩溃 |
| 开发者漏填必填字段 | 构建时立刻报 ValueError（含具体字段名），不会带病运行 |
| 攻击者注入尝试篡改身份 | 注入的写入被 PermissionError 拦截，日志出现"Identity 运行时篡改尝试"警告；用户无感，但安全团队可查 |
| sensitive_permissions 传错类型 | 抛异常拒绝创建（当前 TypeError，见 §9 P2-1），Fail-Safe 成立但异常语义与契约不符 |

### 3f. 成熟度与演进路线

当前状态：**稳定地基**（自 Initial commit 起仅随权限体系演进，git log 显示单次迭代成型）。
已知演进方向：无代码注释中的 TODO；模块 docstring 明确"读操作统一归 LOW/MEDIUM，不进枚举"（models.py:73）为既有设计决策而非预留位。**演进路线：不适用**（模块刻意保持最小——身份类扩展会放大攻击面）。

---

## 4. 模块整体框架

```
                        ┌─────────────────────────────────────┐
                        │          AgentBuilder               │
                        │  .identity(agent_id, agent_name,    │
                        │    when_to_use, sensitive_perms,    │
                        │    trust_level)  ← 唯一合法入口      │
                        └──────────────────┬──────────────────┘
                                           │ 构造（builder.py:216-241）
                                           ▼
┌───────────────────────────────────────────────────────────────┐
│                  pandaren/identity/models.py                  │
│                                                               │
│  ┌─────────────┐   ┌──────────────────────────┐               │
│  │  TrustLevel │   │ SensitivePermission      │               │
│  │  (IntEnum)  │   │ (str Enum, 6 值)          │               │
│  │  1/2/3      │   │ DATA_WRITE/DELETE/...     │               │
│  └──────┬──────┘   └────────────┬─────────────┘               │
│         │                       │ frozenset 聚合              │
│         │                       ▼                             │
│         │            PERMISSION_ALL (frozenset ×6)            │
│         │                       │                             │
│         └───────────┬───────────┘                             │
│                     ▼                                         │
│        ┌──────────────────────────┐  E4 必填校验（ValueError）│
│        │ Identity (__slots__)     │ ← _validate_fields        │
│        │  5 字段 @property 只读    │    (models.py:91-161)     │
│        │  __setattr__/__delattr__ │                          │
│        │  → PermissionError 拦截   │    HC1 物理不可变          │
│        │  has_permission()        │    HC2 深度不可变          │
│        │  __eq__/__hash__/repr    │                          │
│        └────────────┬─────────────┘                          │
└─────────────────────┼─────────────────────────────────────────┘
                      │ 下游消费（只读，无权修改）
   ┌──────────────────┼──────────────────────┬───────────────────┐
   ▼                  ▼                      ▼                   ▼
PermissionGuard   AgentLoop             Observability       SubAgent
(behavior,        (engine, 持有         Provider (trace/    Registry
 permission_guard  _identity, agent_id    audit 锚点 O1)     (信任分级/
 .py:28)          作日志锚点+排除)                            exclude)
```

**读图要点**：
1. **单向依赖**：identity 零上游依赖（仅标准库），所有箭头都指向下游——它是架构图中唯一"被依赖但不依赖别人"的地基层；
2. **构造单点**：所有 Identity 只从 `AgentBuilder.identity()` / 子 Agent 加载器两条路径产生，构造后全链路只读；
3. **裁决分离**：identity 只"声明权限集合"，PermissionGuard 拿集合做三段式判断（LOW/MEDIUM 放行 → HIGH+未声明权限放行 → HIGH+具体权限查集合），两个模块互不耦合。

---

## 5. 核心机制详解

### 机制 1：HC1 物理不可变——`__slots__` + `__setattr__/__delattr__` 三重拦截

- **痛点**：Agent 身份一旦可改，运行时被注入 `trust_level=ORCHESTRATOR` 就是"自我升权"——权限体系瞬间失效。
- **机制**：① `__slots__` 声明 5 个 `_` 私有槽位，任何未声明属性（含注入的 `extra_field`）直接拒收；② 自定义 `__setattr__` 无条件抛 `PermissionError`（连"改回合法值"都拒绝）；③ 自定义 `__delattr__` 同样拦截删除。构造期间用 `object.__setattr__` 绕过拦截器完成一次性赋值。
- **收益**：篡改面收敛到构造入口单点；任何运行时写尝试 = 异常 + warning 留痕（测试 3.4-3.7 实证）。
- **代码事实**：`__slots__` 定义 `models.py:192-195`；拦截器 `models.py:238-255`；构造绕过 `models.py:224-228`；`_safe_agent_id` 防构造异常场景下拦截器日志拿不到 `_agent_id`（`models.py:257-262`）。

### 机制 2：HC2 深度不可变——frozenset + frozen enum 双层

- **痛点**：字段不可变但集合内容可变 = 假不可变（`identity.sensitive_permissions.add(...)` 能注入权限）。
- **机制**：构造时 `set/list` 一律规范化为 `frozenset`（`models.py:211-212`），枚举成员本身不可变；外部拿到的永远是 frozenset，无 `add` 方法。
- **收益**：即使拿到引用也无法改集合；修改原传入 set 不影响 Identity（测试 3.13-3.14 实证）。
- **代码事实**：规范化 `models.py:211-212`；PERMISSION_ALL 即 `frozenset(SensitivePermission)`（`models.py:84`）；property 返回 frozenset（`models.py:282-288`）。

### 机制 3：S2 权限封闭 + S4 信任封闭——枚举拒绝自由字符串

- **痛点**：开放字符串权限（`"can:delete"`、`"*"`）拼错即幽灵权限、伪造即越权；裸 int 信任等级可被任意数值伪造。
- **机制**：SensitivePermission 是 `str, Enum` 双继承封闭枚举（6 值按"做了什么"分类：DATA_WRITE/DATA_DELETE/CODE_EXEC/SYSTEM_CMD/NETWORK_CALL/MEMORY_WRITE）；TrustLevel 是 IntEnum（EXTERNAL=1/SUB_AGENT=2/ORCHESTRATOR=3，可比较不可伪造）。`_validate_fields` 逐元素检查 `isinstance`，非枚举一律 ValueError。
- **收益**：权限类型安全由编译器兜底；新增权限必须改枚举（评审可见），不能运行时发明。
- **代码事实**：SensitivePermission `models.py:58-80`；TrustLevel `models.py:39-51`；元素类型校验 `models.py:127-137`；trust_level 类型校验 `models.py:141-150`。

### 机制 4：E4 必填 fail-fast——无默认值的强校验

- **痛点**：身份字段缺一即不完整——agent_id 空则审计断链、权限缺则放行面失控、trust_level 缺则信任体系崩。静默给默认值 = 带病运行。
- **机制**：`__init__` 全关键字必填，`_validate_fields` 校验：空/纯空格字符串 → ValueError；权限元素类型 → ValueError；trust_level 类型 → ValueError。字符串统一 `.strip()` 规范化存储。
- **收益**：构建期失败而非运行期暴雷；错误信息带具体字段与合法值清单（如 `models.py:134-137`）。
- **代码事实**：`_validate_fields` `models.py:91-161`；strip 规范化 `models.py:224-226`；when_to_use 超 200 字仅 warning 不拒绝（`models.py:155-161`，见 §9 P2-3）。

### 机制 5：Fail-Safe Default——空权限 = 拒绝一切高敏感操作

- **痛点**：权限漏配（空集）的 Agent，是高危工具（删文件/执行命令）的天然泄洪口——默认放行 = 事故。
- **机制**：`frozenset()` 是**合法**的 Identity（测试 3.10 实证），但语义是"仅可用 LOW/MEDIUM 工具"；PermissionGuard 集成验证空集 + HIGH 工具 → deny（测试 4.2）。
- **收益**：权限漏配表现为"工具被拒 + 日志"，而非"工具可用了"；默认行为永远是最安全的。
- **代码事实**：空集合法（测试 `test_identity.py:354-359`）；PermissionGuard deny 集成（`test_identity.py:579-597`）。

### 机制 6：O1 trace 锚点——agent_id 贯穿观测链路

- **痛点**：日志/审计没有 agent 归属，出事故查不到"哪个 Agent 干的"。
- **机制**：agent_id 是 5 字段之一（必填），AgentLoop 用它作日志锚点（`engine/loop.py:166`）、委派时作排除键（`engine/loop.py:202`）；ObservabilityProvider 透传至 Logger/AuditLog（集成测试 4.6 实证）。
- **收益**：一条事故线索可从审计日志反查完整 Agent 行为链。
- **代码事实**：`engine/loop.py:166,202`；集成测试 `test_identity.py:687-699`。

### 机制 7：S3 权限不继承——身份独立声明

- **痛点**：子 Agent 若继承父 Agent 权限，"父授权即子全有" = 权限放大攻击面。
- **机制**：Identity 无 `parent_id`/`inherits_from` 字段（测试 3.21 断言不存在）；每个身份独立声明权限集，PermissionGuard 对父子分别判定（测试 4.3 实证父 allow / 子 deny 同一 DATA_WRITE）。
- **收益**：权限边界 = 身份边界，不可穿透。
- **代码事实**：docstring 声明 `models.py:184`；集成实证 `test_identity.py:599-612`。

---

## 6. 对外能力清单

### API 表

| API | 签名要点 | 说明 |
|-----|---------|------|
| `Identity.__init__` | `(*, agent_id: str, agent_name: str, when_to_use: str, sensitive_permissions: frozenset\|set\|list, trust_level: TrustLevel)` | 全关键字必填；E4 校验；set/list 自动转 frozenset |
| `Identity.agent_id` | `@property → str` | trace/审计关联键（O1） |
| `Identity.agent_name` | `@property → str` | 仅展示用，不参与逻辑 |
| `Identity.when_to_use` | `@property → str` | 调度描述，供 orchestrator 路由（≤200 字建议） |
| `Identity.sensitive_permissions` | `@property → frozenset[SensitivePermission]` | 权限集，深度不可变 |
| `Identity.trust_level` | `@property → TrustLevel` | 静态信任等级 |
| `Identity.has_permission(perm)` | `→ bool` | `perm in sensitive_permissions`（models.py:295-297） |
| `Identity.__eq__/__hash__` | 5 字段全等比较 | 支持 set/dict 去重（SubAgentRegistry 使用） |
| `TrustLevel` | `IntEnum`（1/2/3） | 支持大小比较、是 int 但创建时拒绝裸 int |
| `SensitivePermission` | `str, Enum`（6 值） | 封闭权限类型 |
| `PERMISSION_ALL` | `frozenset[SensitivePermission]` | 全权限常量（主 Agent 用） |

### 关键契约

- **资源所有权**：Identity 持有自己的全部数据，无共享可变状态；frozenset 引用可安全共享。
- **不可变**：HC1（字段）+ HC2（集合深度）双层物理不可变；唯一写点 = 构造期 `object.__setattr__`。
- **异常语义**：必填缺失/类型错误 → `ValueError`（契约）；⚠️ 实际 `sensitive_permissions=None`/含 dict 时因 frozenset 规范化先执行而抛 `TypeError`（见 §9 P2-1）；篡改 → `PermissionError`。
- **Fail-Safe**：空权限集 = 拒绝全部 HIGH/CRITICAL 工具（默认最安全）。
- **共享-独立二分**：权限独立声明（S3），无继承。

### 上下游模块清单（读 import 得出）

**上游（本模块依赖）**：无——仅标准库 `logging`/`enum`（models.py:25-27）。**这是全 SDK 唯一零依赖的核心模块**。

**下游（消费 identity，grep 实证）**：

| 消费方 | 消费内容 | 用途 |
|--------|---------|------|
| `pandaren/builder.py:52` | Identity 全量 | AgentBuilder.identity() 构造入口 |
| `pandaren/agent/agent.py:20` | Identity | Agent 持有身份 |
| `pandaren/agent/blueprint.py:51` | Identity | AgentBlueprint 声明 |
| `pandaren/behavior/permission_guard.py:19` | SensitivePermission | 权限裁决（核心消费） |
| `pandaren/engine/loop.py:36` | Identity | AgentLoop 持有；日志锚点 + 委派排除 |
| `pandaren/sub_agent/{loader,models,registry}.py` | TrustLevel / SensitivePermission / Identity | 子 Agent 信任分级与蓝图 |
| `pandaren/tool/{loader.py:20, definition/context.py:8, definition/tool_policy.py:15}` | SensitivePermission / TrustLevel | 工具声明所需权限 |
| `pandaren/tools/file_tool/delete_file.py:12` | SensitivePermission | 内置工具声明 DATA_DELETE |
| `pandapal/local/run_local.py:398` | PERMISSION_ALL / TrustLevel | 应用层装配（pandapal 侧） |

---

## 7. 关键代码与设计要点

1. **构造绕行 `object.__setattr__`**（`models.py:224-228`）：初始化值必须绕过自定义 `__setattr__`（否则连构造都被自己的拦截器拒绝）。这是"物理不可变"实现的巧妙处——拦截器对所有外部写入生效，内部用 object 级原语完成唯一一次合法赋值。配合 `_safe_agent_id`（`models.py:257-262`）处理"构造失败中途触发拦截器日志"的边界（`_agent_id` 尚不存在时返回 `"<uninitialized>"` 而非抛二次异常）。

2. **校验先于落值**（`models.py:214-221`）：`_validate_fields` 在赋值前完成全部校验，保证 Identity 要么完整创建、要么不创建——不存在"半初始化"状态可被观测到。**但注意**：frozenset 规范化（`:211-212`）在 `_validate_fields` 之前执行，导致 None/含 dict 抛 TypeError 而非 ValueError（§9 P2-1 的根源）。

3. **枚举双继承类型设计**（`models.py:58, 39`）：`SensitivePermission(str, Enum)` 让权限值可直接序列化为字符串、同时保有枚举的类型检查；`TrustLevel(IntEnum)` 让信任可比较（`EXTERNAL < SUB_AGENT < ORCHESTRATOR`）且可哈希——但 `isinstance(TrustLevel.SUB_AGENT, int) == True` 是双刃剑（§9 P3-1）。

4. **`__eq__/__hash__` 五字段全等**（`models.py:301-319`）：Identity 可作 set/dict key（SubAgentRegistry 去重依赖）。五字段全等保证"相同声明 = 相同身份"，但新增字段时必须同步维护这两个方法（§9 P2-4）。

5. **拦截器带安全日志**（`models.py:238-255`）：篡改尝试不是静默拒绝——`logger.warning` 记录 agent_id + 字段名，让"谁尝试改自己"可审计（HC4 审计不关闭的落地之一）。

---

## 8. 数据流

### 链路 1：创建链路（唯一合法入口）

```
AgentBuilder.identity(5 参数)         builder.py:216
  → Identity.__init__                  models.py:197
      → frozenset 规范化（set/list→frozenset）  models.py:211  ⚠️ TypeError 风险点
      → _validate_fields（4 段校验）            models.py:91
      → object.__setattr__ 落值 ×5             models.py:224
  → Agent/AgentBlueprint 持有           agent/blueprint.py:51
  → AgentLoop._identity                engine/loop.py:108
```
每层转换：参数 →（类型规范）→（校验通过/ValueError）→（strip 后不可变字段）。消费方只读。

### 链路 2：权限裁决链路（每轮工具调用）

```
AgentLoop 每轮工具选择
  → PermissionGuard.check_permission(identity.sensitive_permissions,
       tool.sensitivity, tool.permission)     permission_guard.py:28
      ① tool_sensitivity ≤ MEDIUM → "allow"          :47
      ② HIGH/CRITICAL 且 permission=None → "allow"   :51
      ③ permission ∈ sensitive_permissions → "allow" :55
      否则 → "deny" + warning                        :58
  → 结果驱动 Harness 放行/拒绝工具执行
```
数据转换：权限集合（frozenset）+ 工具敏感度（枚举）→ 三段式布尔裁决 → allow/deny 字符串。

### 链路 3：trace/审计链路

```
Identity.agent_id
  → AgentLoop 日志锚点（cancel 日志 agent_id=%s）    engine/loop.py:166
  → 委派排除键 exclude_agent_id                    engine/loop.py:202
  → ObservabilityProvider(agent_id=...) → Logger/AuditLog  测试 4.6 实证
```

### 链路 4：子 Agent 定义链路

```
子 Agent markdown 蓝图
  → loader._parse_trust_level("ORCHESTRATOR") → TrustLevel   测试 4.5
  → loader._parse_sensitive_permissions("code_exec, ...") → frozenset
  → SubAgentBlueprint → Identity 构造（身份独立声明，S3）
```

**多路径对比**：主 Agent 与子 Agent 的 Identity 走不同构造入口（builder vs markdown loader），但都汇入同一 `Identity.__init__` 强校验——保证"身份必须经过同一道门"。

---

## 9. 架构问题与风险

**P0（破坏性/数据丢失）：无。**
核心不可变性由 `__slots__` + 拦截器物理保证，未发现可绕过路径；权限封闭与 Fail-Safe 均经测试实证。

**P1（高严重度）：无。**

**P2（值得改进）：**

| # | 位置 | 影响 | 建议 |
|---|------|------|------|
| P2-1 | `models.py:211-212` | `sensitive_permissions=None` → `frozenset(None)` 抛 `TypeError`；含 dict 元素 → `TypeError(unhashable)`——与 E4 契约（ValueError）不符。**实测 2 条测试失败**（`test_identity.py` 131/133）。虽 Fail-Safe 成立（都拒绝创建），但调用方按 ValueError 捕获会漏接 | 规范化前先 `isinstance(sensitive_permissions, (frozenset, set, list))` 检查，不符即抛 ValueError；或将 frozenset 转换包进 try 转 ValueError |
| P2-2 | `tests/test_identity.py:47` | 自定义 TestResult 框架：pytest 收集时断言失败被吞进 result，**pytest 通过 ≠ 断言全过**（本次实测 pytest 13 passed 但直接运行 2 失败）。新断言易静默失效 | 迁移为 pytest 原生断言（`assert` + `pytest.raises`），或将断言失败 raise 出测试函数 |
| P2-3 | `models.py:155-161` | when_to_use > 200 字仅 warning 不拒绝——超长描述推高 orchestrator 路由 prompt，可能超 context 窗口（性能/质量风险，非安全） | 保持当前 warning 策略（拒绝会破坏兼容），但建议在 builder 层增加硬上限选项 |
| P2-4 | `models.py:301-319` | `__eq__/__hash__` 与 5 字段硬耦合——将来加字段漏更新两个方法会破坏 set/dict 语义 | 加字段时同步更新（可加注释提醒）；或用 dataclass(frozen=True) 重构自动生成 |

**P3（记录在案）：**
- `models.py:39` TrustLevel 是 IntEnum，`isinstance(TrustLevel.SUB_AGENT, int) == True`——下游若用 `== 2` 比较可绕过类型检查（identity 自身强制 isinstance，风险在消费方编码习惯）。
- 无序列化接口（to_dict/from_dict）——子 Agent markdown 加载器需自行解析字符串再构造，格式错误在加载期才暴露（测试 4.5 覆盖了 `_parse_trust_level` 的 ValueError 路径）。
- when_to_use 校验阈值 200 为模块内魔法数字（`models.py:32`），未收编 constants.py（模块内单点，影响小）。

---

## 10. 课程案例素材提炼

| 教学点 | 代码事实 |
|--------|---------|
| **「物理不可变」怎么实现**（面试高频） | `__slots__` + `__setattr__/__delattr__` 拦截 + `object.__setattr__` 构造绕行（models.py:192-255） |
| **枚举双继承的妙用**（str,Enum / IntEnum） | 权限可序列化 + 类型安全；信任可比较 + 不可伪造（models.py:39-80） |
| **Fail-Safe 默认值设计** | 空权限集 = 拒绝高敏感（非放行），默认行为永远最安全（测试 4.2） |
| **身份与裁决分离**（单一职责） | Identity 只声明、PermissionGuard 只裁决，两模块零耦合（permission_guard.py:28 vs models.py:295） |
| **校验先于落值**（无中间状态） | `_validate_fields` 在赋值前完成，要么完整创建要么不创建（models.py:214-221） |
| **自定义测试框架的坑**（测试基建教训） | pytest 收集不到 TestResult 断言失败——"通过"是假象（test_identity.py:47，§9 P2-2） |
| **异常语义契约一致性** | 类型错误应统一 ValueError；frozenset 规范化在前的 TypeError 是反例（models.py:211，§9 P2-1） |

---

## 11. 验证信息与沿革

### 测试覆盖（实测运行 2026-08-18）

| 测试文件 | 运行方式 | 结果 |
|---------|---------|------|
| `test_identity.py`（4 section：trust_level / permission / identity / integration） | `python test_identity.py` | **131 通过 / 2 失败 / 133 总**（2 失败为 P2-1 异常语义） |
| `test_identity_mock.py`（9 场景：validate_fields / logger / tamper_log / inject_exception / permission_guard / when_to_use_warning / agent_builder / safe_agent_id / audit_chain） | `python test_identity_mock.py` | 23 通过 / 0 失败 |
| pytest 收集模式 | `python -m pytest pandaren/identity/tests/ -q` | 13 passed（⚠️ 不反映断言级失败，见 P2-2） |

覆盖约束映射：HC1/HC2/E4/S1/S2/S3/S4/O1 均有直接断言；集成测试覆盖 PermissionGuard 三段式裁决、AgentBuilder 构造、SubAgentBlueprint 解析、ObservabilityProvider 透传。

### 与上下篇印证关系

- **上篇（无）**：identity 为地基层，无更底层模块。
- **下篇**：`04-tool.md`（pandaren/tool）中工具声明 `SensitivePermission`（`tool/loader.py:20`、`tool/definition/tool_policy.py:15`）——工具"要求什么权限"与 identity"持有什么权限"在此对接，由 PermissionGuard 裁决；`sub_agent` 模块消费 TrustLevel 做信任分级（`sub_agent/registry.py:377`）。
- **沿革**：模块自 Initial commit（c7d5e9f）即成型，最近 commit 09b92ff 未触碰 identity（`git log -- pandaren/identity` 仅 2 条：Initial commit + 空）。代码若有变更，本节可能过期。

---

## 自检记录（红线落地检查）

- ✅ file:line 锚点抽查：`models.py:211`（frozenset 规范化）、`models.py:238`（__setattr__ 拦截）、`permission_guard.py:28`（check_permission）、`builder.py:216`（identity 方法）均实测存在
- ✅ P0/P1 状态明确声明（均"无"，P2 四条 + P3 三条给出）
- ✅ 11 节全部写出，无缺失
- ✅ 方案总览（产品视角）写出：场景穷举表（已有/缺失均有依据）+ 总体方案思路 5 条
- ✅ 模块整体框架 ASCII 图画出：分层 + 数据流方向 + 关键组件，全部基于真实代码结构
- ✅ 核心机制详解覆盖 7 个特殊机制，每条含痛点 → 机制 → 收益 → 代码事实
- ✅ 产品视角补充 4 项必查：场景旅程（3a）/ 量化价值与反面案例（3b）/ 能力边界（3d）/ 失败体验（3e）；产品地图定位（3c）、成熟度演进（3f）有依据写明
- ✅ 测试实际跑过：直接运行两测试文件，131/133 + 23/23，2 处失败如实记录于 §9 P2-1
