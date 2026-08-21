# Identity 层 pytest 测试设计（R.I.S.K.-Driven）

> 用途：本文档为 **test-coder 的输入**——`pandaren/identity/models.py` 的测试设计。
> 每条用例含 Given/When/Then、等价类代表值、Oracle、副作用断言、known-gap 声明，可直接落成 pytest 代码。
>
> 设计依据：① 被测源码 `pandaren/identity/models.py`（342 行，白盒通读）；② 模块 docstring 硬约束 HC1/HC2/E4/S2/S3/S4/O1；
> ③ `docs/reviews/pandaren-identity-review.md`（R1/R2/R3 风险来源）；④ 任务提供的 10 条硬约束。
>
> 所有 oracle 值均已在本机对运行中的实现做过验证（脚本实测），非「跑一遍抄来的」——见 §4 Oracle 策略。

---

## 0. 设计元信息

| 项 | 值 |
|----|----|
| 测试框架 | pytest（pyproject.toml `[tool.pytest.ini_options]`：`testpaths=["pandapal","pandaren","scripts"]`、`asyncio_mode="auto"`） |
| 测试层级 | **全部 unit**（无跨进程/真实 I/O；见 §5 层级判定） |
| Mock/Fake | **零 mock**——纯数据模型无外部依赖；日志断言用 pytest 内置 `caplog` fixture |
| 故障注入 | 不适用（无网络/DB/MQ/磁盘依赖），模块内故障路径（TypeError→ValueError、未初始化 AttributeError 吸收）以用例 C08/C14 显式覆盖，见 §5 |
| 用例总数 | 25（P0=9，P1=12，P2=4；含 2 条 known-gap xfail） |
| 覆盖准则 | 分支覆盖（branch）为默认目标；MC-DC/路径覆盖不适用（理由见 §1） |
| 状态机 | 不适用（Identity 非状态机，无非法转换面；不可变性由 HC1 单独覆盖） |
| 确定性控制 | 无时间/随机/浮点/时区依赖；唯一集合顺序点（`logger.info` 中的 `sorted`）在断言中按「集合相等」处理，不依赖顺序 |

---

## 1. 白盒分析摘要（分支清单 + 覆盖准则）

### 1.1 被测结构

```
pandaren/identity/models.py
├── TrustLevel(IntEnum)               EXTERNAL=1 / SUB_AGENT=2 / ORCHESTRATOR=3
├── SensitivePermission(str, Enum)    DATA_WRITE/DATA_DELETE/CODE_EXEC/SYSTEM_CMD/NETWORK_CALL/MEMORY_WRITE
├── PERMISSION_ALL                    frozenset(SensitivePermission)，恒 6 项
├── _validate_fields(*, ...)          纯校验函数：4 个校验阶段 + 1 个长度警告
└── Identity                           __slots__×5 + 不可变拦截 + 5 只读 @property + has_permission + __eq__/__hash__/__repr__
```

### 1.2 分支清单与覆盖映射（覆盖准则：分支覆盖 100%）

| # | 分支/路径 | 触发条件 | 覆盖用例 |
|---|----------|---------|---------|
| B1 | `not agent_id or not agent_id.strip()` → True | agent_id 空/纯空白/None | C02（空/空白）、C05（None） |
| B1' | 同上 → False（通过） | agent_id 合法 | C01 |
| B2 | `not agent_name or not agent_name.strip()` → True | agent_name 空/纯空白/None | C03、C05 |
| B2' | → False | 合法 | C01 |
| B3 | `not when_to_use or not when_to_use.strip()` → True | when_to_use 空/纯空白/None | C04、C05 |
| B3' | → False | 合法 | C01 |
| B4 | `for perm in sensitive_permissions` 循环 + `isinstance(perm, SensitivePermission)` → False | 元素非枚举（str/错误枚举） | C09 |
| B4' | 循环完整迭代无异常元素 | 全枚举元素 | C01 |
| B5 | `isinstance(trust_level, TrustLevel)` → False | 裸 int/str/None/float | C10 |
| B5' | → True | 枚举实例 | C01 |
| B6 | `len(stripped_when) > 200` → True | strip 后 >200 字符 | C25 |
| B6' | → False | ≤200 字符 | C24 |
| B7 | `isinstance(sensitive_permissions, (frozenset,set,list))` → False | None/str/tuple/dict/int 容器 | C07 |
| B7' | → True（进入 frozenset 转换） | frozenset/set/list | C01 |
| B8 | `frozenset()` 抛 `TypeError` → except 分支 | 含不可哈希元素（dict/list 嵌套） | C08 |
| B8' | 转换成功 | 全可哈希元素 | C01 |
| B9 | `__setattr__` raise 分支 | 任何运行时赋值 | C12、C14 |
| B10 | `__delattr__` raise 分支 | 任何运行时删除 | C13、C14 |
| B11 | `_safe_agent_id()` AttributeError 分支 | `_agent_id` 未初始化 | C14 |
| B11' | 正常读取 | 已构造 | C12（日志文案断言依赖） |
| B12 | `has_permission` 返回 True / False 两分支 | 持有/未持有 | C16 |
| B13 | `__eq__` 非 Identity → NotImplemented | 与任意非 Identity 比较 | C22 |
| B14 | `__eq__` 全等 / 任一字段不等 | 五字段全等 / 部分不等 | C20 / C21 |
| B15 | `__hash__` 执行路径 | 任意 | C20、C23 |

**MC-DC 不适用**：B1/B2/B3 的复合条件 `not x or not x.strip()` 是短路 or，两个子条件已由独立样例（空串触发 `not x` 分支、纯空白触发 `not x.strip()` 分支、None 触发 `not x` 分支）分别覆盖，无需 MC-DC。
**路径覆盖不适用**：分支间无耦合状态，组合爆炸无收益；分支覆盖 100% 即为充分。

---

## 2. 不变式清单（inv-1..inv-9）

| 编号 | 不变式 | 来源 |
|------|--------|------|
| inv-1 | **HC1**：Identity 创建后完全不可变——任意字段赋值/删除/新增 → `PermissionError`，字段值保持原样 | 模块 docstring + 任务① |
| inv-2 | **HC2**：`sensitive_permissions` 恒为 `frozenset` 且元素恒为 `SensitivePermission` 枚举成员，结构深度不可变 | 任务② |
| inv-3 | **E4**：agent_id/agent_name/when_to_use 为 None/空串/纯空白 → `ValueError` | 任务④ |
| inv-4 | **S4**：`trust_level` 恒为 `TrustLevel` 实例；裸 int/str/None/float 一律拒绝 | 任务⑥ |
| inv-5 | **S2**：权限只接受 `SensitivePermission` 枚举；`has_permission` 对非枚举输入 fail-closed 返回 `False`（不隐式匹配、不抛 TypeError） | 任务⑤ + review R1 |
| inv-6 | **规范化**：三字符串字段存储前 `strip()`；sensitive_permissions 接受 frozenset/set/list 且统一存为 frozenset | 任务⑦ |
| inv-7 | **eq/hash**：相等 ⟺ 五字段全等；`hash` 与 `eq` 一致且确定；与非 Identity 比较返回 NotImplemented | 任务⑩ |
| inv-8 | **权限判定**：`has_permission(perm) ⟺ perm ∈ sensitive_permissions`（确定性，同输入同输出） | 任务⑨ |
| inv-9 | **长度警告**：when_to_use strip 后 >200 字符 → `logger.warning` 且**不阻断创建**；≤200 不警告 | 任务⑧ |

**S3（权限不继承）维度说明**：该维度不适用单独用例——`Identity` 无父级权限概念、无「继承自」字段，`sensitive_permissions` 完全由构造参数决定；「每个 Identity 独立声明」由构造契约天然保证，由 C01（不同实例各自声明）与 C16（空集合 fail-safe）间接覆盖。若未来引入继承机制再补测。

---

## 3. 风险清单（按优先级排序，S×L 定级）

| 编号 | 风险 | S×L | 优先级 | 状态 | 关联用例 |
|------|------|-----|--------|------|---------|
| Risk-1 | 🔴 **R1**：`has_permission` 传入字符串（str 枚举特性隐式匹配 `"data_write" in frozenset → True`）绕过 S2 封闭性；传入不可哈希对象（如 dict）抛裸 `TypeError`，破坏「统一 ValueError/fail-closed」契约 | 高×中 | **P0** | **已修复**（入口加 isinstance 校验，fail-closed False + warning 留痕） | C17、C18 |
| Risk-2 | 🟡 **R2**：sensitive_permissions 含不可哈希元素（list 内嵌 dict）→ 必须 `ValueError`（当前 try/except TypeError 已实现，验证不回归） | 中×低 | **P1** | 已实现，验证 | C08 |
| Risk-3 | 🟡 **R3**：构造异常路径下 `__setattr__`/`__delattr__`/`_safe_agent_id` 误触发崩溃（`_agent_id` 未初始化） | 中×低 | **P1** | 已实现，验证 | C14 |
| Risk-4 | **R4**：非 str 非空字符串字段值（如 `agent_id=123`）→ `agent_id.strip()` 抛 `AttributeError`，违反「类型错误统一 ValueError 语义」 | 中×中 | **P1** | **已修复**（_validate_fields 第 0 步类型检查，非 str 统一 ValueError） | C06 |
| Risk-5 | **E4 空值**：必填字段空/纯空白被放行 → 生成无锚点（无 trace 键）的 Identity | 高×中 | **P0** | 已实现 | C02/C03/C04/C05 |
| Risk-6 | **S4 伪造**：IntEnum 使 `TrustLevel.EXTERNAL == 1` 成立，若校验用 `==` 或值比较会被裸 int 绕过 | 高×中 | **P0** | 已实现（isinstance 校验正确） | C10 |
| Risk-7 | **HC2 破防**：外部拿到 `sensitive_permissions` 引用后原地修改（需证明返回的是原生不可变 frozenset） | 中×中 | **P1** | 已实现 | C15 |
| Risk-8 | **S2 容器绕过**：sensitive_permissions 传 None/字符串/tuple/dict/int 容器类型绕过封闭性 | 高×中 | **P0** | 已实现 | C07 |
| Risk-9 | **eq/hash 不一致**：`__eq__` 与 `__hash__` 语义漂移 → dict/set 失效或去重错乱（SubAgentRegistry 场景） | 中×中 | **P1** | 已实现 | C20/C21/C22/C23 |
| Risk-10 | **长度警告缺失**：when_to_use 超长未警告 → orchestrator 路由 prompt 超出 context 窗口 | 中×中 | **P2** | 已实现 | C25 |
| Risk-11 | **规范化失效**：strip 未生效 → 空白差异导致「看似不同」的 identity 判等失真 | 中×中 | **P2** | 已实现 | C01 |
| Risk-12 | **PERMISSION_ALL 漂移**：枚举成员增删/枚举值改名后 PERMISSION_ALL 或枚举契约不同步 | 中×低 | **P2** | 已实现 | C19 |

---

## 4. Oracle 策略

| 场景 | Oracle 类型 | 依据 |
|------|------------|------|
| 错误类型 + 文案 | **golden value**（`pytest.raises(..., match=稳定子串)`） | 错误消息为源码字面量，可独立推导；断言稳定子串而非全文，避免 change-detector |
| strip 规范化结果 | **golden value**（手算：`"  alice  " → "alice"`） | 可独立推导 |
| 枚举值 / PERMISSION_ALL 内容 | **golden value**（源码字面量 `"data_write"` 等） | 规格白纸黑字 |
| has_permission 布尔结果 | **golden value** | 确定性布尔 |
| eq/hash 一致性 | **蜕变关系**：`a == b ⟹ hash(a) == hash(b)`；`hash(x)` 多次调用一致 | 输出不可预知（hash 值），断言关系而非绝对值 |
| 不可变性副作用 | **golden value**：篡改后字段值仍为原值 | 可独立推导 |

**无自指 oracle**：所有期望值均来自源码字面量/手算/规格，不依赖「运行被测实现抄输出」。C01 的 frozenset 内容、C19 的枚举值、C25 的边界长度均可独立验证。

---

## 5. Mock/Fake 决策与层级判定

### 5.1 依赖决策（零 mock）

| 依赖 | 决策 | 理由 |
|------|------|------|
| `logging` | 真实现 + `caplog` fixture 捕获 | 进程内日志，非外部 I/O；`caplog` 是 pytest 内置捕获，非 mock 外部依赖 |
| `SensitivePermission`/`TrustLevel`/`PERMISSION_ALL` | 真实现 | 被测对象自身的组成部分，mock 即自指 |
| 外部服务（DB/网络/MQ/磁盘） | 无此依赖 | 纯数据模型 |

### 5.2 层级判定：全部 unit

无任何协作对象或外部 I/O 边界——构造、校验、判等全部在进程内完成；`logging` 属进程内标准库。component(fake)/integration/e2e 均不适用。

### 5.3 故障注入豁免声明（显式）

**该类不适用**：本模块无外部 I/O（网络/DB/MQ/磁盘），无注入点。模块内「故障路径」已作为独立用例覆盖：

| 内部故障 | 注入方式 | 期望行为 | 用例 |
|---------|---------|---------|------|
| 不可哈希元素 → `frozenset()` 抛 TypeError | 传 `[{"a":1}]` | 转 `ValueError`（`raise ... from exc` 链完整） | C08 |
| 未初始化 `_agent_id` → AttributeError | `Identity.__new__` 后触发 `__setattr__` | `_safe_agent_id()` 返回 `"<uninitialized>"`，仍抛 PermissionError 而非崩溃 | C14 |

### 5.4 副作用验证点（必须有）

- 校验失败路径：`logger.error` 留痕（C02 断言）
- 篡改拦截路径：`logger.warning` 留痕（C12 断言）
- 超长警告：`logger.warning`（C25 断言）
- 对象状态：篡改后字段值不变（C12）、frozenset 深度不可变（C15）

---

## 6. 等价类划分总表

### 6.1 字符串字段（agent_id / agent_name / when_to_use 共用维度）

| 等价类 | 代表值 | 期望 | 用例 |
|--------|--------|------|------|
| 合法（strip 后非空） | `"alice"`、`"  alice  "` | 创建成功，存 strip 后值 | C01 |
| 空串 | `""` | ValueError | C02/C03/C04 |
| 纯空白 | `"   "` | ValueError | C02/C03/C04 |
| None | `None` | ValueError | C05 |
| 非 str 非空（int/dict/list） | `123` | **期望 ValueError**（当前 AttributeError，known-gap） | C06 |
| 边界长度（when_to_use 专用） | strip 后 200 / 201 字符 | 200 不警告 / 201 警告且创建成功 | C24/C25 |

### 6.2 sensitive_permissions

| 等价类 | 代表值 | 期望 | 用例 |
|--------|--------|------|------|
| frozenset（合法） | `frozenset({DATA_WRITE})` | 原样存储 | C01 |
| set（合法） | `{DATA_WRITE, CODE_EXEC}` | 转 frozenset | C01 |
| list（合法） | `[DATA_WRITE]` | 转 frozenset | C01 |
| 空集合 | `frozenset()` | 创建成功（fail-safe 默认） | C16 |
| PERMISSION_ALL | `PERMISSION_ALL` | 全 6 权限 | C19 |
| None | `None` | ValueError（容器类型） | C07 |
| 字符串 | `"data_write"` | ValueError（容器类型，禁止字符拆分） | C07 |
| tuple | `(DATA_WRITE,)` | ValueError（容器类型） | C07 |
| dict | `{"a":1}` | ValueError（容器类型） | C07 |
| int | `7` | ValueError（容器类型） | C07 |
| list 含 dict（不可哈希） | `[{"a":1}]`、`[[1]]` | ValueError（R2） | C08 |
| list 含 str（可哈希但非枚举） | `["data_write"]` | ValueError（S2 封闭） | C09 |
| list 含错误枚举 | `[TrustLevel.SUB_AGENT]` | ValueError（S2 封闭） | C09 |

### 6.3 trust_level

| 等价类 | 代表值 | 期望 | 用例 |
|--------|--------|------|------|
| 合法枚举 | `TrustLevel.EXTERNAL/SUB_AGENT/ORCHESTRATOR` | 创建成功，存枚举实例 | C01 |
| 裸 int | `1`、`2` | ValueError（IntEnum == 成立但必须拒绝） | C10 |
| 字符串 | `"ORCHESTRATOR"`、`"1"` | ValueError | C10 |
| None | `None` | ValueError | C10 |
| float | `1.0` | ValueError | C10 |

### 6.4 has_permission 输入

| 等价类 | 代表值 | 期望 | 用例 |
|--------|--------|------|------|
| 持有的枚举 | `SensitivePermission.DATA_WRITE` | True | C16 |
| 未持有的枚举 | `SensitivePermission.CODE_EXEC` | False | C16 |
| 空权限集合 + 任意枚举 | 任一成员 | False（fail-safe） | C16 |
| 字符串值（str 枚举隐式匹配面） | `"data_write"` | **期望 False**（当前 True，known-gap R1） | C17 |
| 不可哈希对象 | `{"a":1}` | **期望 False**（当前 TypeError，known-gap R1） | C17 |
| 其他非枚举（None/int） | `None`、`1` | False（现状即符合，锁定回归） | C18 |

---

## 7. 用例 × 风险/不变式覆盖矩阵

| 用例 | inv-1 HC1 | inv-2 HC2 | inv-3 E4 | inv-4 S4 | inv-5 S2 | inv-6 规范化 | inv-7 eq/hash | inv-8 权限 | inv-9 警告 | R1 | R2 | R3 | R4 | 优先级 |
|------|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| C01 happy path + 规范化 | ✅ | ✅ | ✅ | ✅ | | ✅ | | ✅ | | | | | | P0 |
| C02 agent_id 空/空白 | | | ✅ | | | | | | | | | | | P0 |
| C03 agent_name 空/空白 | | | ✅ | | | | | | | | | | | P0 |
| C04 when_to_use 空/空白 | | | ✅ | | | | | | | | | | | P0 |
| C05 三字段 None | | | ✅ | | | | | | | | | | | P0 |
| C06 agent_id=123 | | | ✅ | | | | | | | | | | ✅ | P1 |
| C07 容器类型错误 | | | ✅ | | ✅ | | | | | | | | | P0 |
| C08 不可哈希元素 | | | ✅ | | | | | | | | ✅ | | | P1 |
| C09 元素非枚举 | | | ✅ | | ✅ | | | | | | | | | P0 |
| C10 trust_level 伪造 | | | ✅ | ✅ | | | | | | | | | | P0 |
| C11 _validate_fields 直测 | | | ✅ | | | | | | | | | | | P2 |
| C12 赋值拦截 | ✅ | | | | | | | | | | | | | P0 |
| C13 删除拦截 | ✅ | | | | | | | | | | | | | P0 |
| C14 未初始化安全 | ✅ | | | | | | | | | | | ✅ | | P1 |
| C15 深度不可变 | ✅ | ✅ | | | | | | | | | | | | P1 |
| C16 权限判定 | | | | | | | | ✅ | | | | | | P1 |
| C17 字符串/不可哈希 fail-closed | | | | | ✅ | | | ✅ | | ✅ | | | | P0 |
| C18 None/int fail-closed | | | | | ✅ | | | ✅ | | ✅ | | | | P1 |
| C19 PERMISSION_ALL | | | | | ✅ | | | ✅ | | | | | | P1 |
| C20 eq/hash 一致 + dict/set | | | | | | | ✅ | | | | | | | P1 |
| C21 单字段不同 | | | | | | | ✅ | | | | | | | P1 |
| C22 非 Identity 比较 | | | | | | | ✅ | | | | | | | P1 |
| C23 hash 确定性 | | | | | | | ✅ | | | | | | | P1 |
| C24 200 字符无警告 | | | | | | | | | ✅ | | | | | P2 |
| C25 201 字符警告不阻断 | | | | | | | | | ✅ | | | | | P2 |

---

## 8. 用例详情

> **公共构造器**（以下所有用例复用；除覆盖项外均为合法基准值）：
> ```python
> def make_identity(**overrides):
>     base = {
>         "agent_id": "alice",
>         "agent_name": "Alice",
>         "when_to_use": "handle user requests",
>         "sensitive_permissions": frozenset({SensitivePermission.DATA_WRITE}),
>         "trust_level": TrustLevel.SUB_AGENT,
>     }
>     base.update(overrides)
>     return Identity(**base)
> ```
> **导入**：`from pandaren.identity.models import Identity, TrustLevel, SensitivePermission, PERMISSION_ALL, _validate_fields`
> （pytest 收集时仓库根自动入 sys.path；若需直接 `python` 运行，按仓库惯例先 `sys.path.insert`，见 §10 推断 2）

---

### Group A — 构造与校验（E4 / S4 / S2 / 规范化）

#### 用例 C01：全字段合法创建成功，规范化生效（strip + frozenset + 枚举存储）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 [P0] + inv-4 [P0] + inv-6 [P1] + inv-2 [P1] + inv-8 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 全部校验分支 False 侧（B1'/B2'/B3'/B4'/B5'/B7'/B8'）+ 分支 B1' 等 |
| Oracle | golden value（strip 结果与枚举值均可手算） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：字符串字段「合法（strip 后非空）」→ 代表值 = `"  alice  "` / `"  Alice  "` / `"  do things  "`；sensitive_permissions 容器「frozenset/set/list」→ 三值各一次；trust_level「合法枚举三值」→ parametrize。

**Given**（前置条件）：
- 无前置

**When**（操作/动作）：
- `Identity(agent_id="  alice  ", agent_name="  Alice  ", when_to_use="  do things  ", sensitive_permissions=sp, trust_level=tl)`，其中 `sp ∈ {frozenset({DATA_WRITE}), {DATA_WRITE, CODE_EXEC}, [DATA_WRITE]}`、`tl ∈ {TrustLevel.EXTERNAL, TrustLevel.SUB_AGENT, TrustLevel.ORCHESTRATOR}`（parametrize 9 组合）

**Then**（预期结果）：
- 创建成功，不抛异常
- `identity.agent_id == "alice"`、`identity.agent_name == "Alice"`、`identity.when_to_use == "do things"`（inv-6 strip 生效）
- `type(identity.sensitive_permissions) is frozenset` 且内容 == 传入集合（inv-6 统一 frozenset，无论容器类型）
- `identity.sensitive_permissions` 中每个元素 `isinstance(perm, SensitivePermission)`（inv-2）
- `identity.trust_level is tl`（inv-4 存储枚举实例本身）
- 副作用：无状态变更（构造期仅 `logger.info` 一条，不断言）

---

#### 用例 C02：agent_id 空串 / 纯空白 → ValueError + logger.error 留痕

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 [P0] + Risk-5 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch B1 → True（`not agent_id` 与 `not agent_id.strip()` 两子条件各由一值触发） |
| Oracle | golden value（错误文案为源码字面量） |
| Mock | 否 |

**等价类划分**：agent_id「空串 / 纯空白」→ 代表值 = `""`、`"   "`（parametrize）

**Given**（前置条件）：
- `caplog.set_level(logging.ERROR, logger="pandaren.identity.models")`

**When**（操作/动作）：
- `make_identity(agent_id="")` 与 `make_identity(agent_id="   ")`

**Then**（预期结果）：
- 均抛 `ValueError`，`pytest.raises(ValueError, match="agent_id 不能为空")`
- 副作用：caplog 含 1 条 level=ERROR 记录，message 含 `"agent_id 为空"`

---

#### 用例 C03：agent_name 空串 / 纯空白 → ValueError

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 [P0] + Risk-5 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch B2 → True |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：agent_name「空串 / 纯空白」→ 代表值 = `""`、`"   "`（parametrize）

**When**：`make_identity(agent_name="")`、`make_identity(agent_name="   ")`

**Then**：均抛 `ValueError`，`match="agent_name 不能为空"`；副作用：caplog level=ERROR 含 `"agent_name 为空"`

---

#### 用例 C04：when_to_use 空串 / 纯空白 → ValueError

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 [P0] + Risk-5 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch B3 → True |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：when_to_use「空串 / 纯空白」→ 代表值 = `""`、`"   "`（parametrize）

**When**：`make_identity(when_to_use="")`、`make_identity(when_to_use="   ")`

**Then**：均抛 `ValueError`，`match="when_to_use 不能为空"`；副作用：caplog level=ERROR 含 `"when_to_use 为空"`

---

#### 用例 C05：三字符串字段传 None → ValueError

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 [P0]（"缺失"按设计意图解释为 None/空值；keyword-only 参数缺失由 Python 抛 TypeError，非模块可控，见 §9 解释说明） |
| 测试层级 | unit |
| 覆盖准则 | branch B1/B2/B3 的 `not x` 子条件（None 触发） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：字符串字段「None」→ 代表值 = `None`（parametrize 三字段）

**When**：
- `make_identity(agent_id=None)`
- `make_identity(agent_name=None)`
- `make_identity(when_to_use=None)`

**Then**：均抛 `ValueError`，match 分别 = `"agent_id 不能为空"` / `"agent_name 不能为空"` / `"when_to_use 不能为空"`

---

#### 用例 C06：agent_id 传非 str 非空值（int）→ 期望 ValueError（known-gap：当前 AttributeError）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-4 [P1]（known-gap）+ inv-3 类型语义 |
| 测试层级 | unit |
| 覆盖准则 | 期望新分支（修复后类型检查）；当前路径为 `agent_id.strip()` 抛 AttributeError |
| Oracle | golden value（期望错误类型与文案） |
| Mock | 否 |

**等价类划分**：字符串字段「非 str 非空」→ 代表值 = `123`（int；dict/list 同属此类，1 个代表值即可）

**Given**（前置条件）：
- 已确认当前实现：`Identity(agent_id=123, ...)` → `AttributeError: 'int' object has no attribute 'strip'`（本机实测）

**When**（操作/动作）：
- `make_identity(agent_id=123)`

**Then**（预期结果，按期望行为写，当前未满足）：
- 抛 `ValueError`，`match="agent_id 不能为空"`（或修复后约定的类型错误文案）
- 副作用：caplog level=ERROR 留痕

**落码标注**：`[known-gap]` → `pytest.mark.xfail(reason="R4: 非 str 字段值泄漏 AttributeError，期望统一 ValueError", strict=True)`

---

#### 用例 C07：sensitive_permissions 容器类型错误（None/str/tuple/dict/int）→ ValueError

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 [P0] + Risk-8 [P0] + inv-3 类型语义 |
| 测试层级 | unit |
| 覆盖准则 | branch B7 → False |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：容器「非 frozenset/set/list」→ 代表值 = `None`、`"data_write"`、`("data_write",)`、`{"a": 1}`、`7`（parametrize 5 值）

**When**：`make_identity(sensitive_permissions=bad)`，bad 遍历上述 5 值

**Then**：均抛 `ValueError`，`match="sensitive_permissions 类型错误"`；且**不抛 TypeError**（统一 ValueError 语义）

---

#### 用例 C08：sensitive_permissions 含不可哈希元素 → ValueError（R2 回归验证）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-2 [P1]（R2）+ inv-3 类型语义 |
| 测试层级 | unit |
| 覆盖准则 | branch B8 → except 分支（`frozenset()` TypeError → ValueError） |
| Oracle | golden value + 异常链 |
| Mock | 否 |

**等价类划分**：元素「不可哈希」→ 代表值 = `[{"a": 1}]`、`[[1]]`（parametrize）

**When**：`make_identity(sensitive_permissions=[{"a": 1}])`、`make_identity(sensitive_permissions=[[1]])`

**Then**（预期结果）：
- 均抛 `ValueError`，`match="不可哈希"`
- 异常链完整：`exc.__cause__` 是 `TypeError`（`raise ... from exc` 未被吞掉，供调试）

---

#### 用例 C09：sensitive_permissions 元素非枚举（可哈希但越界）→ ValueError（S2 封闭）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 [P0] + Risk-8 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch B4 → `isinstance(perm, SensitivePermission)` False |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：元素「非 SensitivePermission」→ 代表值 = `["data_write"]`（自由字符串）、`[TrustLevel.SUB_AGENT]`（错误枚举类型）（parametrize）

**When**：`make_identity(sensitive_permissions=["data_write"])`、`make_identity(sensitive_permissions=[TrustLevel.SUB_AGENT])`

**Then**：均抛 `ValueError`，`match="元素必须是 SensitivePermission"`；错误文案含有效值列表 `"data_write"` 等（golden）

---

#### 用例 C10：trust_level 伪造（裸 int / 字符串 / None / float）→ ValueError（S4）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 [P0] + Risk-6 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch B5 → False |
| Oracle | golden value + 反证（`TrustLevel.EXTERNAL == 1` 为 True，仍必须拒绝裸 int） |
| Mock | 否 |

**等价类划分**：trust_level「非枚举」→ 代表值 = `1`、`2`、`"ORCHESTRATOR"`、`None`、`1.0`（parametrize 5 值）

**When**：
- `make_identity(trust_level=bad)`，bad 遍历上述 5 值
- 反证：`TrustLevel.EXTERNAL == 1` 断言为 True（IntEnum 值语义），但构造必须拒绝 `1`

**Then**：均抛 `ValueError`，`match="trust_level 类型错误"`；错误文案含有效枚举名 `["EXTERNAL", "SUB_AGENT", "ORCHESTRATOR"]`（golden）

---

#### 用例 C11：`_validate_fields` 直接调用契约（模块内部函数直测）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 [P0]（函数级契约）+ 目标点名 `_validate_fields` |
| 测试层级 | unit |
| 覆盖准则 | 空值分支直测；list 传入可迭代校验（不依赖 __init__ 规范化） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：函数输入「非法空值 / 合法（未规范化容器）」→ 代表值 = `agent_id=""` / `sensitive_permissions=[SensitivePermission.DATA_WRITE]`（list，绕过 __init__ 规范化直接可迭代）

**When**：
- `_validate_fields(agent_id="", agent_name="b", when_to_use="c", sensitive_permissions=frozenset(), trust_level=TrustLevel.EXTERNAL)`
- `_validate_fields(agent_id="a", agent_name="b", when_to_use="c", sensitive_permissions=[SensitivePermission.DATA_WRITE], trust_level=TrustLevel.EXTERNAL)`

**Then**：
- 前者抛 `ValueError`，`match="agent_id 不能为空"`
- 后者不抛异常（函数按迭代校验，不要求传入已规范化 frozenset——与 docstring「调用方已规范化」的约定兼容，本机实测通过）
- 副作用：前者 caplog level=ERROR 含 `"agent_id 为空"`

---

### Group B — 不可变性（HC1 / HC2）

#### 用例 C12：已构造实例字段赋值 → PermissionError，字段值不变，logger.warning 留痕

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 [P0]（HC1） |
| 测试层级 | unit |
| 覆盖准则 | branch B9（`__setattr__` raise）+ B11'（`_safe_agent_id` 正常路径） |
| Oracle | golden value（异常类型 + 篡改后值仍为原值） |
| Mock | 否 |

**等价类划分**：可写字段「5 个 slot」→ 代表值 = `agent_id` / `agent_name` / `when_to_use` / `sensitive_permissions` / `trust_level`（parametrize）；另加「新增不存在字段」→ `new_field`

**Given**（前置条件）：
- `id_ = make_identity()`；`caplog.set_level(logging.WARNING, logger="pandaren.identity.models")`

**When**（操作/动作）：
- `with pytest.raises(PermissionError): setattr(id_, field, "hacked")`（field 遍历 5 个 slot + `new_field`）

**Then**（预期结果）：
- 均抛 `PermissionError`，`match="immutable"`（文案含 `cannot modify '{field}'`）
- 副作用①：字段值保持原样——`id_.agent_id == "alice"`（inv-1 值不变）
- 副作用②：caplog 含 level=WARNING 记录，message 含 `"运行时篡改尝试"` 与 `"被拦截"`

---

#### 用例 C13：已构造实例字段删除 → PermissionError

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 [P0]（HC1） |
| 测试层级 | unit |
| 覆盖准则 | branch B10（`__delattr__` raise） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：可删字段「5 个 slot」→ 代表值同上（parametrize）

**When**：`with pytest.raises(PermissionError): delattr(id_, f"_{field}")`（field 遍历 5 个）

**Then**：均抛 `PermissionError`，`match="immutable"`；`id_.agent_id` 仍可正常读取（对象未被破坏）

---

#### 用例 C14：未初始化实例的赋值/删除不崩溃，`_safe_agent_id` 返回占位符（R3）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-3 [P1]（R3）+ inv-1 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch B11（`_safe_agent_id` AttributeError 分支）+ B9/B10 在未初始化态 |
| Oracle | golden value（`"<uninitialized>"` 为源码字面量） |
| Mock | 否 |

**Given**（前置条件）：
- `u = Identity.__new__(Identity)`（绕过 `__init__`，`_agent_id` 等 slot 均未赋值，模拟构造异常路径）

**When**（操作/动作）：
- `with pytest.raises(PermissionError): u.agent_id = "x"`
- `with pytest.raises(PermissionError): del u._agent_id`

**Then**（预期结果）：
- 两者均抛 `PermissionError`（**而非 AttributeError 崩溃**——`__setattr__`/`__delattr__` 内部 `_safe_agent_id()` 在 slot 未初始化时安全降级）
- `u._safe_agent_id() == "<uninitialized>"`（B11 分支）

---

#### 用例 C15：sensitive_permissions 深度不可变（原生 frozenset 封闭）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 [P1]（HC2）+ Risk-7 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 无分支（属性读取 + 原生类型能力断言） |
| Oracle | golden value |
| Mock | 否 |

**Given**：`id_ = make_identity()`

**When**：
- `sp = id_.sensitive_permissions`

**Then**（预期结果）：
- `type(sp) is frozenset`（属性返回原生 frozenset，非可变 set 副本）
- `hasattr(sp, "add") is False` 且 `hasattr(sp, "discard") is False`（frozenset 无原地修改接口）
- `sp == frozenset({SensitivePermission.DATA_WRITE})`（内容精确）
- `all(isinstance(p, SensitivePermission) for p in sp)`（元素封闭）
- `with pytest.raises(TypeError): sp.add(SensitivePermission.CODE_EXEC)`（若有 add 则 TypeError；断言原集合未被修改）
- 副作用：`id_.sensitive_permissions` 仍为 `frozenset({DATA_WRITE})`（外部引用修改尝试不污染对象）

---

### Group C — has_permission（S2 / inv-8）

#### 用例 C16：持有 → True；未持有 → False；空集合 fail-safe → False

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 [P1]（确定性） |
| 测试层级 | unit |
| 覆盖准则 | branch B12 的 True/False 两分支 |
| Oracle | golden value（布尔结果可独立推导） |
| Mock | 否 |

**等价类划分**：
- 「持有」→ `{DATA_WRITE}` 查 `DATA_WRITE` → True
- 「未持有」→ `{DATA_WRITE}` 查 `CODE_EXEC` → False
- 「空集合」→ `frozenset()` 查任一成员（如 `DATA_DELETE`）→ False（fail-safe 默认）

**When**（parametrize 3 组）：
- `make_identity(sensitive_permissions={SensitivePermission.DATA_WRITE}).has_permission(SensitivePermission.DATA_WRITE)`
- `make_identity(sensitive_permissions={SensitivePermission.DATA_WRITE}).has_permission(SensitivePermission.CODE_EXEC)`
- `make_identity(sensitive_permissions=frozenset()).has_permission(SensitivePermission.DATA_DELETE)`

**Then**：分别 `is True` / `is False` / `is False`

---

#### 用例 C17：has_permission 非枚举输入（字符串 / 不可哈希）→ 期望 fail-closed False（known-gap R1）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-1 [P0]（R1，🔴）+ inv-5 [P0] + inv-8 |
| 测试层级 | unit |
| 覆盖准则 | 期望新分支（修复后入口 `isinstance(perm, SensitivePermission)` 检查）；当前实现无该分支（直接 `in`） |
| Oracle | golden value（期望布尔 False，可独立推导：非枚举输入不得匹配任何权限） |
| Mock | 否 |

**等价类划分**：非枚举输入「字符串值 / 不可哈希对象」→ 代表值 = `"data_write"`（str 枚举隐式匹配面）、`{"a": 1}`（不可哈希，当前抛 TypeError）（parametrize）

**Given**（前置条件）：
- 已确认当前行为（本机实测）：
  - `has_permission("data_write")` → **True**（str 枚举 hash/eq 与裸字符串一致，S2 被绕过）
  - `has_permission({"a": 1})` → **TypeError: unhashable type**（契约要求 fail-closed，非崩溃）

**When**（操作/动作）：
- `make_identity(sensitive_permissions={SensitivePermission.DATA_WRITE}).has_permission("data_write")`
- `make_identity(sensitive_permissions={SensitivePermission.DATA_WRITE}).has_permission({"a": 1})`

**Then**（预期结果，按期望行为写，当前均未满足）：
- 两者均返回 `False`（fail-closed：非枚举输入不匹配任何权限，不崩溃）
- （review 另建议拒绝时 `logger.warning` 留痕——属修复实现细节，不纳入本用例断言，避免过度限定修复方案）

**落码标注**：`[known-gap]` → `pytest.mark.xfail(reason="R1: has_permission 接受字符串隐式匹配/不可哈希抛 TypeError，期望 fail-closed False", strict=True)`

---

#### 用例 C18：has_permission 非枚举输入（None / int）→ False（现状即符合，锁定回归）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 [P0] + Risk-1 [P0]（None/int 当前已 fail-closed，防止未来修复引入回归） |
| 测试层级 | unit |
| 覆盖准则 | 现状路径（非枚举不匹配 → False） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：非枚举输入「None / 裸 int」→ 代表值 = `None`、`1`（parametrize）

**When**：`make_identity().has_permission(None)`、`make_identity().has_permission(1)`

**Then**：均 `is False`（不抛异常；本机实测当前即如此——注意与 C17 的字符串/dict 输入区分）

---

#### 用例 C19：PERMISSION_ALL 常量契约 + 全权限判定

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-12 [P2] + inv-5 [P0] + inv-8 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 无分支（常量内容 + 枚举契约 golden） |
| Oracle | golden value（枚举值与数量来自源码字面量） |
| Mock | 否 |

**Given**（前置条件）：
- 枚举契约（golden）：`{DATA_WRITE:"data_write", DATA_DELETE:"data_delete", CODE_EXEC:"code_exec", SYSTEM_CMD:"system_cmd", NETWORK_CALL:"network_call", MEMORY_WRITE:"memory_write"}`

**When**（操作/动作）：
- 断言 `SensitivePermission` 恰有 6 个成员且各成员 `.value` 与上表一致
- `id_all = make_identity(sensitive_permissions=PERMISSION_ALL)`

**Then**（预期结果）：
- `PERMISSION_ALL == frozenset(SensitivePermission)` 且 `type(PERMISSION_ALL) is frozenset`
- `len(PERMISSION_ALL) == 6`
- `all(id_all.has_permission(p) for p in SensitivePermission)` → True（全权限授予生效）
- 对比：`make_identity(sensitive_permissions=frozenset()).has_permission(SensitivePermission.DATA_WRITE)` → False（空集合与 ALL 的反差锁定 fail-safe 语义）

---

### Group D — 等值与哈希（inv-7）

#### 用例 C20：全字段相同 → == True 且 hash 一致，可用作 dict key / set 元素

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 [P1] + Risk-9 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch B14 全等 True 分支 + B15（`__hash__`） |
| Oracle | 蜕变关系（断言关系而非 hash 绝对值） |
| Mock | 否 |

**Given**：`a = make_identity()`、`b = make_identity()`（相同全字段，独立构造）

**When**：
- `a == b`
- `hash(a) == hash(b)`
- `{a: "x"}[b]`、`len({a, b})`

**Then**：
- `a == b` → True（inv-7 全字段深比较）
- `hash(a) == hash(b)` → True（eq ⟹ 同 hash，蜕变关系）
- `{a: "x"}[b] == "x"`（dict key 可用）、`len({a, b}) == 1`（set 去重成功——SubAgentRegistry 等去重场景）

---

#### 用例 C21：任一字段不同 → != （5 变体 parametrize）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 [P1] + Risk-9 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch B14 不等 False 分支（逐字段触发） |
| Oracle | golden value（布尔可推导） |
| Mock | 否 |

**等价类划分**：「单字段差异 × 5 字段」→ 代表值 = `agent_id="bob"` / `agent_name="Bob"` / `when_to_use="other"` / `sensitive_permissions={CODE_EXEC}` / `trust_level=TrustLevel.ORCHESTRATOR`（parametrize 5 组）

**When**：`a = make_identity()`；`b = make_identity(**{field: diff_value})`（field 遍历 5 字段，diff_value 对应上表）

**Then**：`a != b` 且 `a == b` → False（任一字段差异即判不等——全字段深比较语义）

---

#### 用例 C22：与非 Identity 比较 → False（NotImplemented 路径）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 [P1]（非 Identity → NotImplemented） |
| 测试层级 | unit |
| 覆盖准则 | branch B13（`__eq__` NotImplemented 分支） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：比较对象「非 Identity」→ 代表值 = `None`、`5`、`"alice"`（parametrize）

**When**：`a = make_identity()`；`a == other`、`a != other`

**Then**：
- `a == other` → False（NotImplemented 回落为不等，不抛异常）
- `a != other` → True（对称语义正确）

---

#### 用例 C23：hash 确定性（多次调用一致）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 [P1]（hash 与 eq 一致且确定——可变字段不能作 key 的根基） |
| 测试层级 | unit |
| 覆盖准则 | branch B15（`__hash__` 执行路径） |
| Oracle | **property 风格**（确定性：`f(x) == f(x)`；hash 值不硬编码） |
| Mock | 否 |

**等价类划分**：同一实例重复调用 / 等值实例跨构造 → 代表值 = `make_identity()` 两次独立构造

**When**：
- `h1 = hash(a); h2 = hash(a); h3 = hash(a)`
- `b = make_identity(); hb = hash(b)`

**Then**：
- `h1 == h2 == h3`（同一实例多次 hash 一致——对象不可变是前提）
- `h1 == hb`（等值实例 hash 一致）
- 附加：构造后篡改被拦截（C12 已证），故 hash 不会因字段变化而失效

> 说明：本用例按确定性 property 设计，但 `hypothesis` 未在 `pyproject.toml` dev 依赖中（推断，见 §10 推断 3）——落码用确定性多样例断言实现，不引入新依赖。

---

### Group E — when_to_use 长度警告（inv-9）

#### 用例 C24：strip 后恰好 200 字符 → 创建成功且无 warning

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-9 [P2]（≤200 不警告） |
| 测试层级 | unit |
| 覆盖准则 | branch B6' → False（`len(stripped_when) > 200` 不成立） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：长度边界「恰好等于上限」→ 代表值 = `"  " + "x"*200 + "  "`（原始 204 字符，strip 后恰 200——同时锁定「长度按 strip 后计算」语义）

**Given**：`caplog.set_level(logging.WARNING, logger="pandaren.identity.models")`

**When**：`id_ = make_identity(when_to_use="  " + "x"*200 + "  ")`

**Then**：
- 创建成功；`len(id_.when_to_use) == 200`
- caplog 中无 level=WARNING 记录（message 不含 `"when_to_use 过长"`）

---

#### 用例 C25：strip 后 201 字符 → logger.warning，且不阻断创建

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-9 [P2] + Risk-10 [P2] |
| 测试层级 | unit |
| 覆盖准则 | branch B6 → True（警告分支） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：长度边界「超过上限 1 字符」→ 代表值 = `"  " + "x"*201 + "  "`（原始 205 字符，strip 后 201——最严格边界：空白包裹不影响警告判定）

**Given**：`caplog.set_level(logging.WARNING, logger="pandaren.identity.models")`

**When**：`id_ = make_identity(when_to_use="  " + "x"*201 + "  ")`

**Then**：
- 创建成功、**不抛异常**（警告不阻断，inv-9 核心）
- `len(id_.when_to_use) == 201`
- 副作用：caplog 含 level=WARNING 记录，message 含 `"when_to_use 过长"` 且含 `agent_id='alice'`（定位字段）

---

## 9. Known-Gap 清单（设计期望 vs 实现现状）

> **全部已关闭**（2025-xx：随源码修复 R1/R4，测试移除 xfail 后 65 passed 全绿）。

| 用例 | 期望行为（设计） | 实际现状 | 差距原因 | 落码方式 | 修复 |
|------|----------------|---------|---------|---------|------|
| C17 | `has_permission("data_write")` → **False**（fail-closed） | **True** | `SensitivePermission(str, Enum)` 的 hash/eq 与裸字符串一致，`"data_write" in frozenset` 隐式匹配（review R1 🔴） | `pytest.mark.xfail(strict=True)` | ✅ 入口 `isinstance` 校验，非枚举 fail-closed False + warning 留痕 |
| C17 | `has_permission({"a": 1})` → **False**（fail-closed） | **TypeError**（unhashable） | `in` 对不可哈希对象抛裸 TypeError，破坏统一 fail-closed 契约（review R1 🔴） | 同上（同一 parametrize 用例） | ✅ 同上 |
| C06 | `Identity(agent_id=123)` → **ValueError** | **AttributeError**（int 无 `.strip`） | `_validate_fields` 未先做 `isinstance(agent_id, str)` 类型检查（R4） | `pytest.mark.xfail(strict=True)` | ✅ `_validate_fields` 第 0 步类型检查，非 str 统一 ValueError |

**修复方向**（供主 Agent 决策，不在本设计落码范围）：
- C17：`has_permission` 入口加 `isinstance(perm, SensitivePermission)` 检查，非枚举 → `logger.warning` 留痕 + 返回 False（review 建议）
- C06：`_validate_fields` 对三字符串字段先做 `isinstance(x, str)` 检查，非 str → ValueError

**解释说明（非 gap）**：任务约束④「必填字段缺失 → ValueError」——构造参数为 keyword-only 且无默认值，**缺参由 Python 原生抛 TypeError**，模块无法也不应改；「缺失」按设计意图解释为 None/空值 → ValueError（C05 已覆盖）。任务约束④括号「不接受 None/字符串/dict/tuple/int」明确针对 sensitive_permissions 容器类型（C07 覆盖）。

---

## 10. 推断与确认清单（给 test-coder / 主 Agent）

| # | 项 | 推断内容 | 状态 |
|---|----|---------|------|
| 1 | 测试文件位置 | `pandaren/identity/tests/test_models.py`（pandaren 下现有 tests 仅 `__init__.py`，无先例——按 pytest testpaths 收集惯例推断） | 推断，可确认 |
| 2 | import 方式 | `from pandaren.identity.models import ...`；pytest 收集时 tests/identity/pandaren 均有 `__init__.py`，prepend 模式会把仓库根插入 sys.path，直接 import 可解析；若需 `python test_models.py` 直接运行，按 pyproject 注释「pandaren 测试靠 sys.path.insert」惯例加 `sys.path.insert`（E402 已豁免） | 推断，可确认 |
| 3 | 属性测试框架 | `hypothesis` 未在 dev 依赖 → C23 等 property 风格用例用确定性多样例实现；若要真随机属性测试需先加依赖，不在本次范围 | 推断，可确认 |
| 4 | xfail 政策 | known-gap 用例（C06/C17）标 `strict=True`：修复后「意外通过」即 xpass 报警，差距不默默消失 | 遵循本设计元方法 |
| 5 | caplog | pytest 内置 fixture，无额外依赖；断言用「message 含稳定子串」而非全文相等，防 change-detector | 直接采用 |
| 6 | review 🟡#2（`__eq__` 全字段 vs docstring「去重」注释矛盾） | 任务约束⑩明确要求全字段深比较 → 测试锁定全字段语义（C20/C21）；注释/docstring 不一致属文档问题，需主 Agent 决策，**不在本测试范围** | 决策挂起 |
| 7 | review 🔵#2（`object.__setattr__` 可绕过不可变性） | Python 固有局限，恶意代码场景超出测试边界；建议 docstring 注明局限，属文档修复，**不设计测试** | 决策挂起 |

---

## 11. 衔接 test-coder 的落码要点

1. **文件**：`pandaren/identity/tests/test_models.py`；用例函数名对应 C01..C25（如 `test_c01_happy_path_normalized`）。
2. **公共构造器** `make_identity(**overrides)`（§8 顶部）复用，避免 5 字段基准值重复。
3. **断言纪律**：错误文案用 `pytest.raises(..., match=稳定子串)`；日志用 caplog + `message` 含子串；集合断言用 `==` 或 `sorted`，不断言迭代顺序。
4. **known-gap**：C06、C17 两条按 §9 表格标 `pytest.mark.xfail(reason=..., strict=True)`，并注释当前现状（实测 True/TypeError/AttributeError），修复后移除 xfail。
5. **不生成**：不引入 hypothesis、不 mock 被测对象、不做 integration/e2e（本模块无外部 I/O）。
6. **运行验证**：`python -m pytest pandaren/identity/tests/test_models.py -q`（rootdir 为仓库根，`testpaths` 已含 pandaren）。
