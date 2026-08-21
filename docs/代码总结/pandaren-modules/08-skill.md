# 08 — pandaren/skill（技能：按需知识注入 + Action 执行）

> 模块总结 · 以代码为准（不依赖外部设计文档）· 锚点均为本次核实的 file:line
> 生成时点：2026-08-18 @ git HEAD（98e755d 附近）+ 工作区未提交改动（见 §11）

## 0. 元信息

> 模块：`pandaren/skill` | 生成：2026-08-18 | 锚点以生成时点代码为准
> ⚠️ `registry.py` 含**工作区未提交改动**（git diff 对应遗留设计文档 `tests/design/skill_registry.design.md` 的改动①②③，见 §11）；本文档锚点按工作区版本（769 行）核对
> 变更历史见 §11（git log 仅 2 条）

## 1. 模块定位与职责（存在的意义）

**一句话**：pandaren 的「按需知识注入 + 动作执行」能力——让 LLM 在**需要时**用一次工具调用把专项知识/操作流程精准注入上下文，并让带脚本的 Skill（Action Skill）自动变成可直接调用的 Tool，不必在每轮对话里背着所有技能文本。

它是三件套对称设计的第二件（`skill/__init__.py:1-20`）：

| 件 | 一句话 | 回答的问题 |
|----|--------|-----------|
| Tool | 我能做什么（原子操作） | 我能直接执行什么动作 |
| **Skill（本模块）** | 我能知道什么（知识注入） | 我该带什么知识进上下文 |
| Agent | 我能委托谁（任务委派） | 什么活该交给专门的人干 |

**不建它会怎样**：所有技能/领域知识一股脑塞进 system prompt——上下文被几十个 SKILL.md 占满、token 预算爆掉、LLM 注意力被稀释（"知识太多反而不知道用哪个"）；带脚本的可执行技能无从表达参数 schema，只能退化成"让 LLM 读文本自己执行"，不可控不可审计。

**角色分工**（各文件职责边界）：
- `models.py` —— 5 个数据模型（Skill/SkillResult/SkillSummary/SkillSource/SkillType，全部 frozen 不可变）
- `registry.py` —— ★ 运行时核心：注册校验、同名覆盖、摘要注入、search_skills 一步加载、门禁、Turn 级激活、审计
- `bridge.py` —— SkillToolBridge：Action Skill 的 script → 标准 Tool 的纯转换器（类型推导/executor 包装）
- `loader.py` —— Markdown（YAML Frontmatter + body）→ Skill 解析器（单文件 + 目录批量）
- `script_loader.py` —— Python 脚本**安全**加载（路径遍历防护 + 模块缓存 + 入口函数检测）
- `exceptions.py` —— SkillRegistrationError / SkillScriptError
- `tool/builtin/skill.py`（本模块外）—— search_skills 的 ALWAYS 级 Tool 工厂，executor 经 `ctx.metadata["skill_registry"]` 路由回 registry

覆盖文件与测试清单：

| 文件 | 行数 | 角色 |
|------|------|------|
| `pandaren/skill/__init__.py` | 39 | 对外导出面（11 项：7 模型/类 + 2 函数 + 2 异常） |
| `pandaren/skill/models.py` | 104 | 5 个 frozen 数据模型 + 2 枚举 |
| `pandaren/skill/registry.py` | 769 | ★ SkillRegistry：注册/覆盖/摘要/加载/门禁/激活/审计 |
| `pandaren/skill/bridge.py` | 284 | SkillToolBridge：Action Skill → Tool 转换器 |
| `pandaren/skill/loader.py` | 227 | Markdown Skill 解析（单文件 + 目录批量） |
| `pandaren/skill/script_loader.py` | 187 | 脚本安全加载：防路径遍历 + 缓存 + 入口检测 |
| `pandaren/skill/exceptions.py` | 17 | SkillRegistrationError / SkillScriptError |
| `tests/test_skill_registry.py` | 355 | 12 tests（U1~U12）：覆盖 inv-1~9 + R1~R10 |
| `tests/design/skill_registry.design.md` | 480 | 本次改动测试设计（不变式清单 + 风险清单） |
| `tests/conftest.py` | ~90 | fixtures：make_registry/make_context/write_skill_script/fake_audit/tool_registry |

---

## 2. 方案总览（产品视角）

### 2a. 在什么场景下解决什么问题（场景穷举）

| 场景 | 已有/缺失 | 该场景下的问题（业务语言） |
|------|-----------|---------------------------|
| 给 Agent 注入领域知识（设计规范/操作手册/方法论） | 已有 | SKILL.md（Frontmatter + 正文）→ `skills_from_dir()` 批量注册 → system prompt 里只有 ≤1% 预算的摘要，LLM 需要时 `search_skills(name)` 一步加载全文 |
| 技能很多（几十个），不能全塞上下文 | 已有 | 摘要受 1% 上下文预算约束（SK4，registry.py:389），超出从低优先级裁剪 |
| 一个可执行技能（如"评估某公司估值"）要能被 LLM 直接调用且带参数 schema | 已有 | Action Skill：script 字段声明 Python 文件 + entry_function，bridge 自动生成带完整 JSON Schema 的 Tool，LLM 可一步直调（models.py:71-77, bridge.py:60-121） |
| 同名技能多来源注册（内置/项目/用户/代码），谁覆盖谁要可控 | 已有 | SkillSource 优先级 BUILTIN<PROJECT<USER<PROGRAMMATIC，高优先级覆盖低优先级，覆盖写审计（registry.py:178-198） |
| 防止 LLM 在用户没要求时擅自用"高风险"技能 | 已有 | `allow_auto_trigger=False` 门禁：LLM 自动触发被拒，只有用户手动触发（Phase 0.5 标记 `_manually_requested`）才放行（SK3，registry.py:454-470） |
| 技能执行期间要限制工具面（只能调该技能允许的工具） | 已有 | `allowed_tools` Turn 级白名单（SK2）：search_skills 成功时激活，每轮结束 clear_active_skill 清除（registry.py:487-492, 538-546） |
| 技能脚本要防恶意路径遍历（SKILL.md 的 script 指向 base_path 之外） | 已有 | script_loader resolve + startswith 校验，越界即 SkillScriptError（script_loader.py:54-62） |
| 想审计"谁加载了技能、加载了多少 token、谁被拒" | 已有 | 3 类审计事件：SKILL_INVOKED / SKILL_AUTO_TRIGGER_DENIED / SKILL_OVERRIDDEN（registry.py:705-763） |
| 一个技能 description 超长（>250 字符） | 已有 | 自动截断 + WARNING；截断重建时透传全部字段，Action Skill 不退化（registry.py:153-176，改动②） |
| 同名 Action Skill 被更高优先级覆盖 | 已有 | 覆盖前 `_cleanup_action_tool` 注销旧 Tool，防止懒注册幂等检查跳过新 Tool（registry.py:188-192，改动③） |
| 技能加载失败（脚本缺失/损坏）不影响 Agent 启动 | 已有 | SK7 Fail-Safe：Tool 构建失败仅 WARNING，Skill 退化为 Knowledge 使用（registry.py:271-275） |
| 技能标签搜索/模糊匹配 | **缺失** | `_match_skills` 仅 name 精准 + 大小写容错（registry.py:614-637）；tags 字段存在但未被搜索使用 |
| 通用变量渲染（$VAR 体系） | **缺失** | `_render_content` 只替换 `$ARGUMENTS` 一个占位符（registry.py:655-670） |
| 技能热更新（改脚本不重启生效） | **缺失** | script_loader 模块缓存永久生效，仅测试可 clear_cache（script_loader.py:184-187） |

### 2b. 总体方案思路

| 关键思路 | 回答的问题 | 核心机制 |
|---------|-----------|---------|
| 摘要注入 ≠ 全文注入（1% 预算） | "几十个技能塞 system prompt 会爆上下文吗？" | `build_skill_summaries()` 按 token 估算裁剪，低优先级先裁（§5-6） |
| 搜索即加载（一步到位） | "LLM 找到技能后还要再调一次吗？" | `search_skills(name, ctx)` 一次完成匹配+门禁+渲染+提升+激活+审计（§5-4） |
| Action Skill 双阶段注册 | "Tool 什么时候进 ToolRegistry？" | register 时预构建缓存（不进 Registry）→ search_skills 时才懒注册 + promote（KD3）（§5-3） |
| 同名覆盖按来源优先级仲裁 | "内置和用户都定义了同名技能谁说了算？" | SkillSource IntEnum 比较，低优先级跳过零副作用，高优先级覆盖 + 审计（§5-2） |
| 门禁放行一次性消费 | "手动触发标记会被滥用吗？" | `_manually_requested` 一次性 discard，Turn 级生命周期（registry.py:456-462, 538-546） |
| 脚本加载安全默认拒绝 | "恶意 SKILL.md 能把脚本引到任意路径吗？" | resolve + startswith + .py 后缀三重校验（§5-8） |
| 转换器无状态、executor 永不抛 | "Action Skill 执行失败会炸 Agent 循环吗？" | bridge 的 executor try/except 全包，超时/异常 → ToolResult(success=False)（§5-3） |

---

## 3. 产品视角

### 3a. 使用场景与用户旅程

**谁**：应用开发者（通过 AgentBuilder 声明技能、写 SKILL.md）+ 最终用户（间接受益）。

**典型旅程（知识技能）**：开发者在 `resources/skills/` 写 `arch-design/SKILL.md`（声明 name/description/when_to_use + 正文）→ `AgentBuilder.skills_from_dir(...)` 注册 → 用户提需求"帮我设计这个模块" → 主 Agent 的 system prompt 里出现 `<available_skills>` 摘要（仅 name + when_to_use，占 ≤1% 窗口）→ LLM 判断"该用 arch-design" → 调用 `search_skills(skill_name="arch-design")` → registry 匹配、过门禁、渲染正文、写审计 → 技能全文注入上下文 → 后续轮次 LLM 按技能指引行事。

**典型旅程（Action 技能）**：开发者在 SKILL.md 里加 `script: tool.py` + `entry_function: run` → 注册时 bridge 预构建 Tool（DEFERRED 级，含函数签名推导的 JSON Schema）但不进 ToolRegistry → 用户触发加载后，Tool 被懒注册 + promote 到 discovered，LLM 可直接调用 `skill_<name>(参数)` 完成动作（如估值计算），结果以 ToolResult 回传。用户看到的是"技能加载 → 工具执行 → 结果"一条完整链路。

### 3b. 量化价值与反面案例

**反面案例（无此模块）**：
- 所有技能全文拼进 system prompt → 假设 30 个技能 × 平均 2K tokens = 60K tokens，128K 窗口一半被技能占掉，对话历史被压缩；且技能一多 LLM 选择困难（相关性稀释）。
- Action 技能无 schema → LLM 只能"读文本自己动手"，参数靠猜、失败不可审计、执行不可控。
- 无门禁 → LLM 在闲聊中擅自触发"仅用户可用的内部流程技能"。

**量化收益**：
- 摘要预算：默认 1%（128K → 1280 tokens），可支撑几十个技能（registry.py:389）。
- 加载成本：一次 search_skills 调用换取全文注入，且只在需要时发生（按需，非每轮）。
- Action 桥接：注册时 0 次 LLM 调用、0 次 ToolRegistry 变更；首次使用时一次懒注册 + promote 完成（registry.py:548-565）。

### 3c. 产品地图定位

**能力域**：Agent 感知与执行增强（知识注入 + 可执行技能）。
**上游依赖**：identity（无直接依赖，间接经 Tool 层）、tool（Tool/ToolContext/ToolResult/ToolRegistry/ToolTier）、observability（AuditLog + AuditEventType）、constants（CHARS_PER_TOKEN）。
**下游服务**：engine（Phase 1 摘要注入 + Phase 0.5 手动触发 + 每轮 clear_active_skill）、builder（`_resolve_skill_registry` 装配，builder.py:1100-1122）、`tool/builtin/skill.py`（search_skills 工厂）。

关系链：`builder 装配 registry` → `engine 每轮注入摘要` → `LLM 决策 search_skills` → `registry 门禁+渲染+激活` → `bridge 生成 Tool` → `ToolRegistry 提升` → `LLM 直调 Action Tool`。

### 3d. 能力边界与承诺

**能承诺（代码强制保证）**：
1. 注册后 Skill 不可变（frozen dataclass，SK1）；description 超长截断且**不透传字段即静默退化**的问题已被改动②修复
2. 同名覆盖按来源优先级仲裁，覆盖写审计（SKILL_OVERRIDDEN）
3. 自动触发受 `allow_auto_trigger` 门禁，手动请求一次性消费（SK3）
4. Skill 激活期间工具面受限（allowed_tools 白名单），Turn 结束清除（SK2）
5. Action Skill 脚本路径防遍历（resolve + startswith）、仅 .py、executor 永不向外抛异常（E4）
6. 摘要受 1% 上下文预算约束（SK4）
7. 脚本加载失败 Fail-Safe：Skill 退化为 Knowledge，不阻断 Agent 启动（SK7）

**明确不做**：
1. 不做模糊/标签搜索（`_match_skills` 仅 name 精准 + 大小写容错，registry.py:614-637）
2. 不做通用变量渲染（仅 `$ARGUMENTS`）
3. 不做脚本热更新（模块缓存永久）
4. 不规定技能目录——`SkillSource` 只表达来源与优先级，目录由应用层 `skills_from_dir(dir, source=...)` 决定（models.py:26-33，注释明说此前硬编码 `.pandaren/skills/` 是错的）

### 3e. 用户视角的失败体验

| 用户操作 | 失败表现 | 代码路径 |
|---------|---------|---------|
| LLM 想用但门禁拒绝 | 返回 success=False + 提示"需要手动触发"，写 SKILL_AUTO_TRIGGER_DENIED 审计 | registry.py:463-470 |
| 技能名拼错 | success=True + data="未找到名称为 'xxx' 的技能"（不报错，LLM 可重试） | registry.py:445-450 |
| 脚本缺失 | 注册不报错，Skill 保留为 Action 定义但无 Tool 缓存；LLM 加载时拿到 content 但无 `skill_xxx` 工具可调 | registry.py:271-275 + U12 |
| description 超长 | 自动截断（WARNING 留痕），不失败 | registry.py:154-159 |
| name 非法（含空格/特殊字符） | 拒绝注册 + WARNING（不抛异常，防止一个坏 Skill 拖垮 Agent 启动） | registry.py:128-136 |

### 3f. 成熟度与演进路线

| 阶段 | 状态 | 说明 |
|------|------|------|
| 基础（知识注入 + 摘要预算 + 门禁 + 激活） | 已有 | 三件套设计中的成熟件 |
| Action Skill 桥接（脚本 → Tool） | 已有 | 双阶段注册 + 懒提升，改动②③修补了静默退化/覆盖残留两个坑 |
| 测试体系 | 已有 | U1~U12 全过（12/12），design 文档 inv-1~9 + R1~R10 |
| 演进候选 | 缺失 | 标签搜索、通用变量渲染、脚本热更新/缓存失效、`_lazy_register_action_tool` 日志被注释掉的可观测性恢复（见 §9 P2/P3） |

---

## 4. 模块整体框架

```
                    ┌──────────────────────────────────────────────┐
                    │                 AgentBuilder                 │
                    │    _resolve_skill_registry (builder.py:1100) │
                    └──────────────┬───────────────────────────────┘
                                   │ 构造 + 装配
        ┌──────────────────────────▼──────────────────────────────┐
        │                    SkillRegistry (registry.py:42)       │
        │  ┌────────────┐  ┌──────────────┐  ┌─────────────────┐  │
        │  │ _skills    │  │ Action 桥接   │  │ Turn 激活状态    │  │
        │  │ dict[name] │  │ _bridge      │  │ _active_*       │  │
        │  │ → Skill    │  │ _action_tools_cache / _action_skill_tools │
        │  └────────────┘  └──────────────┘  └─────────────────┘  │
        └──┬──────────┬──────────┬──────────┬──────────┬─────────┘
           │          │          │          │          │
   loader.py│  bridge.py│ script_loader.py│ tool/builtin/skill.py│  observability
   Markdown │  Skill→Tool│ 脚本安全加载     │ search_skills Tool   │  AuditLog
   (Frontmatter) │ 转换器 │                  │ (ALWAYS, ctx.metadata)│  (3 类事件)
```

**依赖方向**：`registry → bridge → script_loader`；`registry → models/exceptions`；`tool/builtin/skill → registry.search_skills`（运行时经 metadata，编译期无依赖）。

---

## 5. 核心机制详解

### 5-1. 注册校验与截断重建（registry.py:109-176）

`register_skill` 校验链（SK5）：
1. `name` 非空 → 格式正则 `^[a-zA-Z0-9\u4e00-\u9fff][a-zA-Z0-9_\-\u4e00-\u9fff]*$`（字母/数字/中文开头，仅字母数字连字符下划线，registry.py:39）→ **非法即拒绝注册 + WARNING（不抛异常）**
2. `content` / `description` / `when_to_use` 非空 → 缺失即 `SkillRegistrationError`
3. `description` > `max_description_chars`(250) → 截断 + WARNING，**frozen 不可改 → 重建新 Skill 实例**。⚠️ 关键点（改动②）：重建时必须透传全部字段含 `script`/`entry_function`——否则 Action Skill 静默退化为 Knowledge（`is_action` 变 False，registry.py:161-176）

### 5-2. 同名覆盖优先级语义（registry.py:178-198）

```
existing = _skills.get(name)
├─ 无 → 直接注册
└─ 有 → new.source < existing.source → WARNING + return（跳过，零副作用：Tool 不动、缓存不动、version 不变）
      → new.source >= existing.source → 覆盖：
          ① existing.is_action → _cleanup_action_tool(name)   // 改动③：先注销旧 Tool
          ② _write_audit_skill_overridden(new, existing)      // SKILL_OVERRIDDEN 审计
          ③ _skills[name] = new
```

改动③ 的意义：没有 cleanup 时，同名覆盖后 ToolRegistry 残留旧 Tool，`_lazy_register_action_tool` 的幂等检查（`get_tool(full_name) is not None`）会**永远跳过**新 Tool 的注册——内容更新不生效且静默（R4）。U5 测试直接证明。

### 5-3. Action Skill 双阶段桥接（registry.py:207-209, 257-275, 548-565 + bridge.py）

**阶段一（register 时）**：`_register_action_tool` → bridge.create_tool 预构建 Tool 对象，缓存到 `_action_tools_cache` + 记录 `_action_skill_tools[name] = tool.full_name`（= `skill_<name>`，namespace="skill"）——**不注册进 ToolRegistry**（LLM 必须先 search_skills 加载指令，不能绕过知识直接调工具）。SK7 Fail-Safe：构建失败仅 WARNING，Skill 退化为 Knowledge。

**阶段二（search_skills 时）**：`_lazy_register_action_tool` → 幂等检查（已注册跳过）→ register_tool + `_promote_deferred_tools`（KD3：DEFERRED → discovered）。

**bridge 的 Tool 定义**（bridge.py:96-113）：`tier=DEFERRED`、`namespace="skill"`、`policy=Policy(LOW, reversible, no-audit, idempotent, read_only)`——策略全保守，纯计算型技能。

**executor 包装**（bridge.py:189-256）：
- async 函数：有 running loop → ThreadPoolExecutor 里 `asyncio.run(wait_for(..., 60s))`；无 loop → 直接 asyncio.run
- sync 函数：直接调用
- 超时（TimeoutError）→ ToolResult(success=False, error="执行超时")
- 任意异常 → ToolResult(success=False, error=f"执行失败: {e}") + logger.error(exc_info=True)
- 结果统一 `str(result)`（None → ""）

**Schema 推导**（bridge.py:123-187）：`get_type_hints` + `inspect.signature`；跳过 ToolContext 参数（ctx/context/self/cls）；`_TYPE_MAP`：str/int/float/bool/list/dict → string/integer/number/boolean/array/object（未知类型默认 string）；无默认值 → required；docstring Args 段提取描述（Google 风格）。

### 5-4. search_skills 七步管线（registry.py:414-516）

```
1. _match_skills(skill_name)    精准匹配 + 大小写容错（0/1 个结果）
2. 门禁：_manually_requested 命中 → 一次性 discard + 放行
       否则 _check_auto_trigger → False → SKILL_AUTO_TRIGGER_DENIED 审计 + success=False
3. _render_content：$ARGUMENTS → skill_name（Fail-Safe：异常返回原文）
4. KD3 提升：allowed_tools + Action tool_name → _promote_deferred_tools
5. _activate_skill_tools：白名单（多 Skill 激活取并集；任一 None → 整体不限制）
6. _active_skill_name = name + SKILL_INVOKED 审计（含 content_tokens）
7. 返回 ToolResult（Action Skill 附带"请直接调用 skill_xxx"指引）
```

### 5-5. 门禁与手动触发（registry.py:310-361, 454-470）

- LLM 自动路径：`search_skills` 中 `_check_auto_trigger(skill, is_auto=True)` 检查 `allow_auto_trigger`
- 用户手动路径（Phase 0.5）：`invoke_skill_manually(name)` 只做两件事——验证存在 + 标记 `_manually_requested`，返回 hint 文案引导 LLM 调 search_skills（复用同一条管线）。search_skills 读到标记 → `discard`（一次性消费防滥用）→ 跳过门禁
- `_manually_requested` 与激活状态同生命周期：`clear_active_skill()` 一并清空（Turn 级）

### 5-6. 摘要预算（registry.py:367-408）

- 排序：source 高优先级在前（reverse=True）
- 预算：`int(context_window * 0.01)`（默认 128K → 1280 tokens）
- 条目 token 估算：`(len(name) + len(desc)) // CHARS_PER_TOKEN + 5`，desc 来自 `when_to_use`（非 description！）截断到 250
- 超预算 → `continue`（跳过该技能，低优先级先被裁），有 DEBUG 日志

### 5-7. 审计三事件（registry.py:705-763）

| 事件 | 触发 | 内容 |
|------|------|------|
| `SKILL_INVOKED` | search_skills 成功 | skill name + content_tokens + agent/run/session/step_n |
| `SKILL_AUTO_TRIGGER_DENIED` | 门禁拒绝 | skill name + 上下文 |
| `SKILL_OVERRIDDEN` | 同名覆盖成功 | name + old.source → new.source |

全部 Fail-Safe：`_audit_log is None` 或写入异常 → warning 不阻断（audit_log 由 pandaren 侧保证 HC4，此处是上层注入）。

### 5-8. 脚本安全加载（script_loader.py:35-108, 111-181）

- **防路径遍历**：`base.resolve()` 后 `(base / script_relative).resolve()` 必须 startswith(base)，否则 SkillScriptError（script_loader.py:54-62）
- **文件类型**：仅 `.py`（:64-68）
- **模块缓存**：`_loaded_modules: dict[path → ModuleType]` + `threading.Lock`；命名隔离 `_pandaren_skill_<md5:8>_<stem>`（:79-101）
- **入口函数解析**：显式 entry_name → 精确查找；自动 → 扫描模块内 public 函数（跳过 `_` 开头、跳过 import 进来的），唯一候选用之，多个候选优先唯一 async，仍歧义 → 报错要求显式指定（:111-181）
- **clear_cache()**：仅测试用（:184-187）

---

## 6. 对外能力清单

### 6a. API 表（`__init__.py:22-39` 导出面）

| 符号 | 类型 | 一句话 |
|------|------|--------|
| `Skill` / `SkillResult` / `SkillSummary` | frozen dataclass | 知识包定义 / 调用结果 / 摘要 |
| `SkillSource` / `SkillType` | IntEnum | 来源优先级（BUILTIN=1..PROGRAMMATIC=4）/ 类型 |
| `SkillRegistry` | class | 运行时核心 |
| `SkillToolBridge` | class | Action Skill → Tool 转换器 |
| `load_skill_from_file(path, source)` | function | 单文件加载（默认 PROJECT） |
| `load_skills_from_dir(dir, source, pattern="SKILL.md", recursive=True)` | function | 目录批量加载 |
| `SkillRegistrationError` / `SkillScriptError` | Exception | 注册失败 / 脚本失败 |

### 6b. 关键契约

| 契约 | 值 | 位置 |
|------|-----|------|
| `Skill.is_action` | `script is not None` | models.py:75-77 |
| Action Tool full_name | `skill_<name>`（namespace="skill"） | bridge.py:98-113 |
| Tool tier | DEFERRED（search_skills 前不可见） | bridge.py:103 |
| description 上限 | 250 字符（`_DEFAULT_MAX_DESCRIPTION_CHARS`） | registry.py:34 |
| 摘要预算 | 1% 上下文 | registry.py:389 |
| Action executor 超时 | 60s（`_DEFAULT_EXECUTION_TIMEOUT`） | bridge.py:45 |
| `$ARGUMENTS` 渲染 | content 中替换为 skill_name | registry.py:655-670 |
| version 语义 | register/unregister 成功 +1；被跳过的覆盖不变 | registry.py:211/254/187 |
| name 正则 | `^[a-zA-Z0-9\u4e00-\u9fff][a-zA-Z0-9_\-\u4e00-\u9fff]*$` | registry.py:39 |

### 6c. 上下游模块清单（读 import 得出）

**上游（本模块 import 的）**：
- `..constants`（CHARS_PER_TOKEN）
- `..tool.definition.*`（bridge：Tool/ToolContext/ToolResult/ToolPolicy）
- `..tool.types`（ToolTier/SensitivityLevel）
- `..tool.registry`（TYPE_CHECKING 下：ToolRegistry）
- `..observability.audit`（TYPE_CHECKING：AuditLog）
- `..observability.types`（运行时：AuditEventType）

**下游（import 本模块的）**：
- `builder.py`（`_resolve_skill_registry` 装配 registry + search_skills 工厂，builder.py:1100-1122）
- `tool/builtin/skill.py`（SkillToolFactory，executor 经 `ctx.metadata["skill_registry"]`）
- `engine/`（Phase 1 摘要、Phase 0.5 手动触发、每轮 clear_active_skill——本次未逐行核对 engine 侧接线，标注待补）

---

## 7. 关键代码与设计要点

### 7-1. 截断重建 = 手动全字段透传（registry.py:161-176）
frozen dataclass 不可改，截断只能重建。改动②前漏传 `script/entry_function` → Action 静默退化 Knowledge（`is_action` 变 False）——**最隐蔽的静默失败**：技能还在注册表里、LLM 能加载知识，但生成不了工具。U4 测试锁定。

### 7-2. 覆盖残留 = 懒注册幂等检查的陷阱（registry.py:188-192 + 548-565）
`_lazy_register_action_tool` 幂等依据 `get_tool(full_name) is not None`。若覆盖后不清理，旧 Tool 永远在场 → 新 Tool 永远注册不进去 → **Skill 更新不生效且无任何报错**。改动③加 `_cleanup_action_tool`（覆盖 + 注销双场景复用，registry.py:567-588）。

### 7-3. `_cleanup_action_tool` 的 Fail-Safe（registry.py:567-588）
- `tool_registry is None` → 只清缓存不抛（U8 锁定）
- `unregister_tool` 异常 → debug 日志不阻断（E4）
- 覆盖时仅当 `existing.is_action` 才调（Knowledge→Knowledge 覆盖零 Tool 副作用，U9 锁定）

### 7-4. 门禁"一次性消费"防滥用（registry.py:456-462）
`_manually_requested.discard(name)` 在 search_skills 命中即删——用户授权只对该次加载有效，不会让该技能在后续轮次永久免检。

### 7-5. SkillSource 不规定目录（models.py:26-33）
注释明说：此前硬编码 `.pandaren/skills/` / `~/.pandaren/skills/` 是错的（那两个路径从未存在过），SDK 也无权规定应用把 skill 放哪。目录归属是应用层决定（pandapal：PROJECT=resources/skills/system、USER=.pandapal/skills）。**这是"契约 vs 实现"历史纠偏的活教材**。

### 7-6. search_skills 的"未找到"返回 success=True（registry.py:445-450）
工具执行本身成功，未命中是结果内容——LLM 看到文案可自行重试，不会触发工具级错误重试。语义取舍：失败模式留给"门禁拒绝"（success=False + 明确原因）。

---

## 8. 数据流

### 8a. 注册链路（声明 → 可被加载）

```
SKILL.md（Frontmatter+body）                 .py 脚本
   │ load_skill_from_file (loader.py:21)       │
   ▼                                           │
Skill (frozen, base_path=父目录, script=相对路径)│
   │ register_skill (registry.py:109)          │
   │   ├─ 校验：name 格式 / 必填 / 截断重建     │
   │   ├─ 覆盖仲裁：优先级比较 + cleanup + 审计  │
   │   └─ 入 _skills                           │
   └── Action? → _register_action_tool (registry.py:257)
                  ├─ bridge.create_tool (bridge.py:60)
                  │    ├─ script_loader.load_skill_script (防遍历)
                  │    ├─ resolve_entry_function
                  │    └─ Tool(skill_<name>, DEFERRED, schema=函数签名)
                  └─ 缓存 _action_tools_cache[name]（不进 ToolRegistry）
version += 1
```

### 8b. 加载链路（LLM 决策 → 知识注入 / Action 可调）

```
LLM 看到 <available_skills> 摘要（build_skill_summaries，≤1% 预算）
   │ search_skills(name, ctx)（经 tool/builtin/skill.py 工厂 → ctx.metadata）
   ▼
SkillRegistry.search_skills（registry.py:414）
   1 匹配（name 精准 + 大小写容错）
   2 门禁（_manually_requested 一次性 / allow_auto_trigger）
   3 渲染（$ARGUMENTS → name）
   4 提升（Action: _lazy_register_action_tool + promote_to_discovered）
   5 激活（_activate_skill_tools 白名单，SK2）
   6 审计（SKILL_INVOKED + content_tokens）
   7 ToolResult（含"请直接调用 skill_xxx"指引）
   │
   ├─ content → 注入上下文（LLM 按文本行事）
   └─ Action Tool → ToolRegistry 已注册 + promoted → LLM 后续直调
Turn 结束：clear_active_skill() 清白名单 + _manually_requested（registry.py:538-546）
```

### 8c. 多路径对比

| 路径 | 入口 | 是否过门禁 | 激活白名单 | 审计 |
|------|------|-----------|-----------|------|
| LLM 自动 | search_skills | ✅ allow_auto_trigger | ✅ | SKILL_INVOKED / DENIED |
| 用户手动 | invoke_skill_manually → LLM search_skills | 跳过（一次性标记） | ✅ | SKILL_INVOKED |
| 应用层直接读 | get_skill | 无（受信任） | 无 | 无 |

---

## 9. 架构问题与风险

> 按 design 文档 R1~R10（测试已锁定）之外，本次代码核查新增观察。R1~R10 摘要见 §11 测试覆盖。

### P0（破坏性/数据丢失）
- 无新增。R1（search_skills 断链）与 R2（死代码复活）由 U2/U3/U11 锁定。

### P1（高严重度）
- **P1-A 覆盖残留旧 Tool（历史已修）**：改动③ `_cleanup_action_tool` 修复，U5 锁定——但该修复依赖"覆盖路径必走 cleanup"，若未来在覆盖前新增提前 return 分支需警惕（design 文档 R4）。
- **P1-B Action Skill 静默退化（历史已修）**：改动②截断重建透传 script/entry_function，U4 锁定（design 文档 R3）。

### P2（值得改进）
- **P2-A 摘要裁剪静默**：预算满时 `continue` 跳过（registry.py:399-404），低优先级技能**静默不可见**——只有 DEBUG 日志。技能被裁时 LLM 无从得知它的存在（E4 留痕级别偏低，建议 WARNING）。
- **P2-B `_lazy_register_action_tool` 日志被注释**（registry.py:562-565）：Action Tool 何时进 ToolRegistry 无可观测性，出问题只能靠推断。
- **P2-C ThreadPoolExecutor 每调用新建**（bridge.py:213-217）：Action async 技能每次执行新建线程池，高频调用有创建开销；可模块级复用池。
- **P2-D 脚本模块缓存生产环境永不失效**（script_loader.py:79-101）：改脚本需重启进程；无版本/指纹机制（R10 只测了缺失场景）。

### P3（记录在案）
- **P3-A tags 字段死重**：models.py:68 定义、loader 解析，但 `_match_skills` 不用、摘要不带——当前零消费方。
- **P3-B 通用变量渲染缺失**：仅 `$ARGUMENTS`（registry.py:655-670），扩展变量需改 `_render_content`。
- **P3-C bridge 超时参数只在构造时定死**（bridge.py:57）：`execution_timeout` 可配但 registry 构造 `SkillToolBridge()` 无参，实际恒 60s。
- **P3-D 技能目录责任模糊的历史包袱**（models.py:26-33）：注释已纠偏，但 skillify 等工具可能仍携带旧约定（注释明说"skillify 照抄了这条注释，把错误传播到 skill 生成流程"）。

---

## 10. 课程案例素材提炼

1. **静默退化是最贵的 bug**：Action Skill 截断重建丢 script → 技能"还在但能力没了"，无报错无日志可查（改动②、U4）。教学点：**字段透传的完整性 = 数据全链路**——重建对象时必须问"哪些字段决定了行为？"。
2. **幂等检查可能成为更新障碍**：`get_tool() is not None → skip` 的幂等逻辑，在覆盖场景下变成"旧定义永远挡新定义"（改动③、U5）。教学点：**幂等 ≠ 永远不更新**，要区分"已注册（同源）"和"残留（异源）"。
3. **Fail-Safe 的边界**：脚本缺失 → 注册不报错但 Skill 保留 Action 身份（is_action=True）——U12 锁定"无半残缓存"，但"Skill 声明 Action 却无 Tool 可用"仍是半残状态（P2-D 延伸）。教学点：Fail-Safe 要定义"降级到什么状态"，且该状态必须可观测。
4. **优先级仲裁的零副作用原则**：低优先级覆盖被跳过时，Tool/缓存/version 全部不动（U7，`version == v_before` 作为零副作用锚点）。教学点：**跳过 ≠ 无操作**，要用可断言的状态（version）证明真没动。
5. **契约归属纠偏**：SkillSource 注释记录了一次"SDK 越权规定目录"的历史错误及纠正（models.py:26-33）。教学点：**库不该替应用决定路径归属**，职责边界写进注释就是防回归文档。

---

## 11. 验证信息与沿革

### 测试覆盖

`pandaren/skill/tests/test_skill_registry.py`（U1~U12，**12/12 通过**，运行：`python -m pytest pandaren/skill/tests -q`）：

| 用例 | 覆盖 | 优先级 |
|------|------|--------|
| U1 死代码防复活 | inv-6 + R1 | P0 |
| U2 search_skills 定义 golden | inv-5 + R2 | P0 |
| U3 executor 经 ctx.metadata 路由 | inv-5 + R2 | P0 |
| U4 超长 description Action Skill 保字段 | inv-1 + R3 | P0 |
| U11 builder 装配 search_skills | inv-5 + R2 | P0 |
| U5 同名覆盖重建 Tool | inv-2 + R4 | P1 |
| U6 unregister 全面清理 | inv-3 + R5 | P1 |
| U7 低优先级覆盖零副作用 | inv-4 + R6 | P1 |
| U8 cleanup 无 tool_registry 安全 | inv-7 + R7 | P2 |
| U9 Knowledge 覆盖无 Tool 副作用 | inv-8 + R8 | P2 |
| U10 覆盖审计事件 | R9 | P3 |
| U12 脚本缺失无半残缓存 | R10 | P3 |

设计文档 `tests/design/skill_registry.design.md`：不变式 inv-1~9、风险 R1~R10（S×L 分级）、Mock/Fake 策略（ToolRegistry 真实现、AuditLog 用 Fake、脚本走真实 tmp_path 文件系统）。

**engine 回归**：`test_render_tool_result.py::test_render_none_data_returns_empty_placeholder` 失败——经 git stash 验证为**预先存在**（HEAD 状态同样失败，与 skill 模块无关，工作区另有 hooks/observability 未提交改动所致）。

### 与上下篇的印证关系

- **04-tool**：skill 的 Tool 契约全部复用 tool 层（Tool/ToolContext/ToolResult/ToolRegistry/ToolTier）；`search_skills` 是 ALWAYS 级工具，Action Tool 是 DEFERRED 级 + KD3 提升。
- **07-sub_agent**：skill 摘要注入 <available_skills> 与 sub_agent 摘要注入 <available_agents> 是同构机制（1% 预算 + HEALTHY/存在性过滤），本模块不涉及委派信任/循环检测（那是 sub_agent 的职责）。
- **06-engine**：engine 消费 build_skill_summaries（Phase 1）、invoke_skill_manually（Phase 0.5）、clear_active_skill（每轮结束）；engine 侧接线本次未逐行核对，锚点待补。

### 变更历史

- git log（`pandaren/skill/`）：仅 2 条——`c7d5e9f` Initial commit；`fefaa33` 搬迁 .pandapal 用户技能/代理目录结构并规范化 SkillManager 错误事件。
- **工作区未提交改动**（git diff pandaren/skill/registry.py，+36/-94，对应遗留 design 文档的改动①②③，**均未提交**）：
  1. ① 删除死代码 `register_builtin_tools` / `_builtin_tools_registered`（search_skills 改由 `tool/builtin/skill.py` SkillToolFactory 生成，builder.py:1100-1122 装配）
  2. ② 修复 description 截断重建丢失 `script/entry_function`（Action Skill 静默退化）
  3. ③ 修复同名覆盖残留旧 Action Tool，新增 `_cleanup_action_tool`（覆盖 + 注销双场景复用）
- 测试与设计文档位于 `tests/`（untracked，未纳入版本控制）。
- 本模块对应三件套：Tool(04) / **Skill(08)** / Agent(07)。
