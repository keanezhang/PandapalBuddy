# 04 — pandaren/tool 测试用例设计（回归防护）

> 定位：为重构后 API 建立回归防护。旧测试（`pandaren/tool/tests/`）已删除，全仓库零覆盖；本文档按源码现状（已核实 `git` 工作区 `pandaren/tool/` 全部相关文件）设计。
> 生成方式：白盒分析（通读 11 个源码文件）+ 少量 smoke 运行验证签名与 golden value。
> 设计原则：**R.I.S.K.-Driven Testing**——每个用例绑定不变式/风险项；最少关键用例 × 最强行为证明。

---

## 1. 前置门控：信息确认表

| 必须确认项 | 状态 | 依据 |
|-----------|------|------|
| 被测模块/函数签名 | ✅ 已核实 | safe_name.py / store.py / discovery.py / schema_builder.py / gate_chain.py / executor.py / facade.py / tool.py / tool_policy.py / tool_result.py / context.py 全部通读 |
| 不变式清单 | ✅ 见 §4 | 每模块独立梳理 |
| 风险点清单 | ✅ 见 §4 | 对齐 docs/04-tool.md §9 已修复的 12 条问题 |
| 关键依赖列表 | ✅ 见 §3 | validator / budget / guard_chain / types / identity.models |

| 可推断项 | 推断值 | 备注 |
|---------|-------|------|
| 测试框架 | pytest + pytest-asyncio | 环境已验证 `pytest_asyncio` 可导入（Python 3.12.8）；异步用例用 `asyncio` 模式 |
| Mock 政策 | 算法层零 mock；模块间用真实内存对象（ToolStore/DiscoveryManager/GateChain/ToolBudget 均为纯内存，无外部 I/O） | 全设计无外部依赖 → 无需 mock 任何第三方 |
| 集成用例 | **本设计无集成用例**（见 §3 豁免声明） | 全部目标模块无真实进程边界 I/O |

---

## 2. 测试层级声明（防摇摆）

| 模块 | 层级 | 判据 |
|------|------|------|
| safe_name.py | **unit** | 纯函数，零协作对象 |
| registry/store.py | **component(fake)** | 有内部状态 + 协作 validator/Tool（纯值/纯函数），全部内存 |
| registry/discovery.py | **unit** | 单类纯内存状态机，无协作对象 |
| exposure/schema_builder.py | **component(fake)** | 编排 store/discovery/gate/budget（全内存对象） |
| exposure/gate_chain.py | **unit** | 单类纯逻辑，依赖为 dataclass 值 |
| execution/executor.py | **unit** | executor 用测试内闭包；asyncio/线程池为标准库，非外部依赖 |
| definition/tool_policy.py | **unit** | 纯数据类 |
| facade.py | **component(fake)** | 组合全部内存组件，无真实 LLM / DB / 网络 |

**集成/e2e 豁免声明**：上述模块的依赖图全部落在 `pandaren/tool/` 包内且无真实进程边界 I/O（无真 DB、真 HTTP、真文件系统、真 LLM）。唯一"准外部"点是 executor 的 `asyncio.run_in_executor`（标准库线程池，实现细节，非被测边界）。因此**本设计不包含集成用例**；如需补 `search_tools → LLM 回传 → store.get` 的真 LLM 回环（e2e），需真实 LLM key，另行设计并 skip。

---

## 3. Mock / Fake 策略

| 依赖 | 决策 | 理由 |
|------|------|------|
| Tool / ToolPolicy / ToolContext | 真实构造 | frozen dataclass 纯值，构造成本零 |
| ToolStore / DiscoveryManager | 真实实例 | 被测编排层的直接协作者，内存实现即 Fake，无外部 I/O |
| GateChain / ToolBudget | 真实实例（`GateChain.default()` / `ToolBudget()`） | 纯内存逻辑 |
| validator（register 内部调用） | 真实执行 | 纯函数，属被测 store 的注册链路 |
| executor 函数 | 测试内闭包（记录 kwargs / 抛异常 / 返回自定义对象） | 无外部依赖，闭包即 Fake |
| error_formatter / validate_input / format_result_for_llm | 测试内闭包 | ToolLifecycle 钩子，注入点即测试点 |
| 外部第三方 / 网络 / 数据库 | **不存在** | 全部用例离线可跑，零 mock、零 key |

**豁免规则应用**：纯函数/算法层（safe_name、tool_policy）无副作用验证点 → 写"无副作用"；executor/facade 有状态变更（discovery 写入、cache 重建）→ 必须验证副作用；无外部 I/O → 故障注入类豁免（"该类不适用 + 理由"见各模块）。

---

## 4. 不变式与风险清单（按模块）

### 4.1 safe_name.py

**不变式**
| ID | 内容 |
|----|------|
| inv-s1 | 确定性：`to_safe_name_parts(ns, name)` 同输入恒同输出 |
| inv-s2 | 输出恒 ASCII：任意输入 → `result.isascii() == True` |
| inv-s3 | name 纯 ASCII 且无 ns → 原样返回（identity） |
| inv-s4 | name 非 ASCII → `f"{ns_part}_{md5(name)[:8]}"`（ns_part 为空则裸 hash）；md5 前缀可独立计算 |
| inv-s5 | ns 非 ASCII → ns_part 同样 hash 化（`md5(ns)[:8]`），输出仍 ASCII |
| inv-s6 | name 含下划线不误拆：结果只含 `ns_` 前缀一次，绝不把 name 内下划线当 ns 边界 |

**风险**
| ID | 内容 | 优先级 |
|----|------|--------|
| Risk-s1 | 旧 `rsplit("_",1)` 误拆含下划线 name → schema 名与 store 索引不一致（**已修复**，回归重点） | P0 |
| Risk-s2 | 旧版 ns 含非 ASCII 时输出仍非 ASCII → LLM API 400（**已修复**） | P0 |
| Risk-s3 | 空 name / 空 namespace 边界 | P1 |
| Risk-s4 | 超长 name 不崩溃、hash 前缀长度恒 8 | P2 |

### 4.2 registry/store.py

**不变式**
| ID | 内容 |
|----|------|
| inv-st1 | full_name 唯一：同 namespace+name 重复注册抛 `ToolRegistrationError`（`skip_if_exists=True` 时静默跳过且 version 不变） |
| inv-st2 | safe_name 索引 key == `to_safe_name_parts(tool.namespace, tool.name)` 精确计算值（非字符串猜测） |
| inv-st3 | `get()` 双向：原始 full_name 与 LLM-safe 名均命中；含下划线 name 反查必成功 |
| inv-st4 | safe_name == full_name（纯 ASCII 无 ns）时不建索引，但 `get`/`__contains__` 仍命中 |
| inv-st5 | `unregister()` 后：get→None、`__contains__` False、version+1 |
| inv-st6 | 注销后 ns 下无剩余工具 → `_namespace_registry` 移除该 ns（用 **tool.namespace 字段**，非 `split` 猜测） |
| inv-st7 | `unregister()` 不存在工具 → 返回 False，无副作用（version 不变、索引不变） |
| inv-st8 | version 单调递增：每次成功 register/unregister +1 |

**风险**
| ID | 内容 | 优先级 |
|----|------|--------|
| Risk-st1 | 含下划线 name 的索引 key 与 schema 名不一致（旧 rsplit bug 传导） | P0 |
| Risk-st2 | ns 清理恒不触发 → `_namespace_registry` 残留脏数据（旧 `split(".",1)` bug，**已修复**） | P0 |
| Risk-st3 | 重复注册同名工具未拦截 → 静默覆盖 | P0 |
| Risk-st4 | safe_name 反查失败（LLM 回传 schema 名 get 不到） | P1 |
| Risk-st5 | unregister 用 safe_name 注销失败 | P1 |

### 4.3 registry/discovery.py

**不变式**
| ID | 内容 |
|----|------|
| inv-d1 | `discover()` 是唯一写入点（discover/undiscover/restore/clear 为全部写入入口） |
| inv-d2 | LRU 淘汰按 **step_n 最小者**逐出 |
| inv-d3 | 超过 `max_discovered` 才淘汰，恰好等于不淘汰；一次逐出 excess 个 |
| inv-d4 | `update_step()` 刷新已发现工具的 step_n → 影响后续 LRU 排序（最近使用优先保留） |
| inv-d5 | snapshot/restore 往返无损（`restore(snapshot()) == 原状态`） |
| inv-d6 | `update_step()` 对未发现 name 无副作用（不新增条目） |

**风险**
| ID | 内容 | 优先级 |
|----|------|--------|
| Risk-d1 | LRU 错逐（逐出最近使用而非最久未用） | P0 |
| Risk-d2 | update_step 无调用方 → 长循环高频工具被提前逐出（旧 P3，**已修复**：facade 成功路径调用） | P1 |
| Risk-d3 | restore 后超上限不淘汰 | P1 |
| Risk-d4 | undiscover 返回值语义错误 | P3 |

### 4.4 exposure/schema_builder.py

**不变式**
| ID | 内容 |
|----|------|
| inv-sb1 | 非 ASCII 工具 `ToolSchema.name == to_safe_name_parts(ns, name)` == store 索引 key → `store.get(schema.name)` 必命中真实工具（LLM 回传闭环） |
| inv-sb2 | `result.schemas` 中所有 `name` 恒 ASCII（否则 LLM API 400） |
| inv-sb3 | 三段式：ALWAYS（非 search_tools）→ schemas；DEFERRED 未发现 → 仅 catalog 摘要 + search_enum；DEFERRED 已发现 → 完整 schema |
| inv-sb4 | `search_enum` 只含 DEFERRED **未发现**工具的 safe_name（不含 ALWAYS、不含已发现） |
| inv-sb5 | 各段内按 name 字母序升序 |
| inv-sb6 | DEFERRED 已发现**仍保留**在 `deferred_catalog`（保持 system prompt 缓存命中） |
| inv-sb7 | search_tools 未通过门链 → 不追加其 schema |

**风险**
| ID | 内容 | 优先级 |
|----|------|--------|
| Risk-sb1 | schema 名与 store 索引不一致 → LLM 回传名 get 不到工具（**修复回归重点**） | P0 |
| Risk-sb2 | search_enum 混入已发现/ALWAYS 工具 | P0 |
| Risk-sb3 | 非 ASCII schema 名漏出 | P0 |
| Risk-sb4 | 门链过滤误伤 search_tools / ALWAYS | P1 |

### 4.5 execution/executor.py

**不变式**
| ID | 内容 |
|----|------|
| inv-ex1 | `execute()` 永不外抛：任何异常 → `ToolResult(success=False)` |
| inv-ex2 | `filter_extra_args()`：schema properties 外 key 移除，返回被移除列表（公开方法，无下划线） |
| inv-ex3 | `coerce_args()`：integer/number/boolean 按 schema type 强转（`"20"`→20、`"true"`→True），返回被转换列表 |
| inv-ex4 | 成功 → `ToolResult(success=True, data=格式化后)`；raw 为 ToolResult 时透传 data/error/halt 并覆盖 tool_name |
| inv-ex5 | `error_formatter` 自身异常 → **留痕**（logger.warning）+ 回落默认格式（不静默吞） |
| inv-ex6 | 同步 executor 丢线程池执行（`run_in_executor`），事件循环不被阻塞 |
| inv-ex7 | validate_input 返回 `ValidationResult(valid=False)` → `success=False` + 其 message；返回 None → 继续执行 |
| inv-ex8 | `max_output_bytes` 字节级截断 → `truncated=True` |
| inv-ex9 | 清洗/强转发生时，成功结果 data 前缀注入 `[参数修正]`/`[类型修正]` 提示 |

**风险**
| ID | 内容 | 优先级 |
|----|------|--------|
| Risk-ex1 | 工具异常外抛 → 中断 Agent 循环（O3 核心） | P0 |
| Risk-ex2 | 转换失败（`"abc"`→int）静默吞或误转 | P1 |
| Risk-ex3 | error_formatter 异常静默吞（旧 P3，**已修复**加留痕） | P1 |
| Risk-ex4 | bool 是 int 子类陷阱：`True`→1、`20.0`→20 语义 | P2 |
| Risk-ex5 | 方法名回退下划线（旧 API 名 `_filter_extra_args`）→ 回归断言公开名 | P1 |

### 4.6 definition/tool_policy.py

**不变式**
| ID | 内容 |
|----|------|
| inv-tp1 | `sensitivity` 必填（无默认值），缺失 → TypeError |
| inv-tp2 | `default_result_limit` 字段**已删除**：传入 → TypeError（回归防护） |
| inv-tp3 | `supports_offset_pagination` 保留：默认 False，可覆盖 True |

**风险**：Risk-tp1 字段误回归 [P2]；Risk-tp2 构造签名漂移 [P2]

### 4.7 facade.py

**不变式**
| ID | 内容 |
|----|------|
| inv-f1 | `execute_tool()` 永不外抛：未注册工具 → `ToolResult(success=False, error 含 "未注册")` |
| inv-f2 | DEFERRED 首次执行成功 → `discovery.discover(full_name, ctx.step_n)`；已发现 → `update_step` 刷新 |
| inv-f3 | ALWAYS 执行成功 → 不写 discovery |
| inv-f4 | 未发现 DEFERRED 被 DiscoveryGuard 拦截 → `success=False` + 提示先调 search_tools，executor **未执行** |
| inv-f5 | `is_circuit_tripped: Callable[[str], bool]`：熔断为 True → enabled_cache 置 False |
| inv-f6 | `unregister_tool` 同步清理 store + enabled_cache + discovery 三处 |
| inv-f7 | `set_hooks` 二次调用抛 RuntimeError |
| inv-f8 | `promote_to_discovered`：DEFERRED 以 **full_name** 写入 discovery；ALWAYS/未注册静默返回 |

**风险**
| ID | 内容 | 优先级 |
|----|------|--------|
| Risk-f1 | execute_tool 未注册工具外抛 | P0 |
| Risk-f2 | DEFERRED 发现状态不维护（首次/刷新）→ LRU 排序失真 | P1 |
| Risk-f3 | is_circuit_tripped 类型不收敛（旧 P2，**已修复**） | P2 |
| Risk-f4 | unregister 后残留 cache/discovery 状态 | P2 |
| Risk-f5 | facade 前置清洗与 executor 兜底清洗（双保险）链路断裂 | P2 |

### 4.8 exposure/gate_chain.py

**不变式**
| ID | 内容 |
|----|------|
| inv-g1 | `GateChain.default()` 门链 = **4 道门**，顺序固定：allow_list → enabled → agent_whitelist → skill_whitelist |
| inv-g2 | 未配置 = 全放行（对应 context 字段 None → 透明） |
| inv-g3 | 组合为 AND 交集：任一道门拒 → 该工具被过滤（短路 break） |
| inv-g4 | 每道门独立语义：AllowList(agent_allowed_tools) / Enabled(enabled_cache) / AgentWhitelist(tool.agent_whitelist∩agent_id) / SkillWhitelist(skill_allowed_tools) |

**风险**：Risk-g1 门数/顺序漂移（旧 docstring 写 5 道，**已修复**为 4 道）[P2]；Risk-g2 配置了却不过滤 [P1]

---

## 5. 汇总：用例 × 风险覆盖矩阵

### 5.1 safe_name（unit，6 用例）

| 用例 | inv-s1 | inv-s2 | inv-s3 | inv-s4 | inv-s5 | inv-s6 | Risk-s1 | Risk-s2 | Risk-s3 | Risk-s4 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| SN-1 ASCII identity | ✅ | | ✅ | | | | | | ✅ | |
| SN-2 非 ASCII name golden | | ✅ | | ✅ | | | | | | |
| SN-3 ns 非 ASCII hash 化 | | ✅ | | | ✅ | | | ✅ | | |
| SN-4 下划线 name 不误拆 | | ✅ | | ✅ | | ✅ | ✅ | | | |
| SN-5 确定性+ASCII property | ✅ | ✅ | | | | | | | | |
| SN-6 超长/空边界 | ✅ | ✅ | | ✅ | | | | | ✅ | ✅ |

### 5.2 store（component(fake)，10 用例）

| 用例 | inv-st1 | inv-st2 | inv-st3 | inv-st4 | inv-st5 | inv-st6 | inv-st7 | inv-st8 | Risk-st1 | Risk-st2 | Risk-st3 | Risk-st4 | Risk-st5 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| ST-1 注册基本+version | | ✅ | ✅ | | | | | ✅ | | | | | |
| ST-2 下划线 name 索引精确 | | ✅ | ✅ | | | | | | ✅ | | | ✅ | |
| ST-3 get 双向命中 | | | ✅ | | | | | | | | | ✅ | |
| ST-4 纯 ASCII 不建索引 | | ✅ | ✅ | ✅ | | | | | | | | | |
| ST-5 重复注册+skip | ✅ | | | | | | | ✅ | | | ✅ | | |
| ST-6 unregister(full) | | | | | ✅ | | | ✅ | | | | | |
| ST-7 unregister(safe) | | | | | ✅ | | | ✅ | | | | | ✅ |
| ST-8 ns 清理 | | | | | | ✅ | | | | ✅ | | | |
| ST-9 unregister 不存在 | | | | | | | ✅ | ✅ | | | | | |
| ST-10 查询族+校验拒绝 | ✅ | | | | | | | | | | ✅ | | |

### 5.3 discovery（unit，6 用例）

| 用例 | inv-d1 | inv-d2 | inv-d3 | inv-d4 | inv-d5 | inv-d6 | Risk-d1 | Risk-d2 | Risk-d3 | Risk-d4 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| DI-1 discover+查询 | ✅ | | | | | | | | | |
| DI-2 LRU 淘汰 golden | | ✅ | ✅ | | | | ✅ | | | |
| DI-3 update_step 刷新排序 | | ✅ | | ✅ | | | | ✅ | | |
| DI-4 恰好 max 不淘汰 | | | ✅ | | | | | | | |
| DI-5 snapshot/restore | | | ✅ | | ✅ | | | | ✅ | |
| DI-6 update_step 无副作用+undiscover | | | | | | ✅ | | | | ✅ |

### 5.4 schema_builder（component(fake)，8 用例）

| 用例 | inv-sb1 | inv-sb2 | inv-sb3 | inv-sb4 | inv-sb5 | inv-sb6 | inv-sb7 | Risk-sb1 | Risk-sb2 | Risk-sb3 | Risk-sb4 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| SB-1 三段式基础 | | | ✅ | | | | | | | | |
| SB-2 schema 名=索引 key | ✅ | ✅ | | | | | | ✅ | | ✅ | |
| SB-3 search_enum 只含未发现 | | | ✅ | ✅ | | | | | ✅ | ✅ | |
| SB-4 门链过滤+filtered_count | | | ✅ | | | | | | | | ✅ |
| SB-5 段内排序 | | | | | ✅ | | | | | | |
| SB-6 search_tools 被过滤 | | | | | | | ✅ | | | | ✅ |
| SB-7 已发现保留 catalog | | | ✅ | | | ✅ | | | | | |
| SB-8 token 预算裁剪 | | | ✅ | | | | | | | | |

### 5.5 executor（unit，13 用例）

| 用例 | inv-ex1 | inv-ex2 | inv-ex3 | inv-ex4 | inv-ex5 | inv-ex6 | inv-ex7 | inv-ex8 | inv-ex9 | Risk-ex1 | Risk-ex2 | Risk-ex3 | Risk-ex4 | Risk-ex5 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| EX-1 公开方法存在 | | ✅ | ✅ | | | | | | | | | | | ✅ |
| EX-2 filter_extra_args | | ✅ | | | | | | | | | | | | |
| EX-3 coerce golden | | | ✅ | | | | | | | | | | | |
| EX-4 coerce 无效保留 | | | ✅ | | | | | | | | ✅ | | | |
| EX-5 bool/int 子类陷阱 | | | ✅ | | | | | | | | | | ✅ | |
| EX-6 execute 成功(async) | | | | ✅ | | | | | | | | | | |
| EX-7 execute 成功(sync 线程池) | | | | ✅ | | ✅ | | | | | | | | ✅ |
| EX-8 异常不外抛 | ✅ | | | | | | | | | ✅ | | | | |
| EX-9 error_formatter 生效 | ✅ | | | | | | | | | ✅ | | | | |
| EX-10 formatter 异常回落+留痕 | ✅ | | | | ✅ | | | | | ✅ | | ✅ | | |
| EX-11 validate_input 拦截 | | | | | | | ✅ | | | | | | | |
| EX-12 截断 | | | | | | | | ✅ | | | | | | |
| EX-13 修正提示+双保险+raw包装 | ✅ | ✅ | ✅ | ✅ | | | | | ✅ | | | | | |

### 5.6 tool_policy（unit，3 用例）

| 用例 | inv-tp1 | inv-tp2 | inv-tp3 | Risk-tp1 | Risk-tp2 |
|------|:--:|:--:|:--:|:--:|:--:|
| TP-1 sensitivity 必填 | ✅ | | | | ✅ |
| TP-2 default_result_limit 已删 | | ✅ | | ✅ | |
| TP-3 supports_offset_pagination | | | ✅ | | |

### 5.7 facade（component(fake)，10 用例）

| 用例 | inv-f1 | inv-f2 | inv-f3 | inv-f4 | inv-f5 | inv-f6 | inv-f7 | inv-f8 | Risk-f1 | Risk-f2 | Risk-f3 | Risk-f4 | Risk-f5 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| FA-1 未注册不外抛 | ✅ | | | | | | | | ✅ | | | | |
| FA-2 DEFERRED 首次 discover | | ✅ | | | | | | | | ✅ | | | |
| FA-3 DEFERRED 刷新+ALWAYS 不写 | | ✅ | ✅ | | | | | | | ✅ | | | |
| FA-4 DiscoveryGuard 拦截 | | | | ✅ | | | | | | | | | |
| FA-5 前置清洗双保险 | | | | | | | | | | | | | ✅ |
| FA-6 is_enabled → cache | | | | | ✅ | | | | | | | | |
| FA-7 circuit_tripped 熔断 | | | | | ✅ | | | | | | ✅ | | |
| FA-8 unregister 三处清理 | | | | | | ✅ | | | | | | ✅ | |
| FA-9 set_hooks+promote | | | | | | | ✅ | ✅ | | | | | |
| FA-10 build→get 端到端一致 | | | | | | | | | | | | | |

### 5.8 gate_chain（unit，6 用例）

| 用例 | inv-g1 | inv-g2 | inv-g3 | inv-g4 | Risk-g1 | Risk-g2 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|
| GC-1 default 4 道门 | ✅ | | | | ✅ | |
| GC-2 未配置全放行 | | ✅ | | | | ✅ |
| GC-3 AllowListGate | | | | ✅ | | ✅ |
| GC-4 EnabledGate+AgentWhitelist | | | | ✅ | | ✅ |
| GC-5 SkillWhitelistGate | | | | ✅ | | ✅ |
| GC-6 AND 短路 | | | ✅ | | | |

---

## 6. 全局测试约定（衔接 test-coder 的公共前置）

1. **Tool 构造约定**（`Tool` 为 frozen+kw_only，必填 6 项，`tool.py:33-55`）：
   - `Tool(name=..., description=..., executor=..., policy=ToolPolicy(sensitivity=SensitivityLevel.LOW), input_schema={"type": "object", "properties": {...}}, when_to_use=...)`
   - ⚠️ `input_schema` **不能为空 dict**（validator `validate_required_fields` 拒绝，`validator.py:43-46`）；无参数工具用 `{"type": "object", "properties": {}}`
   - 构造后 `input_schema` 被深拷贝为 `MappingProxyType`（`tool.py:91-96`）——executor 清洗时需经 `_to_serializable` 解包，这是被测行为，测试侧构造不受影响
   - 注册经 `validate_conflicts` 可能自动修正 policy（如不可逆低敏升级 HIGH），测试用默认 `is_reversible=True` 规避干扰
2. **ToolContext 构造约定**（`context.py:12-35`，frozen）：必填 `run_id / step_n / agent_id / session_id`；`session_id` 必传（如 `ToolContext(run_id="r1", step_n=1, agent_id="a1", session_id="s1")`）
3. **异步执行**：`executor.execute` / `facade.execute_tool` 为 async，用 pytest-asyncio 的 asyncio 模式（环境已装 `pytest_asyncio`）；同步工具走 `run_in_executor`，在测试事件循环内 await 即可
4. **日志断言**：error_formatter 留痕用 pytest `caplog`（logger name：`pandaren.tool.execution.executor`）
5. **白盒断言声明**：涉及私有字段（`store._namespace_registry` / `store._safe_name_index` / `discovery._discovered`）的用例为**有意白盒**——这些是 §9 修复点的直接载体，无公开等价行为可替代，作为回归防护保留
6. **确定性控制**：本设计无时间/随机/浮点/顺序不确定源；唯一顺序敏感点是 discovery LRU（step_n 显式给定）与 schemas 排序（源码已 sort，断言排序本身）。无需 fake timer / seed / TZ 固定

---

## 7. 用例展开

### 7.1 safe_name.py（unit · 零 mock）

#### 用例 SN-1：纯 ASCII name + 无 namespace → 原样返回（identity）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-s1 确定性 + inv-s3 identity + Risk-s3 空 ns 边界 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: `namespace` falsy → ns_part=""；`name.isascii()` True → 走 D 分支 |
| Oracle | golden value（"search_tools" 无歧义） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：namespace ∈ {None, ""}；name 纯 ASCII 无下划线 → 代表值 = `(None, "search_tools")`、`("", "search_tools")`

**Given**（前置条件）：
- 无前置，纯函数直接调用

**When**（操作/动作）：
- `to_safe_name_parts(None, "search_tools")`；`to_safe_name_parts("", "search_tools")`

**Then**（预期结果）：
- 返回值 = `"search_tools"`（两次调用均相等）
- 副作用：无副作用，仅验证返回值

---

#### 用例 SN-2：非 ASCII name + ASCII namespace → 确定性 hash 后缀（golden）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-s2 恒 ASCII + inv-s4 hash 规则 + Risk-s2 非 ASCII 输出 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: `namespace` truthy 且 `isascii()` True → B；`name.isascii()` False → E |
| Oracle | golden value（`md5("天气预报").hexdigest()[:8]` 已独立计算 = `a68661fb`，非跑实现抄来） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：name 非 ASCII ∈ {中文、emoji、混合} → 代表值 = `"天气预报"`；ns 纯 ASCII → 代表值 = `"skill"`

**Given**（前置条件）：
- 无前置

**When**（操作/动作）：
- `to_safe_name_parts("skill", "天气预报")`

**Then**（预期结果）：
- 返回值 = `"skill_a68661fb"`
- `返回值.isascii() == True`
- 再调一次结果相同（确定性，inv-s1）
- 副作用：无副作用

---

#### 用例 SN-3：namespace 含非 ASCII → ns_part 也 hash 化，输出恒 ASCII

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-s2 恒 ASCII + inv-s5 ns hash 化 + Risk-s2 旧 bug 回归 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: `namespace` truthy 且 `isascii()` False → C |
| Oracle | golden value（`md5("天气")[:8]` = `265f273c`，独立计算） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：namespace 非 ASCII ∈ {中文、emoji}；name 纯 ASCII → 代表值 = `("天气", "my_tool")`；另测 ns 与 name 均非 ASCII → 代表值 = `("天气", "天气预报")`

**Given**（前置条件）：
- 无前置

**When**（操作/动作）：
- `to_safe_name_parts("天气", "my_tool")`；`to_safe_name_parts("天气", "天气预报")`

**Then**（预期结果）：
- 返回值 1 = `"265f273c_my_tool"`（ns hash 化后拼接 ASCII name）
- 返回值 2 = `"265f273c_a68661fb"`（ns 与 name 均 hash 化）
- 两个返回值 `isascii() == True`（inv-s2 是硬约束，本用例是旧 bug 的直接回归）
- 副作用：无副作用

---

#### 用例 SN-4：name 含下划线不误拆（旧 rsplit bug 回归）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-s6 不误拆 + Risk-s1 [P0]（§9 修复核心，直接对应 store 索引一致性） |
| 测试层级 | unit |
| 覆盖准则 | branch: name.isascii() False → E；验证输出不含 name 内下划线残留 |
| Oracle | golden value（`md5("my_tool_天气")[:8]` = `085755d5`，独立计算）+ 蜕变关系（输出中 `_` 出现次数） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：name 内含下划线 ∈ {`my_tool_天气`、`a_b_c`}；ns ∈ {`ns`, None} → 代表值 = `("ns", "my_tool_天气")`、`(None, "my_tool_天气")`、`("ns", "my_tool_abc")`

**Given**（前置条件）：
- 无前置

**When**（操作/动作）：
- `to_safe_name_parts("ns", "my_tool_天气")`

**Then**（预期结果）：
- 返回值 = `"ns_085755d5"`（❌ 若为 `"ns_my_tool_085755d5"` 或 `"my_tool_085755d5"` 即 rsplit 误拆 bug 复发）
- 蜕变断言：`返回值.count("_") == 1`（ns 前缀分隔符仅一次，name 内下划线绝不外泄）
- `(None, "my_tool_天气")` → `"085755d5"`；`("ns", "my_tool_abc")` → `"ns_my_tool_abc"`（ASCII 时原样拼接）
- 副作用：无副作用

---

#### 用例 SN-5：确定性 + 恒 ASCII 属性测试（property）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-s1 确定性 + inv-s2 恒 ASCII [property] |
| 测试层级 | unit |
| 覆盖准则 | N/A（属性测试，对一整类输入） |
| Oracle | property（fast-check/hypothesis 风格：随机生成 ns/name 断言两条不变式） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：输入空间 = {namespace: str|None, name: str} 全空间随机（含空串、纯 ASCII、中文、emoji、混合、超长、含下划线）

**Given**（前置条件）：
- 无前置

**When**（操作/动作）：
- 随机生成 1000 组 `(ns, name)`，每组调用两次 `to_safe_name_parts`

**Then**（预期结果）：
- `f(ns, name) == f(ns, name)`（确定性，inv-s1）
- `f(ns, name).isascii() == True`（恒 ASCII，inv-s2）
- 副作用：无副作用

---

#### 用例 SN-6：超长 name / 空 name 边界不崩溃且格式正确

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-s4 hash 规则 + Risk-s3 空边界 + Risk-s4 超长 [P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: name.isascii() False → E（超长触发）；`namespace` falsy → A |
| Oracle | 蜕变关系（输出无法人工手算，禁硬编码）+ golden（空串 case） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：name 长度 ∈ {0, 1, 10000}；ns ∈ {None, `"skill"`} → 代表值 = `(None, "")`、`(None, "a")`、`("skill", "天"*10000)`

**Given**（前置条件）：
- 构造 10000 个 `"天"` 的字符串

**When**（操作/动作）：
- `to_safe_name_parts(None, "")`；`to_safe_name_parts(None, "a")`；`to_safe_name_parts("skill", "天"*10000)`

**Then**（预期结果）：
- `(None, "")` → `""`（空 name 原样返回，不崩溃；空串 `"".isascii() == True`）
- `(None, "a")` → `"a"`
- 超长：不抛异常；返回格式 `"skill_" + 8 位 hash`，hash 部分长度恒 8、全 `[0-9a-f]`
- 副作用：无副作用

---

### 7.2 registry/store.py（component(fake) · 真实内存对象）

> 公共前置：`store = ToolStore()`；工具构造用 §6 约定。工具 executor 为 `lambda ctx, **kw: "ok"`。

#### 用例 ST-1：register 基本流程 — full_name 存储 + version 递增

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-st2 + inv-st3 + inv-st8 |
| 测试层级 | component(fake) |
| 覆盖准则 | register 全链路（校验→唯一性→写入→索引） |
| Oracle | 状态断言（get/version/len） |
| Mock | 否 — 真实内存组件 |

**等价类划分**：ns ∈ {None, "skill"}；name 纯 ASCII → 代表值 = `("skill", "weather")`

**Given**（前置条件）：
- 构造 `tool = Tool(namespace="skill", name="weather", ...)`

**When**（操作/动作）：
- `store.register(tool)`

**Then**（预期结果）：
- `store.get("skill_weather") is tool`（full_name 存储）
- `len(store) == 1`；`store.version == 1`
- `"skill_weather" in store` 为 True
- 副作用：`_namespace_registry == {"skill"}`（白盒）；`_safe_name_index == {}`（ASCII name safe_name==full_name，不建索引）

---

#### 用例 ST-2：含下划线 name 的索引 key 精确计算（旧 rsplit bug 回归）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-st2 + inv-st3 + Risk-st1 + Risk-st4 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | register 的 safe_name 索引分支（`safe_name != full_name` 时写入） |
| Oracle | golden value（`md5("my_tool_天气")[:8]` = `085755d5`）+ 状态断言 |
| Mock | 否 |

**等价类划分**：name 含下划线且非 ASCII → 代表值 = `("ns", "my_tool_天气")`；对照：name 含下划线纯 ASCII → `("ns", "my_tool_abc")`

**Given**（前置条件）：
- 构造 `tool = Tool(namespace="ns", name="my_tool_天气", ...)`

**When**（操作/动作）：
- `store.register(tool)`

**Then**（预期结果）：
- `_safe_name_index == {"ns_085755d5": "ns_my_tool_天气"}`（白盒：索引 key 精确等于 `to_safe_name_parts("ns","my_tool_天气")`，❌ 若为 `"ns_my_tool_..."` 前缀即误拆复发）
- `store.get("ns_085755d5") is tool`（LLM 回传 schema 名反查成功，inv-st3）
- 对照：注册 `("ns", "my_tool_abc")` 后 safe_name == full_name == `"ns_my_tool_abc"`，索引为空，但 `get("ns_my_tool_abc")` 命中
- 副作用：`len(store) == 2`、version == 2

---

#### 用例 ST-3：get 双向查找（原始 full_name 与 LLM-safe 名）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-st3 + Risk-st4 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | get 的两条路径（直查 _tools / 反查索引） |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：查询名 ∈ {原始 full_name、safe_name、未注册名} → 代表值 = `"skill_天气预报"`、`"skill_a68661fb"`、`"skill_不存在"`

**Given**（前置条件）：
- 注册 `Tool(namespace="skill", name="天气预报", ...)`（safe_name = `skill_a68661fb`）

**When**（操作/动作）：
- `store.get("skill_天气预报")`；`store.get("skill_a68661fb")`；`store.get("skill_不存在")`

**Then**（预期结果）：
- 前两者均返回同一 Tool 对象
- 未注册名返回 `None`
- 副作用：无（get 只读，version/索引不变）

---

#### 用例 ST-4：纯 ASCII 无 ns 工具 — safe_name==full_name，不建索引但双向命中

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-st4（索引建/不建的边界） |
| 测试层级 | component(fake) |
| 覆盖准则 | register 索引分支的 `safe_name == full_name` 路径（store.py:69） |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：name 纯 ASCII 且 ns=None → 代表值 = `"weather_tool"`；name 纯 ASCII 且 ns 非空 → `("ns", "weather_tool")`

**Given**（前置条件）：
- 注册 `Tool(name="weather_tool", ...)`（无 namespace）

**When**（操作/动作）：
- `store.register(tool)` 后查询

**Then**（预期结果）：
- `_safe_name_index == {}`（safe_name == full_name == `"weather_tool"`，无冗余索引，白盒）
- `store.get("weather_tool") is tool`；`"weather_tool" in store` 为 True
- `len(store) == 1`
- 副作用：无

---

#### 用例 ST-5：重复注册拦截与 skip_if_exists 静默跳过

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-st1 + inv-st8 + Risk-st3 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | register 唯一性分支（store.py:50-57 两条子路径） |
| Oracle | 异常断言 + 状态断言 |
| Mock | 否 |

**等价类划分**：重复方式 ∈ {同 ns 同名、不同 ns 同名}；参数 skip_if_exists ∈ {False, True}

**Given**（前置条件）：
- 注册 `Tool(namespace="ns", name="dup", ...)`（tool_a）

**When**（操作/动作）：
- 注册同名 `tool_b`（namespace="ns", name="dup"）；再以 `skip_if_exists=True` 注册；再注册 `("ns2", "dup")`

**Then**（预期结果）：
- 第二次注册 → 抛 `ToolRegistrationError`，`len(store)` 仍为 1、version 仍为 1（无副作用）
- `skip_if_exists=True` → 静默返回不抛，`len(store)` 仍为 1、version 不变
- 不同 ns 同名 → 注册成功（`len(store) == 2`），`get("ns2_dup")` 命中

---

#### 用例 ST-6：unregister 原始 full_name — 注销后不可查、version 递增

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-st5 + inv-st8 |
| 测试层级 | component(fake) |
| 覆盖准则 | unregister 直查路径（store.py:105-114 else 分支） |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：工具带 ns（非 ASCII name）→ 代表值 = `("skill", "天气预报")`（safe_name=`skill_a68661fb`）

**Given**（前置条件）：
- 注册 `Tool(namespace="skill", name="天气预报", ...)`

**When**（操作/动作）：
- `ok = store.unregister("skill_天气预报")`

**Then**（预期结果）：
- `ok == True`；`store.get("skill_天气预报") is None`；`store.get("skill_a68661fb") is None`（索引同步清理）
- `"skill_天气预报" in store` 为 False；`len(store) == 0`
- `store.version == 2`（register 1 + unregister 1）
- 副作用：`_safe_name_index == {}`（白盒，safe_name 索引已 pop）

---

#### 用例 ST-7：unregister 用 LLM-safe 名注销

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-st5 + Risk-st5 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | unregister 反查索引路径（store.py:106-112 if 分支） |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：注销入参 ∈ {safe_name} → 代表值 = `"skill_a68661fb"`

**Given**（前置条件）：
- 注册 `Tool(namespace="skill", name="天气预报", ...)`（safe_name=`skill_a68661fb`）

**When**（操作/动作）：
- `ok = store.unregister("skill_a68661fb")`

**Then**（预期结果）：
- `ok == True`；`store.get("skill_天气预报") is None`
- `"skill_a68661fb" in store` 为 False
- 副作用：索引与 _tools 均清理

---

#### 用例 ST-8：命名空间清理 — ns 下无剩余工具时移除该 ns（旧 split bug 回归）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-st6 + Risk-st2 [P0]（§9 修复：改用 `tool.namespace` 字段而非 `full_name.split(".",1)`） |
| 测试层级 | component(fake) |
| 覆盖准则 | unregister 的 ns 清理分支（store.py:126-132）——**必须覆盖"name 含下划线"场景**，这正是旧 bug 的触发条件 |
| Oracle | 白盒状态断言（`_namespace_registry`） |
| Mock | 否 |

**等价类划分**：同 ns 下工具数 ∈ {1, 2}；name 含下划线（触发旧 split 误判）→ 代表值 = ns="ns" 下注册 `("ns","my_tool_天气")` 与 `("ns","other")`

**Given**（前置条件）：
- 注册 a = `Tool(namespace="ns", name="my_tool_天气", ...)`、b = `Tool(namespace="ns", name="other", ...)`；`_namespace_registry == {"ns"}`（白盒确认）

**When**（操作/动作）：
- ① `store.unregister("ns_my_tool_天气")` → 检查 ns 是否保留
- ② `store.unregister("ns_other")` → 再检查

**Then**（预期结果）：
- ① 注销 a 后：`_namespace_registry == {"ns"}`（b 仍在 ns 下，ns 必须保留）
- ② 注销 b 后：`_namespace_registry == set()`（ns 下无剩余工具，**必须移除**；❌ 若残留 `{"ns"}` 即旧 split bug 复发）
- 副作用：无（version 各 +1）

---

#### 用例 ST-9：unregister 不存在的工具 → False 且零副作用

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-st7（幂等/无副作用） |
| 测试层级 | component(fake) |
| 覆盖准则 | unregister 未命中路径（store.py:108-112） |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：入参 ∈ {未注册 full_name、未注册 safe_name} → 代表值 = `"ghost"`、`"ghost_a68661fb"`

**Given**（前置条件）：
- 空 store（或仅注册 1 个无关工具）

**When**（操作/动作）：
- `ok1 = store.unregister("ghost")`；`ok2 = store.unregister("ghost_a68661fb")`

**Then**（预期结果）：
- `ok1 == False`、`ok2 == False`
- `len(store)` 不变、`store.version` 不变、`_safe_name_index` 不变（白盒）
- 副作用：无

---

#### 用例 ST-10：查询族基本行为与空 schema 注册拒绝

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-st1（校验链路）+ Risk-st3；查询族 `__len__`/`items`/`list_by_tier`/`list_names`/`__contains__` |
| 测试层级 | component(fake) |
| 覆盖准则 | validate_required_fields 的空 input_schema 分支（validator.py:43-46） |
| Oracle | 状态断言 + 异常断言 |
| Mock | 否 |

**等价类划分**：input_schema ∈ {空 dict、合法}；tier ∈ {ALWAYS, DEFERRED}

**Given**（前置条件）：
- 注册 `always_tool = Tool(tier=ToolTier.ALWAYS, name="always_t", ...)`、`def_tool = Tool(tier=ToolTier.DEFERRED, name="def_t", ...)`

**When**（操作/动作）：
- ① `store.register(坏工具)`，其中坏工具 `input_schema={}`
- ② `store.items()` / `store.list_all()` / `store.list_names()` / `store.list_by_tier(ToolTier.ALWAYS)` / `"def_t" in store`

**Then**（预期结果）：
- ① 抛 `ToolRegistrationError`，消息含 "input_schema 为空"；`len(store)` 不变
- ② `items()` 返回 2 个 `(full_name, tool)`；`list_names()` == `["always_t", "def_t"]`；`list_by_tier(ALWAYS)` 只含 always_tool；`__contains__` 均 True
- 副作用：无

---

### 7.3 registry/discovery.py（unit · 零 mock）

> 公共前置：`dm = DiscoveryManager(max_discovered=N)`。所有写入入口仅 `discover`（+ `undiscover`/`restore`/`clear`），其余为查询。

#### 用例 DI-1：discover 写入 + is_discovered/get_step 查询

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-d1（唯一写入点） |
| 测试层级 | unit |
| 覆盖准则 | discover 常规路径 |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：step_n ∈ {0, 1, 正数} → 代表值 = `("tool_a", 5)`

**Given**（前置条件）：
- `dm = DiscoveryManager(max_discovered=20)`

**When**（操作/动作）：
- `dm.discover("tool_a", 5)`

**Then**（预期结果）：
- `dm.is_discovered("tool_a") == True`；`dm.get_step("tool_a") == 5`
- `dm.is_discovered("tool_b") == False`；`dm.get_step("tool_b") is None`
- `len(dm) == 1`
- 副作用：无（无外部 I/O）

---

#### 用例 DI-2：LRU 淘汰 — 超上限时逐出 step_n 最小者（golden）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-d2 + inv-d3 + Risk-d1 [P0] |
| 测试层级 | unit |
| 覆盖准则 | `_evict_lru`（discovery.py:79-87）主路径 |
| Oracle | golden 状态断言（可手工推导：step 最小者逐出） |
| Mock | 否 |

**等价类划分**：step_n 有序性 ∈ {递增、乱序} → 代表值 = 依次 discover (a,1),(b,2),(c,3)（递增）；对照乱序 (x,10),(y,5),(z,1)

**Given**（前置条件）：
- `dm = DiscoveryManager(max_discovered=2)`

**When**（操作/动作）：
- `dm.discover("a", 1)`；`dm.discover("b", 2)`；`dm.discover("c", 3)`

**Then**（预期结果）：
- `len(dm) == 2`
- `dm.is_discovered("a") == False`（step_n=1 最小者被逐出）
- `dm.is_discovered("b") == True`、`dm.is_discovered("c") == True`
- 乱序对照：discover (x,10),(y,5),(z,1) 后，`is_discovered("z") == False`（step_n=1 最小）
- 副作用：无

---

#### 用例 DI-3：update_step 刷新 step_n 影响 LRU 排序（旧 P3 修复回归）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-d4 + Risk-d2 [P1]（§9 修复：facade 成功路径调用 update_step） |
| 测试层级 | unit |
| 覆盖准则 | update_step 命中路径（discovery.py:49-50）+ 后续 LRU |
| Oracle | 状态断言（step 值 + 淘汰结果可推导） |
| Mock | 否 |

**等价类划分**：刷新后 step 顺序反转 → 代表值 = 先 discover (a,1),(b,2)，update_step("a", 10)，再 discover (c,3)

**Given**（前置条件）：
- `dm = DiscoveryManager(max_discovered=2)`；discover (a,1)、(b,2)

**When**（操作/动作）：
- `dm.update_step("a", 10)`；`dm.discover("c", 3)`

**Then**（预期结果）：
- `dm.get_step("a") == 10`（step 已刷新）
- 触发淘汰时：`is_discovered("b") == False`（b 的 step=2 现在最小）、`is_discovered("a") == True`（a 刚被"使用"，step=10 保留）
- ❌ 若 b 保留而 a 被逐 → update_step 未生效，旧 bug 复发
- 副作用：无

---

#### 用例 DI-4：恰好等于 max_discovered 不淘汰

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-d3 边界（`> max` 才淘汰，`<=` 不淘汰） |
| 测试层级 | unit |
| 覆盖准则 | `_evict_lru` 前置守卫（discovery.py:81-82） |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：数量 ∈ {max, max+1} → 代表值 = max=2，discover 2 个 vs 3 个

**Given**（前置条件）：
- `dm = DiscoveryManager(max_discovered=2)`；discover (a,1)、(b,2)

**When**（操作/动作）：
- 检查后 discover (c,3)

**Then**（预期结果）：
- 2 个时：`len(dm) == 2`，a、b 均保留
- 第 3 个后：`len(dm) == 2`，a（step 最小）被逐出
- 副作用：无

---

#### 用例 DI-5：snapshot/restore 往返无损 + restore 超限触发淘汰

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-d5 + Risk-d3 [P1] |
| 测试层级 | unit |
| 覆盖准则 | restore 两条路径（未超限原样恢复 / 超限触发 _evict_lru，discovery.py:56-61） |
| Oracle | 状态断言（restore(snapshot()) 相等性 + 超限淘汰可推导） |
| Mock | 否 |

**等价类划分**：restore 状态规模 ∈ {≤max, >max} → 代表值 = {a:1,b:2} 与 {a:1,b:2,c:3,d:4}

**Given**（前置条件）：
- `dm = DiscoveryManager(max_discovered=2)`；discover (a,1)、(b,2)

**When**（操作/动作）：
- `snap = dm.snapshot()`；`dm.clear()`；`dm.restore(snap)`（往返）；再 `dm.restore({"a":1,"b":2,"c":3,"d":4})`（超限）

**Then**（预期结果）：
- 往返后：`dm.snapshot() == {"a":1, "b":2}`（无损）
- 超限 restore 后：`len(dm) == 2`，保留 step 最大的 `{"c":3, "d":4}`（a、b 被逐）
- 副作用：无

---

#### 用例 DI-6：update_step 未发现无副作用 + undiscover/clear

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-d6 + Risk-d4（undiscover 语义） |
| 测试层级 | unit |
| 覆盖准则 | update_step 未命中路径（discovery.py:49 守卫）+ undiscover 两分支 + clear |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：操作 ∈ {update_step 未发现名、undiscover 已发现/未发现、clear}

**Given**（前置条件）：
- `dm = DiscoveryManager(max_discovered=20)`；discover (a,1)

**When**（操作/动作）：
- ① `dm.update_step("ghost", 9)`；② `ok1 = dm.undiscover("a")`；`ok2 = dm.undiscover("a")`；③ `dm.discover("b", 2)`；`dm.clear()`

**Then**（预期结果）：
- ① `is_discovered("ghost") == False`、`len(dm) == 1`（update_step 不新增条目，inv-d6）
- ② `ok1 == True`（a 移除）；`ok2 == False`（已不存在）
- ③ clear 后 `len(dm) == 0`、`snapshot() == {}`
- 副作用：无

---

### 7.4 exposure/schema_builder.py（component(fake) · 全内存依赖）

> 公共前置：`store = ToolStore()`；`discovery = DiscoveryManager()`；`gate = GateChain.default()`；`budget = ToolBudget()`；`builder = SchemaBuilder(store, discovery, gate, budget)`；`ctx = ExposureContext()`（空上下文 = 全放行）。工具构造用 §6 约定。

#### 用例 SB-1：三段式基础 — ALWAYS 进 schemas、未发现 DEFERRED 进 catalog+enum、已发现 DEFERRED 进 schemas

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-sb3（三段分类） |
| 测试层级 | component(fake) |
| 覆盖准则 | build 三段分支（schema_builder.py:92-113） |
| Oracle | 状态断言（BuildResult 各字段） |
| Mock | 否 |

**等价类划分**：tier ∈ {ALWAYS, DEFERRED} × 发现状态 ∈ {已发现, 未发现}

**Given**（前置条件）：
- 注册 `always_tool`（ALWAYS，name="always_t"）、`def_unfound`（DEFERRED，name="def_unfound"）、`def_found`（DEFERRED，name="def_found"）
- `discovery.discover("def_found", 1)`

**When**（操作/动作）：
- `result = builder.build(ctx)`

**Then**（预期结果）：
- `result.schemas` 的 name 集合 == `{"always_t", "def_found"}`（ALWAYS + 已发现 DEFERRED）
- `result.deferred_catalog` 含 `def_unfound` 的 `{"name": "def_unfound", "when_to_use": ...}` 摘要
- `result.search_enum == ["def_unfound"]`
- `result.stats.always_count == 1`、`deferred_found_count == 1`、`deferred_unfound_count == 1`、`filtered_count == 0`
- 副作用：无（build 只读 store/discovery）

---

#### 用例 SB-2：非 ASCII 工具 schema.name == store 索引 key（LLM 回传闭环，P0 回归）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-sb1 + inv-sb2 + Risk-sb1 + Risk-sb3 [P0]（§9 修复核心闭环：`to_safe_name_parts` 精确计算） |
| 测试层级 | component(fake) |
| 覆盖准则 | `_to_schema`（schema_builder.py:163-171）全路径 |
| Oracle | 蜕变关系（对每个 schema.name 执行 `store.get(name)` 必须命中）+ golden（safe_name 值） |
| Mock | 否 |

**等价类划分**：工具名非 ASCII ∈ {含下划线、无下划线} → 代表值 = `("ns","my_tool_天气")`、`("skill","天气预报")`；tier ∈ {ALWAYS, DEFERRED 已发现}

**Given**（前置条件）：
- 注册 a = `Tool(namespace="ns", name="my_tool_天气", tier=ALWAYS, ...)`、b = `Tool(namespace="skill", name="天气预报", tier=DEFERRED, ...)`
- `discovery.discover("skill_天气预报", 1)`

**When**（操作/动作）：
- `result = builder.build(ctx)`

**Then**（预期结果）：
- `result.schemas` 中两个 schema 的 `name` 分别为 `"ns_085755d5"`、`"skill_a68661fb"`（golden）
- **闭环断言**：对每个 `s in result.schemas`：`store.get(s.name) is not None` 且 `store.get(s.name).name` 为原始名（LLM 回传 schema 名必能反查到真实工具）
- 全部 `s.name.isascii() == True`（inv-sb2）
- 副作用：无

---

#### 用例 SB-3：search_tools 动态 enum 只含未发现 DEFERRED 的 safe_name

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-sb4 + Risk-sb2 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | `_build_search_tool_schema`（schema_builder.py:174-195）+ 未发现分支收集 |
| Oracle | 状态断言（enum 与未发现集合一一对应） |
| Mock | 否 |

**等价类划分**：DEFERRED ∈ {未发现、已发现}；ALWAYS 存在 → 代表值 = 未发现 `("skill","天气预报")`、已发现 `("skill","已发现工具")`、ALWAYS `always_t`

**Given**（前置条件）：
- 注册 `search_tools`（用 `SearchToolFactory().create_tools()[0]`，ALWAYS）、未发现 `("skill","天气预报")`、已发现 `("skill","工具B")`、ALWAYS `always_t`
- `discovery.discover("skill_工具B", 1)`

**When**（操作/动作）：
- `result = builder.build(ctx)`

**Then**（预期结果）：
- `result.search_enum == ["skill_a68661fb"]`（只含未发现 DEFERRED 的 safe_name，❌ 若含 `always_t`/`search_tools`/已发现工具即 bug）
- schemas 中存在 name == `"search_tools"` 的 schema，其 `parameters.properties.tool_name.enum == ["skill_a68661fb"]`
- 副作用：无

---

#### 用例 SB-4：门链过滤生效 + filtered_count 统计

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-sb3 + Risk-sb4（过滤后三段正确收缩） |
| 测试层级 | component(fake) |
| 覆盖准则 | GateChain.filter 与 build 的协作路径 |
| Oracle | 状态断言 |
| Mock | 否（GateChain.default() 真门链） |

**等价类划分**：ctx.agent_allowed_tools ∈ {None, 子集} → 代表值 = `{"always_t"}`（排除 def_unfound）

**Given**（前置条件）：
- 注册 `always_t`（ALWAYS）、`def_unfound`（DEFERRED）
- `ctx = ExposureContext(agent_allowed_tools={"always_t"})`

**When**（操作/动作）：
- `result = builder.build(ctx)`

**Then**（预期结果）：
- `result.schemas` name 集合 == `{"always_t"}`（def_unfound 被 AllowListGate 过滤）
- `result.stats.filtered_count == 1`；`deferred_unfound_count == 0`（被过滤者不进 catalog 也不进 enum）
- `result.search_enum == []`
- 副作用：无

---

#### 用例 SB-5：各段内按 name 字母序升序

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-sb5 |
| 测试层级 | component(fake) |
| 覆盖准则 | build 尾部三个 sort（schema_builder.py:116-119） |
| Oracle | 状态断言（有序性） |
| Mock | 否 |

**等价类划分**：注册顺序与字母序相反 → 代表值 = 注册 z_alpha、a_omega（ALWAYS 与未发现 DEFERRED 各一对）

**Given**（前置条件）：
- 注册 ALWAYS：`z_always`、`a_always`；未发现 DEFERRED：`z_def`、`a_def`

**When**（操作/动作）：
- `result = builder.build(ctx)`

**Then**（预期结果）：
- `[s.name for s in result.schemas if s.name.startswith(("a_","z_"))]` 按字母升序（`a_always` 在 `z_always` 前）
- `[d["name"] for d in result.deferred_catalog]` 升序（`a_def` 在 `z_def` 前）
- `result.search_enum` 升序
- 副作用：无

---

#### 用例 SB-6：search_tools 被门链过滤 → 其 schema 不出现，其他 ALWAYS 不受影响

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-sb7 + Risk-sb4（search_tools 特判不误伤） |
| 测试层级 | component(fake) |
| 覆盖准则 | build 中 `search_passed` 分支（schema_builder.py:125-132） |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：ctx.agent_allowed_tools ∈ {含 search_tools, 不含} → 代表值 = `{"always_t"}`（不含 search_tools）

**Given**（前置条件）：
- 注册 `search_tools`（ALWAYS）、`always_t`（ALWAYS）
- `ctx = ExposureContext(agent_allowed_tools={"always_t"})`

**When**（操作/动作）：
- `result = builder.build(ctx)`

**Then**（预期结果）：
- `result.schemas` 不含 name == `"search_tools"` 的 schema（被过滤）
- 但 `"always_t"` 的 schema 仍在（ALWAYS 特判不误伤）
- `result.search_enum` 仍正确收集（未发现 DEFERRED 的 enum 独立于 search_tools 是否暴露）
- 副作用：无

---

#### 用例 SB-7：DEFERRED 已发现仍保留在 deferred_catalog（缓存命中保持）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-sb6（schema_builder.py:100-104 显式注释） |
| 测试层级 | component(fake) |
| 覆盖准则 | 已发现分支的 catalog 追加（schema_builder.py:100-104） |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：DEFERRED 已发现数量 ∈ {1} → 代表值 = `("skill","天气预报")`

**Given**（前置条件）：
- 注册 `("skill","天气预报")`（DEFERRED）；`discovery.discover("skill_天气预报", 1)`

**When**（操作/动作）：
- `result = builder.build(ctx)`

**Then**（预期结果）：
- `result.deferred_catalog` **包含** `{"name": "skill_a68661fb", "when_to_use": ...}`（已发现也保留摘要）
- 同时 `result.schemas` 含完整 `"skill_a68661fb"` schema
- `result.search_enum == []`（已发现不在 enum）
- 副作用：无

---

#### 用例 SB-8：token 预算裁剪 — 从尾部裁 DEFERRED，保留 ALWAYS

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-sb3（裁剪不破坏三段语义） |
| 测试层级 | component(fake) |
| 覆盖准则 | budget.enforce 裁剪路径（budget.py:59-66） |
| Oracle | 状态断言 |
| Mock | 否（ToolBudget 真实现） |

**等价类划分**：tool_schema_tokens ∈ {None, 极小值} → 代表值 = `tool_schema_tokens=1`

**Given**（前置条件）：
- 注册 `always_t`（ALWAYS）、`def_found`（DEFERRED 已发现，`discovery.discover("def_found",1)`）

**When**（操作/动作）：
- `result = builder.build(ctx, tool_schema_tokens=1)`

**Then**（预期结果）：
- schemas 非空且**不含 `def_found`**（尾部 DEFERRED 先被裁）
- 若 `always_t` 也被裁，则裁剪不能低于 `budget.max_always_count` 保底（默认 15，本例 1 个 ALWAYS 不会低于保底 → 保留）
- 副作用：无

---

### 7.5 execution/executor.py（unit · 测试内闭包 executor）

> 公共前置：`executor = ToolExecutor()`。工具构造见 §6；测试内 executor 闭包按用例注入。异步用例走 pytest-asyncio。

#### 用例 EX-1：filter_extra_args / coerce_args 为公开方法（API 形状回归）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex2 + inv-ex3 + Risk-ex5 [P1]（§9 修复：去掉下划线） |
| 测试层级 | unit |
| 覆盖准则 | 无（API 形状断言） |
| Oracle | 属性存在性断言 |
| Mock | 否 |

**等价类划分**：方法名 ∈ {公开名, 旧下划线名}

**Given**（前置条件）：
- `executor = ToolExecutor()`

**When**（操作/动作）：
- `hasattr(executor, "filter_extra_args")`；`hasattr(executor, "coerce_args")`；`hasattr(executor, "_filter_extra_args")`

**Then**（预期结果）：
- 公开名均存在且为可调用方法
- ❌ 旧名 `_filter_extra_args` 不存在（若存在说明 API 未迁移）
- 副作用：无

---

#### 用例 EX-2：filter_extra_args 移除 schema 外参数并返回被移除列表

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex2（多余参数移除） |
| 测试层级 | unit |
| 覆盖准则 | filter_extra_args 主分支（executor.py:135-153） |
| Oracle | golden 状态断言 |
| Mock | 否 |

**等价类划分**：args ∈ {全合法、含多余、properties 缺失} → 代表值 = `{"a":1, "ghost":2, "b":3}`（schema properties={a,b}）；`{"x":1}`（schema 无 properties）

**Given**（前置条件）：
- 构造 `tool`，`input_schema={"type":"object","properties":{"a":{"type":"integer"},"b":{"type":"integer"}}}`

**When**（操作/动作）：
- `filtered, removed = executor.filter_extra_args(tool, {"a":1, "ghost":2, "b":3})`

**Then**（预期结果）：
- `filtered == {"a":1, "b":3}`；`removed == ["ghost"]`（排序后）
- 无 properties 的 schema：`executor.filter_extra_args(tool_no_props, {"x":1})` → 返回 `({"x":1}, [])`（不报错）
- 副作用：无

---

#### 用例 EX-3：coerce_args 类型强转 golden（"20"→20、"true"→True）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex3（类型强转） |
| 测试层级 | unit |
| 覆盖准则 | `_coerce_value` 的 integer/number/boolean 成功分支（executor.py:195-223） |
| Oracle | golden 值（可手工推导） |
| Mock | 否 |

**等价类划分**：type ∈ {integer, number, boolean} × 输入 ∈ {str 数字、str bool、数字、bool}

**Given**（前置条件）：
- `tool` 的 schema properties：`{"i":{"type":"integer"},"n":{"type":"number"},"b":{"type":"boolean"}}`

**When**（操作/动作）：
- `coerced, changed = executor.coerce_args(tool, {"i":"20", "n":"1.5", "b":"true"})`

**Then**（预期结果）：
- `coerced == {"i":20, "n":1.5, "b":True}`（`"20"`→int 20、`"1.5"`→float 1.5、`"true"`→True）
- `changed == ["i","n","b"]`
- 补充分支：`"b":"yes"`→True、`"b":"0"`→False、`"b":""`→False、`"b":1`→True、`"n":True`→1.0（number 分支 bool→float）
- 副作用：无

---

#### 用例 EX-4：coerce_args 无效转换保留原值且不列入 changed

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex3 + Risk-ex2（转换失败不误改） |
| 测试层级 | unit |
| 覆盖准则 | `_coerce_value` 的 except 分支（executor.py:197-200 / 207-210 / 219-220） |
| Oracle | 状态断言（原值保留） |
| Mock | 否 |

**等价类划分**：无法转换 ∈ {`"abc"`→int、`"maybe"`→bool、`"1.2.3"`→float} → 代表值 = `{"i":"abc", "b":"maybe", "n":"1.2.3"}`

**Given**（前置条件）：
- `tool` 的 schema properties：`{"i":{"type":"integer"},"n":{"type":"number"},"b":{"type":"boolean"}}`

**When**（操作/动作）：
- `coerced, changed = executor.coerce_args(tool, {"i":"abc", "n":"1.2.3", "b":"maybe"})`

**Then**（预期结果）：
- `coerced == {"i":"abc", "n":"1.2.3", "b":"maybe"}`（原值保留）
- `changed == []`（无转换声明）
- 副作用：无

---

#### 用例 EX-5：bool 是 int 子类陷阱 — True→1、20.0→20 语义

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex3 + Risk-ex4（bool/int 子类顺序，executor.py:201-204 显式排后） |
| 测试层级 | unit |
| 覆盖准则 | `_coerce_value` 的 bool→int / float==int→int 分支 |
| Oracle | golden 值（可推导） |
| Mock | 否 |

**等价类划分**：integer 输入 ∈ {True, 20.0, "20.5"} → 代表值 = `{"i":True}`、`{"i":20.0}`、`{"i":"20.5"}`

**Given**（前置条件）：
- `tool` schema properties：`{"i":{"type":"integer"}}`

**When**（操作/动作）：
- `executor.coerce_args(tool, {"i":True})`；`executor.coerce_args(tool, {"i":20.0})`；`executor.coerce_args(tool, {"i":"20.5"})`

**Then**（预期结果）：
- `{"i":True}` → `{"i":1}`（bool 按 int 处理，不因 `isinstance(True, int)` 误当数字串）
- `{"i":20.0}` → `{"i":20}`（float 整值转 int）
- `{"i":"20.5"}` → `{"i":"20.5"}`（int("20.5") 抛 ValueError → 保留原值）
- 副作用：无

---

#### 用例 EX-6：execute 成功路径（async executor）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex4（成功 → ToolResult(success=True)） |
| 测试层级 | unit |
| 覆盖准则 | execute 的 async 分支（executor.py:70-71）+ 结果包装（83-96） |
| Oracle | 状态断言（ToolResult 字段） |
| Mock | 否（测试内 async 闭包） |

**等价类划分**：executor 返回 ∈ {纯值} → 代表值 = `async def fn(ctx, **kw): return {"ok": 1}`

**Given**（前置条件）：
- 构造 `tool`，executor 为 async 闭包返回 `{"ok":1}`；`context = ToolContext(run_id="r", step_n=1, agent_id="a", session_id="s")`

**When**（操作/动作）：
- `result = await executor.execute(tool, {}, context)`

**Then**（预期结果）：
- `result.success == True`；`result.data == {"ok":1}`（dict 保留原样，executor.py:112-113）
- `result.tool_name == tool.full_name`；`result.error == ""`
- 副作用：无（executor 闭包只读）

---

#### 用例 EX-7：execute 成功路径（sync executor 走线程池，事件循环不阻塞）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex6（run_in_executor）+ Risk-ex5 |
| 测试层级 | unit |
| 覆盖准则 | execute 的 sync 分支（executor.py:73-79） |
| Oracle | 状态断言（返回值正确 + 在事件循环内完成） |
| Mock | 否（测试内 sync 闭包） |

**等价类划分**：executor 为同步函数 → 代表值 = `def fn(ctx, **kw): return "sync_result"`

**Given**（前置条件）：
- 构造 `tool`，executor 为普通 sync 函数返回 `"sync_result"`；`context` 同 EX-6

**When**（操作/动作）：
- `result = await executor.execute(tool, {}, context)`

**Then**（预期结果）：
- `result.success == True`；`result.data == "sync_result"`（str 原样，executor.py:110-111）
- 整个调用在测试事件循环内 await 完成（无阻塞死锁）
- 副作用：无

---

#### 用例 EX-8：工具执行异常 → ToolResult(success=False)，永不外抛（O3 核心）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex1 + Risk-ex1 [P0]（O3 核心，§9 机制 6） |
| 测试层级 | unit |
| 覆盖准则 | execute 的 except 分支（executor.py:98-107） |
| Oracle | 状态断言（无异常上抛 + error 字段） |
| Mock | 否（测试内抛异常闭包） |

**等价类划分**：异常类型 ∈ {ValueError、RuntimeError、自定义异常} → 代表值 = `raise ValueError("boom")`

**Given**（前置条件）：
- 构造 `tool`，executor 为 `def fn(ctx, **kw): raise ValueError("boom")`；无 error_formatter；`context` 同 EX-6

**When**（操作/动作）：
- `result = await executor.execute(tool, {}, context)`

**Then**（预期结果）：
- 调用**不抛异常**（pytest.raises 不应触发）
- `result.success == False`
- `result.error == "工具 'X' 执行失败: ValueError: boom"`（默认格式，`_format_error` 回落）
- `result.tool_name == tool.full_name`
- 副作用：无

---

#### 用例 EX-9：error_formatter 正常生效（自定义错误文案）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex1（异常仍转结果）+ lifecycle.error_formatter 钩子 |
| 测试层级 | unit |
| 覆盖准则 | `_format_error` 的 formatter 成功分支（executor.py:228-230） |
| Oracle | golden 断言（formatter 输出固定文案） |
| Mock | 否（测试内 formatter 闭包） |

**等价类划分**：formatter ∈ {自定义} → 代表值 = `lambda exc, name: f"[FMT]{name}:{exc}"`

**Given**（前置条件）：
- 构造 `tool`，executor 抛 `RuntimeError("x")`；`lifecycle = ToolLifecycle(error_formatter=lambda exc, name: f"[FMT]{name}:{exc}")`

**When**（操作/动作）：
- `result = await executor.execute(tool, {}, context)`

**Then**（预期结果）：
- `result.success == False`；`result.error == "[FMT]<full_name>:x"`（formatter 输出被采用）
- 副作用：无

---

#### 用例 EX-10：error_formatter 自身异常 → 留痕 + 回落默认格式（旧 P3 修复回归）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex5 + Risk-ex3 [P1]（§9 修复：不再 `except: pass` 静默吞，改 warning 留痕） |
| 测试层级 | unit |
| 覆盖准则 | `_format_error` 的 formatter except 分支（executor.py:231-236） |
| Oracle | 状态断言 + 日志断言（caplog） |
| Mock | 否（测试内抛异常 formatter 闭包） |

**等价类划分**：formatter ∈ {自身抛异常} → 代表值 = `lambda exc, name: (_ for _ in ()).throw(RuntimeError("fmt_broken"))`

**Given**（前置条件）：
- 构造 `tool`，executor 抛 `ValueError("boom")`；`lifecycle = ToolLifecycle(error_formatter=<抛异常的 formatter>)`；caplog 捕获 `pandaren.tool.execution.executor` logger

**When**（操作/动作）：
- `result = await executor.execute(tool, {}, context)`

**Then**（预期结果）：
- `result.success == False`；`result.error == "工具 'X' 执行失败: ValueError: boom"`（回落默认格式）
- **留痕断言**：caplog 中存在 level>=WARNING 且消息含 `"error_formatter"` 与 `"回落默认格式"` 的记录（❌ 若无留痕 → 旧静默吞 bug 复发）
- 副作用：无

---

#### 用例 EX-11：validate_input 拦截（ValidationResult(valid=False)）与放行（None）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex7（Phase 1 校验） |
| 测试层级 | unit |
| 覆盖准则 | execute Phase 1 两分支（executor.py:54-61） |
| Oracle | 状态断言（executor 是否被执行） |
| Mock | 否 |

**等价类划分**：validate_input ∈ {返回无效 ValidationResult、返回 None} → 代表值 = `ValidationResult(valid=False, message="path not found")`

**Given**（前置条件）：
- 拦截场景：`lifecycle = ToolLifecycle(validate_input=lambda args, ctx: ValidationResult(valid=False, message="path not found"))`；executor 闭包内计数器 `calls += 1`
- 放行场景：`validate_input=lambda args, ctx: None`

**When**（操作/动作）：
- 拦截：`result = await executor.execute(tool_blocked, {"p":"/x"}, context)`；放行：`result = await executor.execute(tool_pass, {}, context)`

**Then**（预期结果）：
- 拦截：`result.success == False`、`result.error == "path not found"`、executor 闭包 `calls == 0`（未执行）
- 放行：`result.success == True`（执行继续）、`calls == 1`
- 副作用：无

---

#### 用例 EX-12：max_output_bytes 字节级截断 → truncated=True

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex8（Phase 4 截断，executor.py:121-127） |
| 测试层级 | unit |
| 覆盖准则 | 截断分支（len(encoded) > max_bytes） |
| Oracle | 状态断言（字节数 + 截断标记） |
| Mock | 否 |

**等价类划分**：输出长度 vs max ∈ {超限、未超限} → 代表值 = 输出 `"abcdefg"`（7 字节），`max_output_bytes=4`；对照 `max_output_bytes=None`

**Given**（前置条件）：
- 构造 `tool`，executor 返回 `"abcdefg"`；`policy = ToolPolicy(sensitivity=LOW, max_output_bytes=4)`

**When**（操作/动作）：
- `result = await executor.execute(tool, {}, context)`

**Then**（预期结果）：
- `result.truncated == True`
- `len(result.data.encode("utf-8")) <= 4`（前 4 字节，`"abcd"`；`errors="replace"` 不抛异常）
- 对照：`max_output_bytes=None` 时 `result.truncated == False`、data 完整
- 副作用：无

---

#### 用例 EX-13：修正提示注入 + execute 直接调用的双保险清洗 + raw ToolResult 包装

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex9 + inv-ex2/ex3（双保险：facade 独立调用 execute 场景也防御）+ inv-ex4 raw 包装 |
| 测试层级 | unit |
| 覆盖准则 | `_fix_hint` 注入分支（executor.py:129-131）+ raw ToolResult 包装（83-93）+ 内部清洗（39-42） |
| Oracle | 状态断言（data 前缀 + 透传字段） |
| Mock | 否 |

**等价类划分**：args ∈ {含多余+字符串数字}；executor 返回 ∈ {ToolResult} → 代表值 = args `{"valid":1, "ghost":2, "num":"20"}`；executor 返回 `ToolResult(success=True, data="raw", halt=True)`

**Given**（前置条件）：
- 场景 A：`tool` schema properties `{"valid":{"type":"integer"},"num":{"type":"integer"}}`；executor 闭包记录收到的 kwargs 并返回 `"done"`
- 场景 B：`tool2` executor 返回 `ToolResult(success=True, data="raw", halt=True)`

**When**（操作/动作）：
- A：`result = await executor.execute(tool, {"valid":1, "ghost":2, "num":"20"}, context)`（**不经 facade，直接 execute**）
- B：`result2 = await executor.execute(tool2, {}, context)`

**Then**（预期结果）：
- A：executor 收到 kwargs == `{"valid":1, "num":20}`（多余 `ghost` 已移除、"20" 已强转——双保险兜底生效）
- A：`result.success == True`；`result.data` 以 `"[参数修正] 已自动忽略无效参数：ghost。[类型修正] 已将以下字符串转为数字：num。"` 开头并含 `"done"`（inv-ex9）
- B：`result2.data == "raw"`、`result2.halt == True`（透传）、`result2.tool_name == tool2.full_name`（覆盖）
- 副作用：无

---

### 7.6 definition/tool_policy.py（unit · 零 mock）

#### 用例 TP-1：sensitivity 必填（无默认值）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-tp1 + Risk-tp2（构造签名漂移） |
| 测试层级 | unit |
| 覆盖准则 | dataclass 必填字段（tool_policy.py:33） |
| Oracle | 异常断言（TypeError） |
| Mock | 否 |

**等价类划分**：构造参数 ∈ {缺 sensitivity、给 SensitivityLevel 值}

**Given**（前置条件）：
- 无前置

**When**（操作/动作）：
- `ToolPolicy()`；`ToolPolicy(sensitivity=SensitivityLevel.LOW)`

**Then**（预期结果）：
- 缺 sensitivity → 抛 `TypeError`（dataclass missing required argument）
- 显式声明 → 构造成功，`p.sensitivity == SensitivityLevel.LOW`
- 副作用：无

---

#### 用例 TP-2：default_result_limit 字段已删除（回归防护）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-tp2 + Risk-tp1 [P2]（§9 修复：字段已删） |
| 测试层级 | unit |
| 覆盖准则 | 字段删除的 API 形状 |
| Oracle | 异常断言（TypeError） |
| Mock | 否 |

**等价类划分**：关键字 ∈ {default_result_limit}

**Given**（前置条件）：
- 无前置

**When**（操作/动作）：
- `ToolPolicy(sensitivity=SensitivityLevel.LOW, default_result_limit=5)`

**Then**（预期结果）：
- 抛 `TypeError: __init__() got an unexpected keyword argument 'default_result_limit'`（已核实）
- 副作用：无

---

#### 用例 TP-3：supports_offset_pagination 保留

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-tp3（保留字段的默认值与覆盖） |
| 测试层级 | unit |
| 覆盖准则 | 字段默认/覆盖 |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：覆盖 ∈ {不传、True}

**Given**（前置条件）：
- 无前置

**When**（操作/动作）：
- `ToolPolicy(sensitivity=SensitivityLevel.LOW)`；`ToolPolicy(sensitivity=SensitivityLevel.LOW, supports_offset_pagination=True)`

**Then**（预期结果）：
- 默认 `supports_offset_pagination == False`
- 覆盖后 `== True`
- 副作用：无

---

### 7.7 facade.py（component(fake) · 全内存组件）

> 公共前置：`registry = ToolRegistry()`（内部真实 ToolStore/DiscoveryManager/GateChain/SchemaBuilder/ToolExecutor）。工具构造见 §6。异步用例走 pytest-asyncio。

#### 用例 FA-1：execute_tool 未注册工具 → success=False，不外抛

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-f1 + Risk-f1 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | execute_tool 的未注册分支（facade.py:242-249） |
| Oracle | 状态断言（ToolResult 字段） |
| Mock | 否 |

**等价类划分**：tool_name ∈ {未注册 full_name、未注册 safe_name} → 代表值 = `"ghost_tool"`

**Given**（前置条件）：
- 空 registry；`context = ToolContext(run_id="r", step_n=1, agent_id="a", session_id="s")`

**When**（操作/动作）：
- `result = await registry.execute_tool("ghost_tool", {}, context)`

**Then**（预期结果）：
- 调用不抛异常
- `result.success == False`；`result.error == "Tool 'ghost_tool' 未注册"`；`result.tool_name == "ghost_tool"`
- 副作用：无（discovery 未被写入）

---

#### 用例 FA-2：DEFERRED 首次执行成功 → discovery.discover（full_name + step_n）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-f2 + Risk-f2 [P1]（§9 修复：成功路径维护发现状态） |
| 测试层级 | component(fake) |
| 覆盖准则 | execute_tool 成功分支的 discover 路径（facade.py:286-292） |
| Oracle | 状态断言（discovery 写入值） |
| Mock | 否 |

**等价类划分**：tier = DEFERRED；入参 name ∈ {safe_name} → 代表值 = 注册 `("skill","天气预报")`，调用 `execute_tool("skill_a68661fb", ...)`

**Given**（前置条件）：
- 注册 DEFERRED `Tool(namespace="skill", name="天气预报", executor=<成功闭包>, ...)`
- `context = ToolContext(run_id="r", step_n=7, agent_id="a", session_id="s")`

**When**（操作/动作）：
- `result = await registry.execute_tool("skill_a68661fb", {}, context)`（LLM 用 safe_name 回调）

**Then**（预期结果）：
- `result.success == True`
- **副作用断言**：`registry.discovery.is_discovered("skill_天气预报") == True`（以 **full_name** 写入，非 safe_name）
- `registry.discovery.get_step("skill_天气预报") == 7`（== ctx.step_n）
- 副作用：discovery 状态已写入（这是本用例的关键验证点）

---

#### 用例 FA-3：DEFERRED 已发现再次执行 → update_step 刷新；ALWAYS 不写 discovery

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-f2 刷新路径 + inv-f3 + Risk-f2 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | execute_tool 成功分支的 update_step / ALWAYS 跳过（facade.py:287-292） |
| Oracle | 状态断言（step_n 前后对比） |
| Mock | 否 |

**等价类划分**：tier ∈ {DEFERRED 已发现、ALWAYS} → 代表值 = DEFERRED 先 discover(step=1) 再执行(step=5)；ALWAYS 执行

**Given**（前置条件）：
- 注册 DEFERRED `def_t`（成功闭包）、ALWAYS `always_t`（成功闭包）
- `registry.discovery.discover("def_t", 1)`（预置已发现）

**When**（操作/动作）：
- ① `ctx1 = ToolContext(run_id="r", step_n=5, agent_id="a", session_id="s")`；`await registry.execute_tool("def_t", {}, ctx1)`
- ② `ctx2 = ToolContext(run_id="r", step_n=3, agent_id="a", session_id="s")`；`await registry.execute_tool("always_t", {}, ctx2)`

**Then**（预期结果）：
- ① `registry.discovery.get_step("def_t") == 5`（step_n 从 1 刷新为 5，update_step 生效）
- ② `registry.discovery.is_discovered("always_t") == False`（ALWAYS 不写 discovery，inv-f3）
- 副作用：discovery 状态已按上述变更

---

#### 用例 FA-4：未发现 DEFERRED 被 DiscoveryGuard 拦截，executor 未执行

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-f4（GuardChain 第 4 道门） |
| 测试层级 | component(fake) |
| 覆盖准则 | GuardChain.check_all 拒绝路径（facade.py:260-263 → guard DiscoveryGuard） |
| Oracle | 状态断言（error 文案 + executor 未调用） |
| Mock | 否 |

**等价类划分**：tier = DEFERRED 未发现 → 代表值 = 注册 `("skill","天气预报")` 未 discover

**Given**（前置条件）：
- 注册 DEFERRED `("skill","天气预报")`，executor 闭包计数 `calls += 1`
- `context = ToolContext(...)`

**When**（操作/动作）：
- `result = await registry.execute_tool("skill_a68661fb", {}, context)`

**Then**（预期结果）：
- `result.success == False`；`result.error` 含 `"尚未加载 schema"` 与 `"search_tools"`（提示先调 search_tools）
- executor 闭包 `calls == 0`（拦截发生在执行前）
- 副作用：无（discovery 未被写入）

---

#### 用例 FA-5：facade 前置清洗（双保险）— 多余参数 + 字符串数字在到达 executor 前被修正

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-ex2/ex3 双保险 + Risk-f5 [P2]（§9 修复：facade 前置清洗 + executor 兜底） |
| 测试层级 | component(fake) |
| 覆盖准则 | facade 清洗路径（facade.py:269-270）→ jsonschema 校验通过 → executor |
| Oracle | 状态断言（executor 收到的 kwargs） |
| Mock | 否（jsonschema 已装则走真实校验；未装时 `_validate_args` 跳过——两种环境断言一致） |

**等价类划分**：args ∈ {多余参数 + 字符串数字} → 代表值 = `{"valid":1, "ghost":2, "num":"20"}`（schema properties={valid:integer, num:integer}）

**Given**（前置条件）：
- 注册 DEFERRED `tool`，schema properties `{"valid":{"type":"integer"},"num":{"type":"integer"}}`，executor 闭包记录 kwargs 返回 `"ok"`
- `registry.discovery.discover("tool", 1)`（先发现，绕过 DiscoveryGuard 聚焦清洗链路）

**When**（操作/动作）：
- `result = await registry.execute_tool("tool", {"valid":1, "ghost":2, "num":"20"}, context)`

**Then**（预期结果）：
- `result.success == True`
- executor 闭包收到的 kwargs == `{"valid":1, "num":20}`（`ghost` 已滤、`"20"` 已转）
- 副作用：discovery step 被刷新（成功路径，与 FA-2 一致）

---

#### 用例 FA-6：update_enabled_tools 按 is_enabled 重建 enabled_cache → 禁用工具被 EnabledGuard 拒

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-f5（is_enabled 回调 → cache） |
| 测试层级 | component(fake) |
| 覆盖准则 | `_check_one` 的 is_enabled 分支（facade.py:320-336）+ EnabledGuard |
| Oracle | 状态断言（cache 值 + 执行拒绝） |
| Mock | 否 |

**等价类划分**：is_enabled ∈ {返回 False、返回 True、为 None} → 代表值 = 工具 A `is_enabled=lambda ctx: False`、工具 B `is_enabled=lambda ctx: True`

**Given**（前置条件）：
- 注册 `tool_off`（lifecycle.is_enabled 返回 False）、`tool_on`（返回 True）；均 DEFERRED 且已 discover
- `context = ToolContext(...)`

**When**（操作/动作）：
- `await registry.update_enabled_tools(context=context)`；随后 `await registry.execute_tool("tool_off", {}, context)`；`await registry.execute_tool("tool_on", {}, context)`

**Then**（预期结果）：
- `registry._enabled_cache["tool_off"] == False`、`registry._enabled_cache["tool_on"] == True`（白盒，或经行为断言）
- `execute_tool("tool_off", ...)` → `success == False`，error 含 `"当前不可用"`（EnabledGuard 拒）
- `execute_tool("tool_on", ...)` → `success == True`
- 副作用：cache 已重建

---

#### 用例 FA-7：is_circuit_tripped 熔断 → 熔断工具被禁用（类型收敛回归）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-f5（circuit 分支，facade.py:312-318）+ Risk-f3 [P2]（§9 修复：参数类型收敛为 `Callable[[str], bool]`） |
| 测试层级 | component(fake) |
| 覆盖准则 | `_check_one` 的 is_circuit_tripped 分支 |
| Oracle | 状态断言（熔断名被禁用） |
| Mock | 否 |

**等价类划分**：is_circuit_tripped ∈ {对特定名返回 True/False} → 代表值 = `lambda name: name == "tool_a"`

**Given**（前置条件）：
- 注册 `tool_a`、`tool_b`（DEFERRED，已 discover）

**When**（操作/动作）：
- `await registry.update_enabled_tools(context=context, is_circuit_tripped=lambda name: name == "tool_a")`

**Then**（预期结果）：
- `registry._enabled_cache["tool_a"] == False`（熔断 → 禁用）、`registry._enabled_cache["tool_b"] == True`
- 行为断言：`execute_tool("tool_a", ...)` 被 EnabledGuard 拒（success=False）
- 副作用：cache 已重建

---

#### 用例 FA-8：unregister_tool 三处同步清理（store + enabled_cache + discovery）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-f6 + Risk-f4 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | unregister_tool 清理路径（facade.py:144-155） |
| Oracle | 状态断言（三处均无残留） |
| Mock | 否 |

**等价类划分**：入参 ∈ {safe_name} → 代表值 = 注册 `("skill","天气预报")`（safe=`skill_a68661fb`），discover 后注销

**Given**（前置条件）：
- 注册 `("skill","天气预报")`；`registry.discovery.discover("skill_天气预报", 1)`；`registry._enabled_cache["skill_天气预报"] = False`（预置脏缓存）

**When**（操作/动作）：
- `ok = registry.unregister_tool("skill_a68661fb")`（用 safe_name 注销）

**Then**（预期结果）：
- `ok == True`
- `registry.get_tool("skill_天气预报") is None`（store 清理）
- `registry.discovery.is_discovered("skill_天气预报") == False`（discovery 清理）
- `"skill_天气预报" not in registry._enabled_cache`（cache 清理，白盒）
- 再 `unregister_tool("skill_a68661fb")` → `False`（幂等）
- 副作用：无残留

---

#### 用例 FA-9：set_hooks 二次注入抛 RuntimeError + promote_to_discovered 语义

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-f7 + inv-f8（promote 以 full_name 写入 / ALWAYS 跳过 / 未注册静默） |
| 测试层级 | component(fake) |
| 覆盖准则 | set_hooks 锁分支（facade.py:107-112）+ promote_to_discovered 三分支（facade.py:218-228） |
| Oracle | 异常断言 + 状态断言 |
| Mock | 否（AgentHooks 用极简测试替身——纯内存，属 Fake） |

**等价类划分**：promote 入参 ∈ {safe_name、ALWAYS 名、未注册名} → 代表值 = `"skill_a68661fb"`、`"always_t"`、`"ghost"`

**Given**（前置条件）：
- `registry2 = ToolRegistry()`；注册 DEFERRED `("skill","天气预报")`、ALWAYS `always_t`；`hooks = <极简 AgentHooks 替身，仅满足 set_hooks 签名>`

**When**（操作/动作）：
- ① `registry2.set_hooks(hooks)` 两次
- ② `registry2.promote_to_discovered("skill_a68661fb", 5)`；`registry2.promote_to_discovered("always_t", 5)`；`registry2.promote_to_discovered("ghost", 5)`

**Then**（预期结果）：
- ① 第二次 `set_hooks` 抛 `RuntimeError`（消息含 "不允许二次替换"）
- ② `discovery.is_discovered("skill_天气预报") == True`、`get_step == 5`（safe_name 入参 → full_name 写入）
- `discovery.is_discovered("always_t") == False`（ALWAYS 静默跳过）
- 不抛异常（ghost 静默返回）
- 副作用：无

---

#### 用例 FA-10：build_tool_schemas 端到端 — schema 名全 ASCII 且经 store.get 反查命中

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-sb1/sb2 经 facade 传导 + Risk-sb1 [P0]（facade → SchemaBuilder 全链路闭环） |
| 测试层级 | component(fake) |
| 覆盖准则 | build_tool_schemas 的 ctx 组装 + builder.build（facade.py:182-194） |
| Oracle | 蜕变关系（每个 schema.name → store.get 命中）+ ASCII 断言 |
| Mock | 否 |

**等价类划分**：注册集合 = 1 ALWAYS + 2 DEFERRED（含非 ASCII 名）→ 代表值 = `always_t`、`("skill","天气预报")`（已发现）、`("ns","my_tool_天气")`（未发现）

**Given**（前置条件）：
- 注册上述 3 工具；`registry.discovery.discover("skill_天气预报", 1)`

**When**（操作/动作）：
- `schemas = registry.build_tool_schemas(agent_id="a1")`

**Then**（预期结果）：
- 每个 `s.name.isascii() == True`
- 对每个 `s in schemas`：`registry.get_tool(s.name) is not None`（schema 名 → store 反查闭环）
- `schemas` 含 `always_t` 与 `skill_a68661fb`，不含 `ns_my_tool_天气` 的完整 schema（未发现仅进摘要）
- 副作用：`registry.get_deferred_summaries()` 已缓存最近一轮 catalog

---

### 7.8 exposure/gate_chain.py（unit · 零 mock）

> 公共前置：工具构造见 §6；`ExposureContext` 为 frozen dataclass（gate_chain.py:57-63）。

#### 用例 GC-1：GateChain.default() 门链 = 4 道门且顺序固定

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-g1 + Risk-g1 [P2]（§9 修复：docstring 5 道 → 实际 4 道） |
| 测试层级 | unit |
| 覆盖准则 | default() 工厂（gate_chain.py:182-190） |
| Oracle | 状态断言（门名序列） |
| Mock | 否 |

**等价类划分**：门数量 ∈ {4} → 代表值 = `GateChain.default()`

**Given**（前置条件）：
- 无前置

**When**（操作/动作）：
- `chain = GateChain.default()`；读取 `chain._gates` 的 `name` 属性（白盒，或经 filter 行为推断）

**Then**（预期结果）：
- `[g.name for g in chain._gates] == ["allow_list", "enabled", "agent_whitelist", "skill_whitelist"]`（4 道门，顺序固定）
- 副作用：无

---

#### 用例 GC-2：未配置 = 全放行（Fail-Safe Default）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-g2（None 透明） |
| 测试层级 | unit |
| 覆盖准则 | 各门 `context 字段为 None → return True` 分支 |
| Oracle | 状态断言（全通过） |
| Mock | 否 |

**等价类划分**：ctx 字段 ∈ {全 None} → 代表值 = `ExposureContext()`；工具含白名单字段

**Given**（前置条件）：
- 注册 2 个工具（一个带 `agent_whitelist={"agent_x"}`）；`ctx = ExposureContext()`（全空）

**When**（操作/动作）：
- `passed = GateChain.default().filter(store.items(), ctx)`

**Then**（预期结果）：
- `len(passed) == 2`（全部放行，含带白名单工具——context.agent_id 为 None 时 AgentWhitelistGate 透明）
- 副作用：无

---

#### 用例 GC-3：AllowListGate — agent_allowed_tools 过滤

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-g4（AllowList 语义）+ Risk-g2 |
| 测试层级 | unit |
| 覆盖准则 | AllowListGate.should_pass 两分支（gate_chain.py:87-90） |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：agent_allowed_tools ∈ {None, 子集} → 代表值 = `{"tool_a"}`；工具 = tool_a、tool_b

**Given**（前置条件）：
- 注册 tool_a、tool_b；`ctx = ExposureContext(agent_allowed_tools={"tool_a"})`

**When**（操作/动作）：
- `passed = GateChain.default().filter(store.items(), ctx)`

**Then**（预期结果）：
- `[fn for fn, _ in passed] == ["tool_a"]`（tool_b 被 AllowListGate 过滤）
- 副作用：无

---

#### 用例 GC-4：EnabledGate + AgentWhitelistGate 组合过滤

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-g4（Enabled + AgentWhitelist 语义） |
| 测试层级 | unit |
| 覆盖准则 | EnabledGate.should_pass + AgentWhitelistGate.should_pass 各分支 |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：enabled_cache ∈ {含 False}；agent_whitelist ∩ agent_id ∈ {空} → 代表值 = cache `{"tool_b": False}`；tool_c 带 `agent_whitelist={"agent_x"}`、ctx.agent_id=`"agent_y"`

**Given**（前置条件）：
- 注册 tool_a、tool_b、tool_c（policy.agent_whitelist=frozenset({"agent_x"})）
- `ctx = ExposureContext(agent_id="agent_y", enabled_cache={"tool_b": False})`

**When**（操作/动作）：
- `passed = GateChain.default().filter(store.items(), ctx)`

**Then**（预期结果）：
- `[fn for fn, _ in passed] == ["tool_a"]`（tool_b 被 EnabledGate 拒；tool_c 被 AgentWhitelistGate 拒——agent_y 不在白名单）
- 副作用：无

---

#### 用例 GC-5：SkillWhitelistGate — skill_allowed_tools 过滤

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-g4（Skill 语义） |
| 测试层级 | unit |
| 覆盖准则 | SkillWhitelistGate.should_pass 两分支（gate_chain.py:128-131） |
| Oracle | 状态断言 |
| Mock | 否 |

**等价类划分**：skill_allowed_tools ∈ {None, 元组子集} → 代表值 = `("tool_a",)`

**Given**（前置条件）：
- 注册 tool_a、tool_b；`ctx = ExposureContext(skill_allowed_tools=("tool_a",))`

**When**（操作/动作）：
- `passed = GateChain.default().filter(store.items(), ctx)`

**Then**（预期结果）：
- `[fn for fn, _ in passed] == ["tool_a"]`
- 对照：`skill_allowed_tools=None` 时全放行（临时约束自动恢复语义）
- 副作用：无

---

#### 用例 GC-6：AND 短路 — 任一道门拒即过滤（组合交集）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-g3（短路 break，gate_chain.py:166-171） |
| 测试层级 | unit |
| 覆盖准则 | filter 循环的 break 路径 |
| Oracle | 状态断言（被多门同时拒的工具仍只过滤一次） |
| Mock | 否 |

**等价类划分**：工具同时被多道门拒 → 代表值 = tool_d（enabled_cache False 且不在 agent_allowed_tools）

**Given**（前置条件）：
- 注册 tool_d、tool_e；`ctx = ExposureContext(agent_allowed_tools={"tool_e"}, enabled_cache={"tool_d": False})`

**When**（操作/动作）：
- `passed = GateChain.default().filter(store.items(), ctx)`

**Then**（预期结果）：
- `[fn for fn, _ in passed] == ["tool_e"]`
- tool_d 被过滤一次即短路（若用计数门替身可断言 `should_pass` 调用次数为 1——第一道门拒后不再调后续门）
- 副作用：无

---

## 8. 覆盖准则达成声明

| 模块 | 目标覆盖 | 达成方式 |
|------|---------|---------|
| safe_name.py | 分支覆盖（3 个独立判定：namespace truthy / ns.isascii / name.isascii） | SN-1~SN-4 覆盖全部真/假组合；SN-5/6 property 兜底 |
| store.py | 分支覆盖（register 唯一性两分支、unregister 三路径、ns 清理两分支） | ST-5/6/7/8 逐分支 |
| discovery.py | 分支覆盖（_evict_lru 守卫、update_step 命中/未命中、restore 超限） | DI-3/4/5/6 |
| schema_builder.py | 三段分支 + search_tools 特判 + 排序 + 裁剪 | SB-1/3/6/8 |
| executor.py | 分支覆盖（_coerce_value 全部类型分支、execute Phase1-4、_format_error 两分支） | EX-3/4/5/10/11/12/13 |
| facade.py | 分支覆盖（execute_tool 成功/未注册/Guard 拒、discover/update_step/ALWAYS 跳过） | FA-1/2/3/4 |
| gate_chain.py | 每道门 should_pass 两分支 + 短路 | GC-2/3/4/5/6 |

> 未采用 MC-DC / Path 覆盖：被测判定均为单条件（无 `a && b || c` 复合），分支覆盖即等价于 MC-DC；Path 覆盖不现实（模块间组合路径爆炸），以模块级矩阵组合保证。

---

## 9. Known-Gap 清单

设计基于源码现状（§9 的 11 条源码问题已修复）。逐一核对后：

| 用例 | 期望行为 | 实际现状 | 差距原因 | 处理 |
|------|---------|---------|---------|------|
| 全部 | 与 §4 不变式一致 | 源码已按不变式实现（smoke 验证：safe_name 哈希、store 双向索引、discovery LRU、ToolPolicy 字段删除均符合） | 无差距 | 无需 xfail |

**当前无 known-gap。** 若实现后续偏离（如 `unregister` 命名空间清理退化、`update_step` 失去调用方、`default_result_limit` 回归），对应用例将自动失败报警——这正是回归防护的目的。

---

## 10. 修订记录

| 日期 | 版本 | 说明 |
|------|------|------|
| 2026-08-21 | v1.0 | 初版：基于源码通读 + smoke 验证（Python 3.12.8 / pytest_asyncio 可用）；golden values（md5 前缀）经 `hashlib` 独立计算，非跑被测实现抄取 |
