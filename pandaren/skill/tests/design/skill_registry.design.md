# SkillRegistry 本次改动测试设计

> 被测范围：`pandaren/skill/registry.py`（SkillRegistry）、`pandaren/tool/builtin/skill.py`（SkillToolFactory）、`pandaren/builder.py:1100-1122`（_resolve_skill_registry 装配）
> 改动内容：① 删除死代码 `register_builtin_tools` / `_builtin_tools_registered`；② 修复 description 截断重建丢 script/entry_function；③ 修复同名覆盖残留旧 Action Tool，新增 `_cleanup_action_tool`。
> 后续由 test-coder 转 pytest，测试落位 `pandaren/skill/tests/`。

---

## 0. 关键构造签名与依赖事实（test-coder 前置，白盒确认）

| 项 | 事实 |
|---|---|
| SkillRegistry 构造 | `SkillRegistry(*, tool_registry=None, audit_log=None, max_description_chars=250)` |
| Skill 判定 | `is_action == (script is not None)`（models.py:74-77） |
| Tool.full_name | `namespace="skill"` → `"skill_<name>"`（tool.py:110-115） |
| SkillSource 优先级 | BUILTIN=1 < PROJECT=2 < USER=3 < PROGRAMMATIC=4 |
| version 语义 | register/unregister **成功**时 +1；低优先级覆盖被跳过（`return`）不变；校验失败抛异常不变 |
| ToolRegistry | 纯内存实现（facade.py），提供 `get_tool / register_tool / unregister_tool / promote_to_discovered / register_builtin_factories / list_tool_names`；`unregister_tool` 内部同步清理 store + `_enabled_cache` + `discovery`（facade.py:130-155） |
| 脚本加载 | `load_skill_script(base_path, script)` 走**真实文件系统**，全局模块缓存 `_loaded_modules`，测试 teardown 必须 `script_loader.clear_cache()` |
| ToolContext | `ToolContext(run_id, step_n, agent_id, session_id, metadata=MappingProxyType({...}))`，metadata 只读 |

---

## 1. 不变式清单

| 编号 | 不变式 | 来源分支 |
|---|---|---|
| inv-1 | description 超长截断重建后，Skill 身份字段（name/script/entry_function）与原始一致，`is_action` 保持 True | registry.py:154-176（截断重建分支） |
| inv-2 | 同名 Action Skill 被更高优先级覆盖后，ToolRegistry 中旧 Tool 必须已注销；新 Tool 可通过 `_lazy_register_action_tool` 重新注册 | registry.py:191-192 + 548-565 |
| inv-3 | unregister_skill 成功后，`_skills` / `_action_tools_cache` / `_action_skill_tools` / ToolRegistry 均无该 Skill 痕迹 | registry.py:218-255 |
| inv-4 | 新 Skill.source 优先级 < 已有时，覆盖被跳过且**零副作用**：旧 Tool 不动、缓存不动、version 不变 | registry.py:181-187（跳过分支） |
| inv-5 | search_skills 工具恒由 SkillToolFactory 生成：name=search_skills、tier=ALWAYS、executor 通过 `ctx.metadata["skill_registry"]` 取 registry | skill.py:22-56 + builder.py:1118-1120 |
| inv-6 | SkillRegistry 不存在 `register_builtin_tools` / `_builtin_tools_registered`（死代码防复活） | 改动① |
| inv-7 | `_cleanup_action_tool` 在 `tool_registry is None` 时不抛异常，且仍清理缓存 | registry.py:567-588 |
| inv-8 | 覆盖 existing 为 Knowledge（非 Action）时不调用 cleanup，无 Tool 相关副作用 | registry.py:191（`existing.is_action` 条件） |
| inv-9 | 每次 register/unregister **成功** version 严格 +1；被跳过的覆盖 version 不变 | registry.py:211/254/187 |
| inv-10 | `_cleanup_action_tool` 内 `unregister_tool` 抛异常时（E4 Fail-Safe），异常被吞、缓存仍清理、覆盖/注销主流程不阻塞、留 debug 日志 | registry.py:578-584 |

---

## 2. 风险清单（S 严重度 × L 可能性 → 优先级）

| 编号 | 风险 | 触发条件 | S×L | 优先级 |
|---|---|---|---|---|
| R1 | 死代码复活：`register_builtin_tools` 被重新引入，与 SkillToolFactory 形成重复实现，search_skills 双注册/注册错源 | 未来重构误回滚 | 中×低 | P0（防回归锚点） |
| R2 | search_skills 断链：Tool 未被生成/注册，或 executor 的 `ctx.metadata["skill_registry"]` key 改名 → KeyError | 工厂或装配改动 | 高×中 | P0 |
| R3 | description 截断重建丢 script/entry_function → Action Skill 静默退化 Knowledge（is_action=False），LLM 无法直调工具 | description >250 的 Action Skill 注册 | 高×中 | P0 |
| R4 | 同名覆盖残留旧 Action Tool：ToolRegistry 旧定义不清理，`_lazy_register_action_tool` 幂等检查永远跳过 → Skill 内容更新不生效 | 高优先级同名 Action Skill 覆盖 | 高×中 | P1 |
| R5 | unregister_skill 残留：缓存/映射/ToolRegistry 未清，幽灵 Tool 可被继续发现/执行 | 注销 Action Skill | 高×中 | P1 |
| R6 | 低优先级覆盖跳过时误触发 cleanup，把正在生效的旧 Action Tool 注销 | 低优先级同名注册 | 中×中 | P1 |
| R7 | `_cleanup_action_tool` 在 tool_registry=None（无 Tool 装配的纯 Knowledge 部署）时抛异常，阻断覆盖/注销主流程 | tool_registry=None 场景 | 中×低 | P2 |
| R8 | 非 Action Skill 覆盖误触发 cleanup 或其他 Tool 副作用，污染 Knowledge skill | Knowledge→Knowledge 覆盖 | 低×中 | P2 |
| R9 | 覆盖动作丢失 SKILL_OVERRIDDEN 审计事件（观测/追责缺口） | 覆盖成功 | 低×高 | P3 |
| R10 | Action Skill 脚本加载失败时留下半残缓存（cache 有 key 但 Tool 无效） | 脚本文件缺失/损坏 | 中×低 | P3 |
| R11 | `_cleanup_action_tool` 注销 ToolRegistry 失败时异常上抛，阻断覆盖/注销主流程（E4 失效） | ToolRegistry.unregister_tool 抛异常（极端故障） | 中×低 | P2 |

---

## 3. Mock / Fake 策略

| 依赖 | 决策 | 理由 |
|---|---|---|
| ToolRegistry | **真实现**（内存，无外部 I/O） | 有完整查询/注销 API，可直接断言副作用；纯内存不算外部边界 |
| SkillToolBridge + script_loader | **真实现** | 修复②③ 的证明必须走真实脚本加载与真实 Tool 构建，否则无法证明"script/entry_function 保留 → tool 正常生成" |
| AuditLog | **Fake**（记录 `write_sync` 调用列表） | 断言审计事件内容；真实现写磁盘是噪音 |
| 文件系统（脚本 .py） | **真实 tmp_path 临时目录** | script_loader 只支持真实文件；每次测试唯一 tmp_path 天然隔离模块缓存 |
| AgentBuilder | 直接调用 `_resolve_skill_registry`（构造 `AgentBuilder()` + 设置 `_skill_list`） | 无需 mock 整个 build 流程 |

---

## 4. 测试层级与确定性控制

**层级判定**：凡 Action Skill 注册（必经真实文件系统脚本加载）→ **integration**；纯内存协作（Knowledge、工厂、路由、builder 装配）→ **component(fake)**；私有方法/属性存在性 → **unit**。

**确定性控制**：
- script_loader 全局缓存 → 每个测试使用独立 tmp_path；autouse fixture 在 teardown 调 `script_loader.clear_cache()`
- 无时间/随机/时区依赖；dict 断言一律用集合相等或精确 key
- ToolContext 每用例显式构造（无隐式全局）

**覆盖准则**：分支覆盖（Branch）为默认目标；U3/U5 的复合流程标注所服务的具体分支。

---

## 5. 汇总：用例 × 风险覆盖矩阵

| 用例 | R1 | R2 | R3 | R4 | R5 | R6 | R7 | R8 | R9 | R10 | R11 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| U1 死代码防复活 | ✅ | | | | | | | | | | |
| U2 工厂生成 search_skills | | ✅ | | | | | | | | | |
| U3 executor 经 metadata 路由 | | ✅ | | | | | | | | | |
| U4 截断 Action Skill 保字段 | | | ✅ | | | | | | | | |
| U5 同名覆盖 → 旧Tool清理+新Tool重注册 | | | | ✅ | | | | | | | |
| U6 unregister 全面清理 | | | | | ✅ | | | | | | |
| U7 低优先级覆盖跳过且零副作用 | | | | | | ✅ | | | | | |
| U8 cleanup 在 tool_registry=None 安全 | | | | | | | ✅ | | | | |
| U9 Knowledge 覆盖不触发 cleanup | | | | | | | | ✅ | | | |
| U10 覆盖审计事件 SKILL_OVERRIDDEN | | | | | | | | | ✅ | | |
| U11 builder 装配：SkillToolFactory 注册 search_skills | | ✅ | | | | | | | | | |
| U12 脚本缺失 → 无半残缓存（SK7） | | | | | | | | | | ✅ | |
| U13 cleanup 注销失败吞异常（E4） | | | | | | | | | | | ✅ |

用例数：13（P0×5 / P1×3 / P2×3 / P3×2）。

---

## 6. 全局 fixture 约定（test-coder 落 test 前先建）

```python
# pandaren/skill/tests/conftest.py（建议）
# 1) autouse fixture：每个测试后 script_loader.clear_cache()
# 2) helper：写脚本文件 + 构造 Action Skill
#    script 内容模板（entry_function="run" 显式指定）：
#      def run(query: str) -> str:
#          """Args:
#          query: 查询词
#          """
#          return f"processed:{query}"
# 3) helper：构造 SkillRegistry(tool_registry=ToolRegistry(), audit_log=FakeAuditLog())
# 4) helper：构造 ToolContext(run_id="r1", step_n=1, agent_id="a1", session_id="s1",
#                              metadata=MappingProxyType({"skill_registry": registry}))
# 5) FakeAuditLog：events: list[(event_type, detail)]，write_sync 追加记录
```

---

## 7. 用例详情

### U1：死代码防复活——SkillRegistry 无 register_builtin_tools / _builtin_tools_registered

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 + R1 [P0] |
| 测试层级 | unit |
| 覆盖准则 | N/A（属性存在性检查） |
| Oracle | 直接断言（布尔） |
| Mock | 否 — 纯属性检查 |

**等价类划分**：属性存在性 {存在, 不存在} → 代表值 = 类属性名 `register_builtin_tools`、`_builtin_tools_registered`

**Given**：无

**When**：
- 检查 `hasattr(SkillRegistry, "register_builtin_tools")` 与 `hasattr(SkillRegistry, "_builtin_tools_registered")`
- 实例化 `registry = SkillRegistry()` 后复查实例属性

**Then**：
- `hasattr(SkillRegistry, "register_builtin_tools") is False`
- `hasattr(SkillRegistry, "_builtin_tools_registered") is False`
- `hasattr(registry, "_builtin_tools_registered") is False`
- 副作用：无

---

### U2：SkillToolFactory 生成的 search_skills 工具定义正确（name/tier/schema）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 + R2 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | N/A（工厂无分支） |
| Oracle | golden value（规格白纸黑字：name/tier/schema 可独立推导） |
| Mock | 否 — 无依赖 |

**等价类划分**：工厂产出列表 {空, 1 个} → 代表值 = `SkillToolFactory().create_tools()`

**Given**：无

**When**：`tools = SkillToolFactory().create_tools()`

**Then**：
- 返回值：`len(tools) == 1`；`tools[0].name == "search_skills"`
- `tools[0].tier == ToolTier.ALWAYS`
- `tools[0].input_schema["required"] == ["skill_name"]`
- `tools[0].policy.is_idempotent is True`；`tools[0].policy.sensitivity == SensitivityLevel.LOW`
- `tools[0].executor` 可调用（`callable(...) is True`）
- 副作用：无

---

### U3：search_skills executor 经 ctx.metadata["skill_registry"] 路由到 SkillRegistry 并成功返回

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 + R2 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：executor 主路径（metadata 读取 + search_skills 成功返回）；对应 skill.py:29-30 |
| Oracle | 参考行为链（executor 返回值 == registry.search_skills 的返回值） |
| Mock | 否 — SkillRegistry 真实现（Knowledge skill，不碰文件系统） |

**等价类划分**：Skill 类型 {Knowledge, Action} → 代表值 = Knowledge（避免文件系统，聚焦路由）；executor 返回值 = 匹配成功路径

**Given**：
- `registry = SkillRegistry(tool_registry=ToolRegistry())`
- `registry.register_skill(Skill(name="route-test", description="d", when_to_use="w", content="指引内容 $ARGUMENTS"))`
- `tools = SkillToolFactory().create_tools()`；`executor = tools[0].executor`
- `ctx = ToolContext(run_id="r1", step_n=1, agent_id="a1", session_id="s1", metadata=MappingProxyType({"skill_registry": registry}))`

**When**：`result = executor(ctx, skill_name="route-test")`

**Then**：
- 返回值：`result.success is True`；`result.tool_name == "search_skills"`
- `"route-test" in result.data`（匹配成功走正常加载路径）
- 副作用：`registry.get_active_skill_name() == "route-test"`（search_skills 写入激活状态）
- 无文件系统接触（Knowledge 无 script）

---

### U4：description 超长（>250）的 Action Skill 截断后 is_action 仍为 True、字段保留、Tool 正常生成

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 + R3 [P0] |
| 测试层级 | integration（真实文件系统脚本加载） |
| 覆盖准则 | branch：register_skill 的截断重建分支（registry.py:154-176，`len(description) > max` 为真） |
| Oracle | golden value（250 截断边界可手算）+ 副作用断言 |
| Mock | 否 — 真实 bridge + 真实脚本加载（修复②的证明必须走真实链路） |

**等价类划分**：description 长度 {≤250, >250} → 代表值 = 300 字符（`"长描述" * 100` 恰好 300 字符）；脚本加载 {成功} → 代表值 = 唯一 public function `run`

**Given**：
- `base = tmp_path`；写 `base / "tool.py"` 内容为 fixture 约定的 `run(query: str) -> str` 脚本
- `skill = Skill(name="trunc-action", description="长描述" * 100, when_to_use="w", content="c", source=SkillSource.USER, base_path=str(base), script="tool.py", entry_function="run")`

**When**：`registry.register_skill(skill)`（registry 带真实 ToolRegistry）

**Then**：
- `s = registry.get_skill("trunc-action")`：`s.is_action is True`（**修复②核心断言：未退化为 Knowledge**）
- `s.script == "tool.py"`；`s.entry_function == "run"`；`len(s.description) == 250`
- 副作用：`registry.get_action_tool_name("trunc-action") == "skill_trunc-action"`；`"trunc-action" in registry._action_tools_cache`（Tool 已预构建）
- ToolRegistry 此时**不含** `skill_trunc-action`（懒注册，未触发 search_skills）

---

### U5：同名 Action Skill 覆盖——旧 Tool 被注销、新 Tool 可重新懒注册、内容更新生效

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 + R4 [P1] |
| 测试层级 | integration |
| 覆盖准则 | branch：覆盖分支（`existing is not None` + `existing.is_action` 真 → cleanup，registry.py:191-192）+ `_lazy_register_action_tool` 幂等检查分支（registry.py:559，必须走到"未注册 → 重新注册"） |
| Oracle | 蜕变/副作用断言：**关键证明 = 第二次 search_skills 后 ToolRegistry 中的 Tool.description 是新版本**（若无 cleanup，幂等检查会跳过，description 仍是旧值 → 用例失败即暴露原 bug） |
| Mock | 否 — 真实脚本两版 + 真实 ToolRegistry |

**等价类划分**：覆盖来源优先级 {高} → v1=PROJECT(2)，v2=USER(3)；类型 {Action→Action}

**Given**：
- `base = tmp_path`；写两版脚本：`v1.py` 与 `v2.py`（内容不同，entry 均 `run`）
- v1 = `Skill(name="overlap", description="v1 desc", ..., source=SkillSource.PROJECT, base_path=str(base), script="v1.py", entry_function="run")`
- v2 = `Skill(name="overlap", description="v2 desc", ..., source=SkillSource.USER, base_path=str(base), script="v2.py", entry_function="run")`
- 注册 v1；用 U3 的 ctx（metadata 含 registry）调 `executor(ctx, skill_name="overlap")` → 触发懒注册，`tool_registry.get_tool("skill_overlap").description == "v1 desc"`

**When**：
- 注册 v2（覆盖）
- **断言点 A（清理完成）**：`tool_registry.get_tool("skill_overlap") is None`；`registry.get_action_tool_name("overlap") == "skill_overlap"`（映射已重建为新 Tool）
- 再次调 `executor(ctx, skill_name="overlap")`（触发重新懒注册）

**Then**：
- **断言点 B（新 Tool 生效，修复③核心证明）**：`tool_registry.get_tool("skill_overlap")` 非 None 且 `.description == "v2 desc"`；`.executor` 执行结果为 v2 脚本的输出（`f"processed:...` 或 v2 自定义返回）
- `registry.get_skill("overlap").description == "v2 desc"`；`registry._action_tools_cache["overlap"].description == "v2 desc"`
- `registry.version` 较初始 +2（v1 注册 +1，v2 覆盖 +1）
- 返回值：两次 executor 调用 `result.success is True`

---

### U6：unregister_skill 注销 Action Skill 后 ToolRegistry 与缓存全面清理

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 + R5 [P1] |
| 测试层级 | integration |
| 覆盖准则 | branch：unregister 主路径（registry.py:234-255）+ `_cleanup_action_tool` 双清理（ToolRegistry 注销 + 缓存 pop） |
| Oracle | 副作用断言（注销后各存储均不可再查到） |
| Mock | 否 — 真实链路 |

**等价类划分**：注销目标 {存在, 不存在} → 先测存在（主路径），随后测不存在（返回 False）

**Given**：
- 注册 Action Skill `ghost-action`（tmp_path 脚本，source=USER）
- 调 executor 触发懒注册 → `tool_registry.get_tool("skill_ghost-action")` 非 None，且已被 `promote_to_discovered`（`"skill_ghost-action" in tool_registry.discovery._discovered`）

**When**：`ok = registry.unregister_skill("ghost-action")`

**Then**：
- 返回值：`ok is True`
- `registry.get_skill("ghost-action") is None`
- `registry.get_action_tool_name("ghost-action") is None`（映射清空）
- `"ghost-action" not in registry._action_tools_cache`（缓存清空）
- `tool_registry.get_tool("skill_ghost-action") is None`；`"skill_ghost-action" not in tool_registry.list_tool_names()`
- `"skill_ghost-action" not in tool_registry.discovery._discovered`（发现状态已 undiscover）
- `registry.version` +1
- 补充断言：`registry.unregister_skill("ghost-action") is False`（不存在 → False，无副作用，version 不变）

---

### U7：低优先级同名覆盖被跳过，旧 Action Tool 不动（零副作用）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 + R6 [P1] |
| 测试层级 | integration（v1 为 Action，需脚本） |
| 覆盖准则 | branch：覆盖跳过分支（registry.py:181-187，`skill.source < existing.source` 为真） |
| Oracle | 副作用断言（跳过 = 全链路零变化） |
| Mock | 否 |

**等价类划分**：新来源优先级 {低} → v1=USER(3)，v2=BUILTIN(1)

**Given**：
- 注册 v1 = Action Skill `prio-guard`（source=USER，description="keep me"，tmp_path 脚本）
- 调 executor 触发懒注册 → `tool_registry.get_tool("skill_prio-guard").description == "keep me"`
- 记录 `v_before = registry.version`
- v2 = `Skill(name="prio-guard", description="intruder", ..., source=SkillSource.BUILTIN, base_path=str(base), script="v2.py", entry_function="run")`

**When**：`registry.register_skill(v2)`

**Then**：
- `registry.get_skill("prio-guard").source == SkillSource.USER`（仍是 v1）
- `registry.get_skill("prio-guard").description == "keep me"`（内容未被覆盖）
- `tool_registry.get_tool("skill_prio-guard")` 非 None 且 `.description == "keep me"`（**旧 Tool 未被 cleanup，R6 直接证明**）
- `registry._action_tools_cache["prio-guard"].description == "keep me"`
- `registry.version == v_before`（version 不变 = 零副作用锚点）

---

### U8：_cleanup_action_tool 在 tool_registry=None 时不抛异常且清理缓存

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 + R7 [P2] |
| 测试层级 | unit（直接调私有方法） |
| 覆盖准则 | branch：`_cleanup_action_tool` 的 `tool_registry is None` 分支（registry.py:577）+ 缓存 pop 分支 |
| Oracle | 不抛异常 + 缓存清空（副作用断言） |
| Mock | 否 — 无依赖（tool_registry 显式 None） |

**等价类划分**：tool_registry {None} × 缓存状态 {空, 有残留} → 代表值 = None + 残留缓存（最严苛组合）

**Given**：
- `registry = SkillRegistry(tool_registry=None)`
- 手动植入残留：`registry._action_tools_cache["ghost"] = object()`；`registry._action_skill_tools["ghost"] = "skill_ghost"`

**When**：`registry._cleanup_action_tool("ghost")`

**Then**：
- 不抛任何异常（try/except 吞 ToolRegistry 注销错误路径已由 None 短路验证）
- `"ghost" not in registry._action_tools_cache`；`"ghost" not in registry._action_skill_tools`（缓存仍被清理）
- 补充：`registry._cleanup_action_tool("不存在的skill")` 也不抛异常

---

### U9：Knowledge → Knowledge 同名覆盖不触发 cleanup，无 Tool 副作用

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 + R8 [P2] |
| 测试层级 | component(fake)（无脚本，纯内存） |
| 覆盖准则 | branch：覆盖分支中 `existing.is_action` 为假 → cleanup 不执行（registry.py:191 条件假分支） |
| Oracle | 副作用断言（无任何 Tool 相关状态变化） |
| Mock | 否 |

**等价类划分**：existing 类型 {Knowledge} → v1/v2 均 script=None

**Given**：
- v1 = `Skill(name="kb", description="v1", when_to_use="w", content="c1", source=SkillSource.USER)`
- 注册 v1；`v_before = registry.version`
- v2 = `Skill(name="kb", description="v2", when_to_use="w", content="c2", source=SkillSource.PROGRAMMATIC)`

**When**：`registry.register_skill(v2)`

**Then**：
- `registry.get_skill("kb").description == "v2"`（覆盖成功）
- `registry.get_action_tool_name("kb") is None`；`"kb" not in registry._action_tools_cache`；`"skill_kb" not in tool_registry.list_tool_names()`（无 Tool 副作用）
- `registry.version == v_before + 1`（仅覆盖 +1，无额外变更）

---

### U10：覆盖成功写 SKILL_OVERRIDDEN 审计；低优先级跳过不写

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P3]（辅助 inv-4/inv-2 的观测证明） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_write_audit_skill_overridden` 调用分支（覆盖成功路径）+ 跳过分支（不调用） |
| Oracle | FakeAuditLog 事件列表断言 |
| Mock | FakeAuditLog（记录 write_sync 调用） |

**等价类划分**：覆盖结果 {成功, 跳过} → 两种各 1 代表

**Given**：
- `audit = FakeAuditLog()`；registry 注入 audit
- 场景 A：v1=Knowledge(USER)，v2=Knowledge(PROGRAMMATIC) 覆盖成功
- 场景 B：v1=Knowledge(USER)，v2=Knowledge(BUILTIN) 覆盖跳过

**When**：依次执行 A、B 的 `register_skill`

**Then**：
- 场景 A：`audit.events` 含 1 条 `AuditEventType.SKILL_OVERRIDDEN`，`detail` 含 `"USER → PROGRAMMATIC"`（格式 `f"{old.source.name} → {new.source.name}"`，registry.py:757-760）
- 场景 B：`audit.events` 中无新增 SKILL_OVERRIDDEN 事件

---

### U11：builder 装配——_resolve_skill_registry 将 SkillToolFactory 注册的 search_skills 挂到 ToolRegistry

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 + R2 [P0]（生产注册路径，改动①的死代码替代路径） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_resolve_skill_registry` 非空路径（builder.py:1102-1122）+ `register_builtin_factories` 调用 |
| Oracle | golden value（search_skills 工具在 ToolRegistry 中可查） |
| Mock | 否 — 直接构造 `AgentBuilder()` 调私有方法（构造无参，无需 mock） |

**等价类划分**：`_skill_list` {空, 非空} → 代表值 = 1 个 Knowledge Skill；空列表路径顺带断言（返回 None）

**Given**：
- `builder = AgentBuilder()`；`builder._skill_list = [Skill(name="asm", description="d", when_to_use="w", content="c")]`
- `tool_registry = ToolRegistry()`；`audit = FakeAuditLog()`

**When**：`registry = builder._resolve_skill_registry(audit, tool_registry)`

**Then**：
- 返回值：`registry` 非 None，且 `registry.skill_count() == 1`
- 副作用：`tool_registry.get_tool("search_skills")` 非 None，`.name == "search_skills"`，`.tier == ToolTier.ALWAYS`
- 补充：`builder2 = AgentBuilder()`（`_skill_list` 空）→ `builder2._resolve_skill_registry(audit, tool_registry) is None`

---

### U12：Action Skill 脚本缺失 → 注册不抛异常、不留下半残缓存（SK7 Fail-Safe）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R10 [P3]（Action Skill 桥接路径既有 Fail-Safe 回归，防覆盖场景叠加出新问题） |
| 测试层级 | integration（真实文件系统查询，脚本不存在） |
| 覆盖准则 | branch：`_register_action_tool` 的 except 分支（registry.py:271-275） |
| Oracle | 副作用断言（SK7：Skill 退化为 Knowledge 使用，缓存无残留） |
| Mock | 否 — 真实 bridge 抛 SkillScriptError |

**等价类划分**：脚本加载 {失败} → 代表值 = `base_path` 指向不存在文件 `missing.py`

**Given**：
- `skill = Skill(name="broken", description="d", when_to_use="w", content="c", base_path=str(tmp_path), script="missing.py", entry_function="run")`

**When**：`registry.register_skill(skill)`（带真实 ToolRegistry）

**Then**：
- 不抛异常（SK7 Fail-Safe）
- `registry.get_skill("broken")` 非 None 且 `is_action is True`（Skill 定义保留）
- `"broken" not in registry._action_tools_cache`；`registry.get_action_tool_name("broken") is None`（**无半残缓存**）
- `"skill_broken" not in tool_registry.list_tool_names()`

---

### U13：_cleanup_action_tool 注销失败吞异常（E4 Fail-Safe）——主流程不阻塞 + 缓存仍清理 + debug 留痕

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-10 + R11 [P2] |
| 测试层级 | 场景 A：unit（直接调私有方法）；场景 B：integration（真实覆盖链路 + monkeypatch） |
| 覆盖准则 | branch：`_cleanup_action_tool` 的 try/except 异常分支（registry.py:578-584，`unregister_tool` 抛异常为真） |
| Oracle | 副作用断言（不抛异常 + 缓存清空 + 主流程完成）+ caplog 留痕断言（debug 级日志含 skill 名） |
| Mock | 场景 A：`ExplodingToolRegistry` stub（仅实现 `unregister_tool`，抛 `RuntimeError`）；场景 B：真实 ToolRegistry + `monkeypatch` 其 `unregister_tool` 抛异常 |

**等价类划分**：注销失败 {抛异常} × 调用场景 {直接调私有方法, 覆盖路径} → 两场景各 1 代表

**场景 A（unit，直接调私有方法）**

**Given**：
- `registry = SkillRegistry(tool_registry=ExplodingToolRegistry())`
- 手动植入：`registry._action_skill_tools["ghost"] = "skill_ghost"`；`registry._action_tools_cache["ghost"] = object()`

**When**：`registry._cleanup_action_tool("ghost")`（caplog 开启 DEBUG）

**Then**：
- 不抛任何异常（E4 吞掉 RuntimeError）
- `"ghost" not in registry._action_tools_cache`；`"ghost" not in registry._action_skill_tools`（**pop 在 try 外，异常后仍清理缓存**）
- caplog 含 debug 记录，文案含 `skill_ghost`（注销失败留痕，非静默）

**场景 B（integration，覆盖路径不阻塞）**

**Given**：
- 真实 ToolRegistry；`monkeypatch.setattr(tool_registry, "unregister_tool", _explode)`（`_explode` 抛 RuntimeError）
- v1 = Action Skill `overlap`（tmp_path 脚本，source=PROJECT）；注册 v1 并调 executor 触发懒注册 → `tool_registry.get_tool("skill_overlap")` 非 None
- `v_before = registry.version`
- v2 = Action Skill `overlap`（新脚本，source=USER）

**When**：`registry.register_skill(v2)`（覆盖路径触发 `_cleanup_action_tool` → unregister 抛异常被吞）

**Then**：
- 不抛异常（覆盖主流程未被 E4 阻断）
- `registry.get_skill("overlap").description == "v2 desc"`（覆盖成功）
- `registry.version == v_before + 1`（覆盖 version 正常 +1）
- `registry._action_tools_cache["overlap"].description == "v2 desc"`（缓存已重建为新 Tool——cleanup 后 `_register_action_tool` 正常执行）
- `registry._action_tools_cache["overlap"].executor(ctx, query="hello").data == "processed_v2:hello"`（新 Tool 本体执行新脚本）
- `registry.get_action_tool_name("overlap") == "skill_overlap"`（映射已重建）
- 注：旧 Tool 因 unregister 失败残留于 ToolRegistry，`_lazy_register_action_tool` 幂等检查将跳过新 Tool 再注册——这是 E4 降级的**显式留痕**后果（debug 日志），非静默失败；下次同场景 cleanup 成功时自然收敛（完整收敛链路由 U5 断言点 A/B 验证，两用例互证）

---

## 8. 覆盖准则达成表（Branch 目标自查）

| 分支点 | 覆盖用例 |
|---|---|
| register_skill：截断重建（len>max 真） | U4 |
| register_skill：name 非法/校验异常（既有，非本次改动） | 未覆盖（非本次范围，标注） |
| register_skill：覆盖成功（existing 非 None + source≥） | U5 / U9 / U10 |
| register_skill：覆盖跳过（source<） | U7 |
| register_skill：existing.is_action 真 → cleanup | U5 |
| register_skill：existing.is_action 假 → 不 cleanup | U9 |
| _lazy_register_action_tool：幂等已注册 → 跳过 | 隐含于 U5 断言点 A 前状态（旧 Tool 在册） |
| _lazy_register_action_tool：未注册 → 注册 | U5 断言点 B |
| _cleanup_action_tool：tool_registry None | U8 |
| _cleanup_action_tool：注销失败吞异常（E4） | U13 |
| unregister_skill：存在 / 不存在 | U6 |
| search_skills：匹配成功 + Action 懒注册 | U3 / U5 |
| builder._resolve_skill_registry：空 / 非空 | U11 |

**豁免声明**：
- 故障注入：U1/U2/U3/U9/U10/U11 无外部 I/O 依赖 → 该类不适用（纯内存/纯函数）。
- name 格式校验 / content 为空等既有校验分支：非本次改动范围，豁免（防止范围蔓延）。
- `_cleanup_action_tool` 注销失败吞异常分支（E4）：~~v1 豁免~~ → **v2 已由 U13 覆盖**（消除上一版的技术债豁免）。

---

## 9. 已知差距（Known-Gap）

无。源码（registry.py:161-176 透传字段、191-192 cleanup、567-588 双清理 + E4 try/except）与设计预期一致，无需 xfail 标记。U13 已消除 E4 分支的覆盖豁免（见 §8）。

---

## 10. 修订记录

- v1（本次）：基于 registry.py / skill.py / builder.py:1100-1122 白盒分析产出。
- v2（U13 补充）：新增 U13 用例覆盖 `_cleanup_action_tool` 注销失败吞异常分支（inv-10 + R11, P2），
  消除 §8 中 E4 分支的覆盖豁免（见 §8 豁免声明与 §9）；设计文档与测试文件头同步更新为 U1~U13。
