# 测试设计：SQLite 读侧防御改动（sqlite_read_defense）

**被测对象**：`pandaren/observability/backend/sqlite.py`（读侧防御）+ `backend/console.py`（export_span 渲染）+ `backend/markdown.py`（渲染回归）
**测试框架**：pytest（`tmp_path` + `caplog` + `capsys`），参考基座 `tests/test_sqlite_backend.py`
**设计日期**：2026-02（白盒分析基于当前工作区源码）

---

## 1. 前置信息（已确认）

| 项 | 值 | 来源 |
|----|----|------|
| 新增函数 | `_safe_enum(enum_cls, value, fallback=None, *, label="") -> Any` | sqlite.py:153 |
| Audit 读侧 | `_row_to_record(row) -> AuditRecord \| None`；`query()` 过滤 None | sqlite.py:368 / 365 |
| Tracer 读侧 | `_row_to_span(row) -> Span \| None`；`get_spans()` 过滤 None | sqlite.py:465 / 458 |
| 枚举定义 | `AuditEventType`(21 值) / `AuditSeverity`(INFO/WARN/CRITICAL) / `SpanType`(8 值) / `SpanStatus`(OK/ERROR/CANCELLED) | types.py |
| warning logger | `logging.getLogger("pandaren.observability.backend.sqlite")` | sqlite.py:58 |
| console 改动 | `export_span` 显示 `span.span_type.name.lower()` | console.py:84（已实现） |
| markdown | 未改动，回归守护 | markdown.py:250 |

**关键白盒发现（先于用例，影响范围）**：
- ⚠️ sqlite.py:372 引用 `AuditSeverity.MEDIUM`，但 **types.py 的 `AuditSeverity` 枚举只有 INFO/WARN/CRITICAL，无 MEDIUM 成员**。Python 参数在函数体执行前求值 → **任何 `_row_to_record` 调用（即 audit 全部读路径）立即抛 `AttributeError: MEDIUM`**。→ 见 Known-Gap KG-1。
- ⚠️ `_SCHEMA_SPANS` 仅有 run / session_start / trace / run_step 四个索引，**缺 `(span_type, start_time)` 复合索引**。→ 见 Known-Gap KG-2。

---

## 2. 白盒分析：分支结构与覆盖目标

### 2.1 `_safe_enum`（sqlite.py:153-169，3 个分支）

```
if not value:            → return fallback          （分支 B1：空值/None，无 warning）
try: enum_cls(value)     → return 枚举成员          （分支 B2：已知值）
except ValueError:       → logger.warning(...)      （分支 B3：未知值）
                          return fallback
```

> 注：B1 无 warning 是**设计意图**（空值常见，不刷日志）；不是缺陷。输入域限定 sqlite TEXT 列读出的 `str | None`——Enum 对不可哈希值抛 TypeError 不在现实输入域，不测。

### 2.2 `_row_to_record`（sqlite.py:368-387）

```
event_type = _safe_enum(AuditEventType, row["event_type"], fallback=None, label=...)   # D1
severity  = _safe_enum(AuditSeverity,  row["severity"],  fallback=AuditSeverity.MEDIUM) # D2 ⚠️KG-1
if event_type is None: return None                                                       # D3：跳过
return AuditRecord(ts=_iso_to_dt(row["ts"]) or datetime.now(utc), ...)                   # D4：坏 ts 兜底 now()
```

### 2.3 `_row_to_span`（sqlite.py:464-491）

```
span_type = _safe_enum(SpanType, row["span_type"], fallback=None)          # S1
if span_type is None: return None                                           # S2：跳过
attrs = json.loads(...)  except → {}                                        # S3：坏 JSON 兜底
status = _safe_enum(SpanStatus, row["status"], fallback=OK) or OK           # S4：未知/NULL → OK
return Span(start_time=_iso_to_dt(...) or now(utc), ...)                    # S5：坏 start_time 兜底
```

> 分支覆盖目标：`_safe_enum` 3 分支全达；`_row_to_record` D1-D4 全达；`_row_to_span` S1-S5 全达（默认 branch 覆盖）。

---

## 3. 不变式清单（inv）

| # | 不变式 | 类型 |
|---|--------|------|
| inv-1 | 读侧防御不抛异常：**任何** event_type/severity/span_type/status/ts/attributes 值组合下，`query`/`get_spans` 不抛异常（已知值、未知值、空值、坏 JSON、坏时间戳全输入域） | 全覆盖属性 |
| inv-2 | 未知 event_type / span_type 行被跳过：`_row_to_*` 返回 None，读侧不可见，**但库中行数不变（数据不删）** | 属性 |
| inv-3 | 可降级字段降级留痕：severity 未知 → MEDIUM、status 未知/NULL → OK，且**每次降级发一条 warning** | 属性 |
| inv-4 | 正常数据往返一致：write→read 逐字段相等（防御改动不误伤 happy path） | 属性 |
| inv-5 | `_safe_enum` 确定性：同输入 → 同输出（含 fallback 路径） | property 候选 |
| inv-6 | 过滤后返回值无 None 元素：`query`/`get_spans` 返回的列表里不含 None、不含坏行 | 属性 |

---

## 4. 风险清单（RISK 打分排序）

| # | 风险 | 严重度 | 可能性 | 优先级 |
|---|------|:--:|:--:|:--:|
| R1 | 未知枚举行导致读侧抛 ValueError 崩溃（改动前行为） | 高 | 中 | **P0** |
| R2 | 可降级字段（severity/status）降级无留痕，坏数据静默被"洗白" | 中 | 中 | **P0** |
| R3 | 全坏数据下 `query`/`get_spans` 不返回 `[]`（抛异常 / 返回含 None） | 高 | 低 | **P0** |
| R4 | 防御改动破坏正常数据往返（过滤误伤 / 字段错位） | 高 | 中 | **P0** |
| R5 | `_safe_enum` 空值/None 输入路径行为漂移（崩溃 / 误报 warning） | 中 | 中 | P1 |
| R6 | 混合好坏数据：坏行被跳过但**数据仍在库**（append-only 不被破坏）；过滤/排序仍正确 | 中 | 中 | P1 |
| R7 | warning 日志内容不可观测（缺枚举名 / 缺值 / 缺 label） | 低 | 中 | P1 |
| R8 | 复合索引缺失/失效 → 按 (event_type,ts) / (span_type,start_time) 查询退化为全表扫描 | 低 | 高 | **P2** |
| R9 | console `span_type.name.lower()` 渲染漂移；与 markdown（按 value 渲染）口径分叉 | 低 | 低 | **P2** |
| R10 | markdown 渲染格式回归漂移（表头 / 行结构 / 状态三态 / icon） | 低 | 低 | **P2** |

**非功能范围声明**：性能（索引命中率压测）、并发写放大、DB 文件损坏/磁盘故障等**不在本设计范围**——本改动是"数据内容防御"，非基础设施故障防御；故障注入维度对纯读转换层不适用（见 §6 豁免）。

---

## 5. Oracle 策略

| 用例域 | Oracle 类型 | 依据 |
|--------|------------|------|
| `_safe_enum` / `_row_to_*` 已知值、未知值、空值 | golden value | 规格明确：未知→fallback、空→fallback、已知→成员，可人工推导 |
| 正常往返（write→query） | roundtrip 对拍 | 写入对象字段作为 oracle（reference） |
| 坏 ts / 坏 start_time 兜底 `now()` | 蜕变关系 | 输出值不确定 → 断言 `isinstance(datetime)` 且非 None，**禁硬编码抄值** |
| console / markdown 渲染 | golden value | 格式字符串可人工推导（`[llm_call]`、`| 🚀 run |` 等） |
| warning 留痕 | 副作用断言 | caplog 捕获 logger 记录，断言消息含枚举名/值/label |
| 索引存在 | golden value | sqlite_master 查询，索引名 + 列组合为规格 |

---

## 6. Mock / Fake 策略与豁免

- **零 mock**：全部用例。`_safe_enum` / `_row_to_*` 为纯转换（unit）；sqlite 用真实 sqlite 引擎（tmp_path 文件 = integration）；console 用 capsys 捕获真实 stderr；warning 用 caplog 捕获真实日志（均为 pytest 内置捕获，非 mock）。
- **豁免声明**：
  - 副作用验证：`_safe_enum`/`_row_to_*` 纯函数无状态副作用，仅验证返回值 + warning 日志（日志是唯一副作用，已纳入断言）。
  - 回滚/清理：读侧无写入事务，不适用；"数据仍在库"（inv-2）以 COUNT 断言替代。
  - 故障注入：无外部 I/O 依赖（网络/磁盘满/DB 断开），不适用——已由"坏数据输入"用例覆盖等价风险面。
  - 状态机：读侧为纯转换+查询，无状态机，不适用。
  - 并发/时序：本改动无 check-then-act/竞态逻辑，既有 `test_concurrent_writes_no_loss` 已覆盖写并发，不新增。
  - 多参数 pairwise：query 的 agent_id/event_type/时间窗/limit 过滤为既有功能（`test_audit_query_filters_and_limit` 已测），与防御正交，本设计不重复，仅验证"过滤与防御共存"。

---

## 7. 汇总：用例 × 风险/不变式覆盖矩阵

| 用例 | 层级 | inv-1 不抛 | inv-2 跳过留数据 | inv-3 降级留痕 | inv-4 往返一致 | inv-5 确定性 | inv-6 无 None | R1 | R2 | R3 | R4 | R5 | R6 | R7 | R8 | R9 | R10 |
|------|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| 1. `_safe_enum` 已知值 | unit | ✅ | | | | ✅ | | | | | | | | | | | |
| 2. `_safe_enum` 未知值+warning | unit | | | | | ✅ | | ✅ | | | | | | ✅ | | | |
| 3. `_safe_enum` 空值/None | unit | ✅ | | | | ✅ | | | | | | ✅ | | | | | |
| 4. `_safe_enum` fallback=None | unit | | | | | ✅ | | ✅ | | | | | | | | | |
| 5. `_row_to_record` 正常 | unit | | | | ✅ | | | | | | ✅ | | | | | | |
| 6. `_row_to_record` 未知 event_type | unit | ✅ | ✅ | | | | ✅ | ✅ | | | | | | ✅ | | | |
| 7. `_row_to_record` 未知 severity + 坏 ts | unit | ✅ | | ✅ | | | | | ✅ | | | | | ✅ | | | |
| 8. `query` 全坏 → `[]` 数据在库 | integration | ✅ | ✅ | | | | ✅ | ✅ | | ✅ | | | ✅ | | | | |
| 9. `query` 混合：过滤+降级+排序 | integration | ✅ | ✅ | ✅ | | | ✅ | | ✅ | | | | ✅ | ✅ | | | |
| 10. `query` 正常往返（3 组参数化） | integration | | | | ✅ | | | | | | ✅ | | | | | | |
| 11. `_row_to_span` 未知 span_type | unit | ✅ | ✅ | | | | ✅ | ✅ | | | | | | ✅ | | | |
| 12. `_row_to_span` 未知 status + 坏 attrs | unit | ✅ | | ✅ | | | | | ✅ | | | | | ✅ | | | |
| 13. `get_spans` 混合 + run 过滤 + 排序 | integration | ✅ | ✅ | ✅ | | | ✅ | | ✅ | | | | ✅ | | | | |
| 14. `get_spans` 全坏 + status NULL | integration | ✅ | ✅ | ✅ | | | ✅ | ✅ | | ✅ | | ✅ | | | | | |
| 15. audit 复合索引存在 | integration | | | | | | | | | | | | | | ✅ | | |
| 16. spans 复合索引存在 **[KG-2]** | integration | | | | | | | | | | | | | | ✅ | | |
| 17. console `[name.lower()]` 格式 | unit | | | | | | | | | | | | | | | ✅ | |
| 18. console 全 SpanType + name/value 一致 | unit | | | | | | | | | | | | | | | ✅ | |
| 19. markdown 表格格式回归 | integration | | | | | | | | | | | | | | | | ✅ |
| 20. markdown status 三态 + icon 回归 | integration | | | | | | | | | | | | | | | | ✅ |

**用例总数**：20 条设计用例（18、20 参数化，下游展开为更多测试函数）。

---

## 8. 用例详情

### 用例1：`_safe_enum` 已知值返回对应枚举成员

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 确定性 [property] + R1 已知路径不误伤 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: B2（try 成功） |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：value ∈ {已知枚举 value 的代表值} → 代表值 = `"warn"`（AuditSeverity）、`"llm_call"`（SpanType）

**Given**：无前置，纯函数直接调用
**When**：`_safe_enum(AuditSeverity, "warn", fallback=AuditSeverity.INFO, label="AuditSeverity")`
**Then**：
- 返回值：`is AuditSeverity.WARN`（身份断言，非相等断言）
- 副作用：无（caplog 无新增 warning 记录）

---

### 用例2：`_safe_enum` 未知值 → fallback + warning 留痕（含 label 回落）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 确定性 + R1 未知值不抛 [P0] + R7 warning 可观测 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: B3（except ValueError） |
| Oracle | golden value + 副作用断言 |
| Mock | 否 |

**等价类划分**：value ∈ {不在枚举中的任意字符串} → 代表值 = `"v99_alien"`

**Given**：`caplog.set_level(logging.WARNING, logger="pandaren.observability.backend.sqlite")`
**When**：两次调用
- `_safe_enum(SpanStatus, "v99_alien", fallback=SpanStatus.OK, label="SpanStatus")`
- `_safe_enum(SpanStatus, "v99_alien", fallback=SpanStatus.OK)`（label 缺省）
**Then**：
- 返回值：两次均 `is SpanStatus.OK`
- 副作用（warning 留痕，两条）：
  - 消息含 `"unknown SpanStatus value 'v99_alien'"`（枚举名 + repr 值）
  - 显式 label 调用消息含 `label=SpanStatus`
  - label 缺省调用消息回落为 `label=SpanStatus`（`label or enum_cls.__name__`）

---

### 用例3：`_safe_enum` 空值/None → fallback 且**无** warning

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 不抛 + inv-5 + R5 空值路径 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: B1（`if not value`） |
| Oracle | golden value + 副作用断言 |
| Mock | 否 |

**等价类划分**：value ∈ {None, 空串 `""`, `0`}（B1 的 `not value` 真集）→ 代表值 = `""`、`None`；边界对照 = 已知值 `"ok"`

**Given**：同上 caplog 设置
**When**：
- `_safe_enum(SpanStatus, "", fallback=SpanStatus.OK, label="SpanStatus")`
- `_safe_enum(SpanStatus, None, fallback=SpanStatus.OK, label="SpanStatus")`
**Then**：
- 返回值：均 `is SpanStatus.OK`
- 副作用：caplog 中**无** `"unknown SpanStatus"` 记录（B1 静默回落是设计意图，不是漏测）

---

### 用例4：`_safe_enum` fallback=None 时未知值返回 None（skip 语义）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 + R1 [P0]（调用方"跳过该行"契约的根基） |
| 测试层级 | unit |
| 覆盖准则 | branch: B3 + fallback=None 组合 |
| Oracle | golden value |
| Mock | 否 |

**When**：`_safe_enum(SpanType, "v99_alien", fallback=None, label="SpanType")`
**Then**：
- 返回值：`is None`
- 副作用：warning 消息含 `"unknown SpanType value 'v99_alien'"`（降级路径也留痕）

---

### 用例5：`_row_to_record` 已知行 → 逐字段 AuditRecord

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 往返一致 [P0] + R4 不误伤 [P0] |
| 测试层级 | unit（静态方法；Row 为值对象，用内存连接构造夹具） |
| 覆盖准则 | branch: D1+D2 已知值、D4 正常 ts |
| Oracle | golden value（每字段可人工推导） |
| Mock | 否 |
| known-gap | **[KG-1]**：`fallback=AuditSeverity.MEDIUM` 参数求值即抛 `AttributeError` → 当前 xfail |

**等价类划分**：Row 字段按完整值域的代表组合 → 代表值见 Given

**Given**：内存连接构造 `sqlite3.Row`：
- `event_type="run_started"`、`severity="warn"`、`ts="2026-01-01T12:00:00+00:00"`、`record_id="rec-1"`、`agent_id="pandapal"`、`run_id="run-1"`、`session_id="s1"`、`step_n=2`、`tool_name="calc"`、`terminal_reason="none"`、`detail="hello 你好"`

**When**：`SQLiteAuditBackend._row_to_record(row)`
**Then**：
- 返回值非 None；`event_type is AuditEventType.RUN_STARTED`；`severity is AuditSeverity.WARN`
- `timestamp == datetime(2026,1,1,12,0,0, tzinfo=timezone.utc)`（ISO 精确解析）
- `record_id=="rec-1"`、`agent_id=="pandapal"`、`run_id=="run-1"`、`session_id=="s1"`、`step_n==2`、`tool_name=="calc"`、`terminal_reason=="none"`、`detail=="hello 你好"`
- 副作用：无 warning 记录

---

### 用例6：`_row_to_record` 未知 event_type → None + warning

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 + inv-2 跳过语义 [P0] + inv-6 + R1 + R7 |
| 测试层级 | unit |
| 覆盖准则 | branch: D1 → D3（`event_type is None → return None`） |
| Oracle | golden value + 副作用断言 |
| Mock | 否 |
| known-gap | **[KG-1]**：同上，当前 xfail |

**等价类划分**：event_type ∈ {未知字符串}；severity 用已知值隔离变量 → 代表值 = `event_type="v99_event"`、`severity="info"`

**Given**：caplog 设置；构造 Row（`event_type="v99_event"`，其余字段合法）
**When**：`_row_to_record(row)`
**Then**：
- 返回值：`is None`
- 副作用：warning 含 `"unknown AuditEventType value 'v99_event'"` 与 `label=AuditEventType`
- 注意：severity 合法（"info"）不该被触达——若实现先降级后跳过，本用例同时锁定"跳过优先级高于降级"

---

### 用例7：`_row_to_record` 未知 severity → MEDIUM 降级 + 坏 ts 兜底

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 + inv-3 降级留痕 [P0] + R2 + R7 + 坏 ts 蜕变 |
| 测试层级 | unit |
| 覆盖准则 | branch: D2 未知值、D4 坏 ts（`or datetime.now(utc)`） |
| Oracle | golden value（severity）+ 蜕变关系（ts 兜底，输出不确定 → 断言类型） |
| Mock | 否 |
| known-gap | **[KG-1]**：当前 xfail（修复前连函数体都进不去） |

**等价类划分**：severity ∈ {未知字符串}；ts ∈ {不可解析字符串} → 代表值 = `severity="bogus"`、`ts="not-a-date"`

**Given**：构造 Row（`event_type="run_started"` 已知、`severity="bogus"`、`ts="not-a-date"`）
**When**：`_row_to_record(row)`
**Then**：
- 返回值：非 None（不丢行——审计行 HC4 强制保留）
- `severity is AuditSeverity.MEDIUM`（降级，**期望值来自规格**，非实现抄录）
- `event_type is AuditEventType.RUN_STARTED`（未知 severity 不影响已知字段）
- `timestamp`：`isinstance(datetime)` 且非 None（**蜕变断言**：兜底 `now()` 值不确定，禁止硬编码时间）
- 副作用：warning 含 `"unknown AuditSeverity value 'bogus'"`

---

### 用例8：`query` 全坏数据 → 返回 `[]` 且数据仍在库

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 + inv-2 + inv-6 [P0] + R1 + R3 + R6 |
| 测试层级 | integration（真实 sqlite 文件，tmp_path） |
| 覆盖准则 | branch: D3 全行跳过路径 + query 过滤 None 组合 |
| Oracle | golden value |
| Mock | 否 — 真库 |
| known-gap | **[KG-1]**：当前 xfail |

**等价类划分**：行集合 ∈ {全部为未知 event_type} → 代表值 = 2 行 `event_type="v99"`

**Given**：
- `be = SQLiteAuditBackend(db_path=str(tmp_path/"obs.db"))`
- 直接经 `be._conn` 手工 INSERT 2 行坏数据（`event_type="v99"`、`severity="info"`、其余列合法占位）——绕过 write（write 只能写合法枚举），模拟版本演进/外部坏数据
**When**：`be.query()`
**Then**：
- 返回值：`== []`（不抛异常、不含 None 元素）
- 副作用（inv-2 数据仍在库）：`SELECT COUNT(*) FROM audit_records` 仍 `== 2`（读侧跳过 ≠ 删除，append-only 不被破坏）
- 副作用：caplog 有 2 条 `"unknown AuditEventType value 'v99'"`

---

### 用例9：`query` 混合好坏数据 → 过滤 + 降级 + 排序共存

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 + inv-3 + inv-6 [P1] + R2 + R6 + R7 |
| 测试层级 | integration |
| 覆盖准则 | branch: D3 跳过 + D2 降级 + 查询 SQL 顺序不变 |
| Oracle | golden value |
| Mock | 否 |
| known-gap | **[KG-1]**：当前 xfail |

**Given**：INSERT 3 行（绕过 write）：
- `t=2026-01-01T12:00:00+00:00`、`event_type="run_started"`、`severity="warn"`（好行）
- `t=2026-01-01T12:00:02+00:00`、`event_type="v99"`（坏 event，应跳过）
- `t=2026-01-01T12:00:01+00:00`、`event_type="run_finished"`、`severity="bogus"`（坏 severity，应降级）

**When**：`be.query()`（默认 ORDER BY ts DESC, id DESC）
**Then**：
- 返回值长度 `== 2`；顺序为 `["run_started"(t=12:00:02), "run_finished"(t=12:00:01)]`（坏行 t=12:00:02 已被跳过，排序仍正确）
- `run_started` 行：`severity is AuditSeverity.WARN`（好数据不受降级污染）
- `run_finished` 行：`severity is AuditSeverity.MEDIUM`（降级留痕）
- 副作用：caplog 各 1 条 `"unknown AuditEventType value 'v99'"` 与 `"unknown AuditSeverity value 'bogus'"`

---

### 用例10：`query` 正常往返一致（防御改动不回归，3 组参数化）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 [P0] + R4 |
| 测试层级 | integration |
| 覆盖准则 | N/A（happy path 守护） |
| Oracle | roundtrip 对拍（写入对象为 oracle） |
| Mock | 否 |
| known-gap | **[KG-1]**：当前 xfail |

**等价类划分**：正常数据按 (event_type × severity) 代表组合 → 3 组 = `(RUN_STARTED, INFO)`、`(TOOL_EXECUTED, WARN)`、`(PERMISSION_DENIED, CRITICAL)`

**Given**：`be = SQLiteAuditBackend(db_path=tmp_path)`；`_mk_audit` 构造 3 条合法记录（复用既有基座 helper）
**When**：依次 `be.write(rec)` → `be.query()`
**Then**（对每组，逐字段对拍）：
- `len(query()) == 3`；`query()` 内无 None
- 每条的 `record_id / event_type / severity / detail / session_id / step_n / tool_name / terminal_reason` 与写入对象**逐一相等**（含 `is` 身份断言枚举成员）

---

### 用例11：`_row_to_span` 未知 span_type → None + warning

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 + inv-2 + inv-6 [P0] + R1 + R7 |
| 测试层级 | unit |
| 覆盖准则 | branch: S1 → S2（`span_type is None → return None`） |
| Oracle | golden value + 副作用断言 |
| Mock | 否 |

**等价类划分**：span_type ∈ {未知字符串}；status 用已知值隔离 → 代表值 = `span_type="v99_span"`、`status="ok"`

**Given**：构造 Row（`span_type="v99_span"`、`status="ok"`、`attributes_json='{"a":1}'`、其余列合法）
**When**：`SQLiteTracerBackend._row_to_span(row)`
**Then**：
- 返回值：`is None`
- 副作用：warning 含 `"unknown SpanType value 'v99_span'"` 与 `label=SpanType`

---

### 用例12：`_row_to_span` 未知 status → OK 降级 + 坏 attributes_json → `{}`

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 + inv-3 [P0] + R2 + R7 + S3 坏 JSON 兜底 |
| 测试层级 | unit |
| 覆盖准则 | branch: S1 已知 + S3 坏 JSON + S4 未知 status |
| Oracle | golden value + 副作用断言 |
| Mock | 否 |

**等价类划分**：status ∈ {未知字符串}；attributes_json ∈ {非 JSON 文本} → 代表值 = `status="v99_status"`、`attributes_json="{broken json"`

**Given**：构造 Row（`span_type="llm_call"` 已知、`status="v99_status"`、`attributes_json="{broken json"`、`start_time="2026-01-01T12:00:00+00:00"`、`end_time` 同上、`duration_ms=1.5`、其余列合法）
**When**：`_row_to_span(row)`
**Then**：
- 返回值非 None；`span_type is SpanType.LLM_CALL`；`status is SpanStatus.OK`（降级）
- `attributes == {}`（坏 JSON 兜底，不抛）
- `start_time == datetime(2026,1,1,12,0,0, tzinfo=utc)`（S5 已知 ts 精确解析）
- 副作用：warning 含 `"unknown SpanStatus value 'v99_status'"`（坏 JSON 不产生 warning——**实现如此，非缺陷**）

---

### 用例13：`get_spans` 混合好坏 + run 过滤 + start_time 排序

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 + inv-3 + inv-6 [P1] + R2 + R6 |
| 测试层级 | integration |
| 覆盖准则 | branch: S2 跳过 + S4 降级 + ORDER BY start_time ASC 不变 |
| Oracle | golden value |
| Mock | 否 |

**Given**：`be = SQLiteTracerBackend(db_path=tmp_path)`；INSERT 4 行（绕过 export_span）：
- run1：`start=2026-01-01T12:00:01+00:00`、`span_type="llm_call"`、`status="error"`（好行）
- run1：`start=2026-01-01T12:00:00+00:00`、`span_type="tool_call"`、`status="v99_status"`（坏 status → OK）
- run1：`start=2026-01-01T12:00:02+00:00`、`span_type="v99_span"`（坏 type → 跳过）
- run2：`start=2026-01-01T12:00:00+00:00`、`span_type="run"`、`status="ok"`（对照组）

**When**：`be.get_spans("run1")` 与 `be.get_spans()`
**Then**：
- `get_spans("run1")`：长度 `== 2`；顺序 `[tool_call(12:00:00), llm_call(12:00:01)]`（ASC）；`tool_call` 行 `status is SpanStatus.OK`；`llm_call` 行 `status is SpanStatus.ERROR`
- `get_spans()`：长度 `== 3`（含 run2，坏 type 行不可见）；无 None 元素
- 副作用：caplog 有 `"unknown SpanType value 'v99_span'"` 与 `"unknown SpanStatus value 'v99_status'"`

---

### 用例14：`get_spans` 全坏 → `[]` + status=NULL → OK 且无 warning

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 + inv-2 + inv-3 [P0/P1] + R3 + R5 |
| 测试层级 | integration |
| 覆盖准则 | branch: S2 全跳过 + S4 空值路径（status 列可 NULL，schema 未标 NOT NULL） |
| Oracle | golden value + 副作用断言 |
| Mock | 否 |

**Given**：INSERT 3 行：
- 2 行 `span_type="v99_span"`（全坏）
- 1 行 `span_type="run"`、`status=NULL`、`start_time=NULL`、其余列合法

**When**：`be.get_spans()`
**Then**：
- 全坏场景：前 2 行被跳过 → 返回值长度 `== 1`（只含 status=NULL 行）；`SELECT COUNT(*) FROM spans` 仍 `== 3`（数据在库）
- status=NULL 行：`status is SpanStatus.OK`（`_safe_enum(None)` → B1 空值路径）
- `start_time`：`isinstance(datetime)` 且非 None（S5 兜底蜕变断言，禁硬编码）
- 副作用：**无** `"unknown SpanStatus"` warning（NULL 走 B1 静默路径，不是 B3）；但有 `"unknown SpanType value 'v99_span'"` ×2

---

### 用例15：audit 复合索引 `idx_audit_event_ts` 存在

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R8 索引存在 [P2] |
| 测试层级 | integration |
| 覆盖准则 | N/A（schema 声明） |
| Oracle | golden value（sqlite_master 查询） |
| Mock | 否 |

**Given**：`be = SQLiteAuditBackend(db_path=tmp_path)`（init 已建表建索引）
**When**：`SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='audit_records'`
**Then**：
- 索引名集合含 `idx_audit_event_ts`
- 其 `sql` 含 `event_type` 与 `ts` 列（复合列序为 (event_type, ts)）
- 附：重复 `SQLiteAuditBackend(db_path=same)`（幂等重建）不抛异常

---

### 用例16：spans 复合索引 `(span_type, start_time)` 存在

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R8 [P2]（按 span_type 过滤 + start_time 排序的查询路径） |
| 测试层级 | integration |
| 覆盖准则 | N/A（schema 声明） |
| Oracle | golden value |
| Mock | 否 |
| known-gap | **[KG-2]**：当前 `_SCHEMA_SPANS` 无此索引 → 当前 xfail |

**Given**：`be = SQLiteTracerBackend(db_path=tmp_path)`
**When**：`SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='spans'`
**Then**：
- 存在一个索引，其 `sql` 按序含 `span_type` 与 `start_time` 两列
- 期望索引名 `idx_spans_span_type_start`（**命名推断，请确认**；断言以"列组合"为主，索引名可放宽）

---

### 用例17：console `export_span` 渲染 `[span_type.name.lower()]`

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 渲染格式 [P2] |
| 测试层级 | unit（独立类、无协作对象；真实 stderr 经 capsys 捕获） |
| 覆盖准则 | branch: `span_type == LLM_CALL` 的 else 分支（缩进 4 空格） |
| Oracle | golden value（格式串可人工推导） |
| Mock | 否 |

**Given**：`Span(span_id="s1", trace_id="t", parent_span_id=None, span_type=SpanType.LLM_CALL, name="llm:gpt-4o", agent_id="a", run_id="run-1", session_id="", step_n=2, start_time/end_time=固定 UTC 时间, duration_ms=1.5, status=SpanStatus.OK, attributes={"model":"gpt-4o"})`
**When**：`ConsoleTracerBackend().export_span(span)` → `capsys.readouterr().err`
**Then**（子串断言，ANSI 前缀不参与精确匹配）：
- 含 `[llm_call]`（`LLM_CALL.name.lower()`，**改动点**）
- 含 `llm:gpt-4o`（name）、`OK`（status）、`1.5ms`（duration）、`step=2`、`model=gpt-4o`（attrs）

---

### 用例18：console 全 SpanType 渲染 `name.lower()` + name/value 口径守护（参数化 8 成员）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 [P2]（8 个类型全不漂移 + console 与 markdown 口径不分叉） |
| 测试层级 | unit |
| 覆盖准则 | 全 SpanType 成员遍历 |
| Oracle | golden value + 属性守护断言 |
| Mock | 否 |

**Given**：`parametrize` 8 个 `SpanType` 成员
**When**：对每个成员 `export_span(Span(span_type=member, name="n", ...))` → 捕获 stderr
**Then**：
- stderr 含 `[{member.name.lower()}]`
- 守护断言：`member.name.lower() == member.value`（当前枚举下 console 用 name.lower()、markdown 用 value 渲染同一文本；若未来 name/value 分叉，此断言报警，阻止口径静默分裂）

---

### 用例19：markdown 渲染表格格式回归

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R10 渲染不漂移 [P2] |
| 测试层级 | integration（真实文件系统，tmp_path） |
| 覆盖准则 | N/A（渲染格式回归守护） |
| Oracle | golden value（`_HEADER` + line 模板可人工推导） |
| Mock | 否 |

**Given**：
- `MarkdownTracerBackend(tmp_path)`；`end_time = datetime(2026,1,1,12,0,0, tzinfo=timezone.utc)`（固定时钟，防 flaky）
- `Span(span_type=SpanType.RUN, name="agent run", session_id="s1", run_id="run1234abcd", end_time=上述值, status=SpanStatus.OK, duration_ms=500, step_n=None, attributes={})`

**When**：`be.export_span(span)` → 读 `tmp_path/sessions/s1/traces.md`
**Then**：
- 文件含表头 `| 时间 | 类型 | 名称 | 状态 | 结束原因 | 耗时(ms) | Step | Run | 属性 |`
- 文件含数据行 `| 12:00:00 | 🚀 run | \`agent run\` | ✅ ok |  | **500** |  | \`run1234\` |  |`（逐列 golden）
- 无多余表头重复（`_headered` 幂等——再 export 一次仍只有一行表头）

---

### 用例20：markdown status 三态 + span_type icon 渲染（参数化）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R10 [P2]（状态三态与类型 icon 是渲染核心，历史上有漏渲染口径问题） |
| 测试层级 | integration |
| 覆盖准则 | branch: markdown export_span 的 status 三分支（ok / cancelled / else） |
| Oracle | golden value |
| Mock | 否 |

**Given**：`parametrize` 3 组 (status → 期望文本) + 2 组 span_type icon：
- `(SpanStatus.OK → "✅ ok")`、`(SpanStatus.CANCELLED → "⏸️ cancelled")`、`(SpanStatus.ERROR → "❌ error")`
- icon 抽样：`SpanType.STEP → "📍 step"`、`SpanType.LLM_CALL → "🤖 llm_call"`

**When**：每组 export_span（session_id="" 走 `_no_session/traces.md`）→ 读文件
**Then**：
- 数据行第 4 列 == 对应期望状态文本（如 `| ✅ ok |`）
- 数据行第 2 列 == 对应 icon（`_SPAN_TYPE_ICON` 表，按 `span.span_type.value` 查——**与 console 的 name.lower() 口径一致由用例 18 守护**）

---

## 9. Known-Gap 清单

> 设计基于任务规格（期望行为）；以下为当前实现与期望的差异。下游 test-coder 对受影响用例落 `pytest.xfail(reason=...)`——修复后测试"意外通过"即报警，差距不默默消失。

| # | 影响用例 | 期望行为（规格） | 当前实现现状 | 差距原因 | 修复方向 |
|---|---------|-----------------|-------------|---------|---------|
| KG-1 **[P0]** | 5, 6, 7, 8, 9, 10 | severity 未知 → 降级 `AuditSeverity.MEDIUM` + warning | `types.AuditSeverity` **只有 INFO/WARN/CRITICAL**，`sqlite.py:372` 的 `fallback=AuditSeverity.MEDIUM` 在**任何** `_row_to_record` 调用时参数求值即抛 `AttributeError` → **audit 全部读路径当前不可用**（连正常行都炸） | 枚举缺 MEDIUM 成员（或实现引用了不存在的成员） | ① 给 `AuditSeverity` 补 `MEDIUM = "medium"`；或 ② 改用现有成员（如 WARN）——**须与规格确认** |
| KG-2 **[P2]** | 16 | `spans(span_type, start_time)` 复合索引存在 | `_SCHEMA_SPANS` 仅有 idx_spans_run / session_start / trace / run_step，**无 (span_type, start_time) 索引** | 建表脚本漏加 | `CREATE INDEX IF NOT EXISTS idx_spans_span_type_start ON spans(span_type, start_time)` |
| KG-3 **[说明，不设 xfail]** | 15 | audit 索引声明为 `(event_type, ts DESC)` | `idx_audit_event_ts ON audit_records(event_type, ts)` **无 DESC 声明** | 声明方向差异 | SQLite 索引可反向扫描，`ORDER BY ts DESC` 不受影响——**行为无差异，仅当任务方要求严格匹配 sqlite_master.sql 文本时才需对齐** |

---

## 10. 推断标注（请确认）

1. **spans 新索引命名** `idx_spans_span_type_start`（用例 16）——按既有 `idx_spans_*` 命名模式推断；断言以列组合为主，索引名可放宽。
2. **AuditSeverity 降级目标值** MEDIUM 以任务规格为准；若修复走"补枚举成员"，`MEDIUM = "medium"` 的字符串值为推断（`types.py` 现值为小写单词风格），请与规格确认。
3. **console 输出断言用子串匹配**（ANSI 转义序列存在，不参与精确匹配）——下游实现时注意先 `strip()` 或子串断言。

---

## 11. 交付给 test-coder 的要点

- 文件建议：`pandaren/observability/tests/test_sqlite_read_defense.py`（基座 helper `_mk_audit`/`_mk_span` 可自 tests/test_sqlite_backend.py 复制或 import）。
- 坏数据注入统一走**手工 `INSERT`**（write/export_span 只能写合法枚举，模拟版本演进/外部坏数据必须绕过后端写入口）。
- 全用例零 mock；层级与 Oracle 见各用例属性表；KG-1/KG-2 用例落 `xfail`，其余按常规断言。
