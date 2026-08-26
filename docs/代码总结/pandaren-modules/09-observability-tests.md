# 测试设计：observability 近期 5 项修复（回归 + 防回归）

**被测对象**：`pandaren/observability/` 近期修复的 5 个问题点（顶层导出面 / InMemoryTracerBackend 线程安全 / Logger 降级留痕 / Markdown 后端 surrogate 防御 / AuditLog HC4 传播语义）
**测试框架**：pytest（`tmp_path` / `caplog` / `capsys`），Python 3.12+
**设计日期**：2026（白盒分析基于当前工作区源码 + 行为探针实测）
**测试文件建议**：`pandaren/tests/test_observability_fixes.py`（`pyproject.toml` testpaths 已含 `pandaren`；import 风格对齐 `pandaren/tests/test_cancellation.py`：`from pandaren.observability import ...`）

---

## 1. 前置信息（已确认）

| 项 | 值 | 来源 |
|----|----|------|
| 修复 1 导出面 | `observability/__init__.py:32,34` 已补 `InMemoryLoggerBackend`/`MarkdownLoggerBackend`；16 个后端名与 `backend/__init__.py` 一致 | 实测通过 |
| 修复 2 线程安全 | `in_memory.py:59` `InMemoryTracerBackend._lock`；export_span/get_spans/clear/query_spans 全部 `with self._lock` | 实测并发 0 异常 |
| 修复 3 留痕 | `logger.py:66` `logger.debug("observability logger write failed", exc_info=True)`；模块级标准库 logger，非 Facade 自身 | 实测 caplog 命中 |
| 修复 4 surrogate 防御 | `markdown.py:155/290/497` 单行 `line.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")`；`audit.py:24-37` facade 级 `_sanitize_surrogates` 有 try/except + `errors='replace'` 兜底 | ⚠️ 见 KG-1 |
| 修复 5 HC4 | `audit.py:146-155` write+flush 原子 → 失败 `_write_fallback`（stderr AUDIT_FALLBACK JSON）→ `raise AuditWriteError from e`；`backend=None` → ValueError | 实测通过 |
| 关键数据类 | `AuditRecord`/`Span` frozen dataclass；`AuditEventType` 21 值；`LogLevel` IntEnum（DEBUG=10/INFO=20/WARN=30/ERROR=40） | types.py |
| 查询过滤实现 | `InMemoryAuditBackend.query`（in_memory.py:29-46）：agent 精确、event_type 按 `r.event_type.value == event_type` 字符串匹配、时间窗闭区间、`sort(reverse=True)` 倒序、limit 截断 | 实测通过 |

**关键白盒发现（先于用例，影响范围）**：

- ⚠️ **KG-1（surrogate 全范围未防御）**：`'\ud800'.encode('utf-8', errors='surrogateescape')` 会抛 `UnicodeEncodeError`——surrogateescape 编码侧只处理 `U+DC80–U+DCFF`（PEP 383 范围）。markdown.py 三后端（audit/tracer/logger）的单行防御对 `U+D800–U+DBFF`（高代理）、`U+DC00–U+DC7F`、`U+DD00–U+DFFF`（低代理其余）以及 surrogate pair 仍抛错、丢整条记录。实测：三个后端对 `'\ud800'`/`'\udfff'` 全部 RAISE。仅 audit.py 的 facade 级 `_sanitize_surrogates` 能处理全范围（`errors='replace'` → `'?'`）。→ 见 Known-Gap KG-1，相关用例标 `[known-gap]` + xfail。

---

## 2. 白盒分析：分支结构与覆盖目标

| 修复点 | 分支/路径 | 覆盖目标 |
|--------|----------|---------|
| `Logger._write`（logger.py:59-66） | B1 `if not self._should_log: return`；B2 try 成功；B3 except（debug 留痕） | B1/B2/B3 全达 |
| `AuditLog.write_sync`（audit.py:146-155） | B1 try 成功（write+flush）；B2 except（fallback + raise） | B1/B2 全达；B2 内 write 抛与 flush 抛两个故障点分别注入 |
| `MarkdownTracerBackend.export_span`（markdown.py:262-292） | B1 `if span.attributes:`（渲染 attrs）；B2 空 attributes | B1/B2 全达（B2 由空 attrs 的 span 覆盖） |
| `AuditLog.__init__`（audit.py:98-103） | B1 `if backend is None: raise ValueError` | 达 |
| `InMemoryTracerBackend` 三操作 | 无分支，纯顺序/裁剪逻辑 | 语句覆盖 + 并发属性 |

其余（导出面、query 过滤、DualAuditBackend）为直线逻辑或组合过滤，按等价类覆盖。

---

## 3. 不变式清单（inv）

| # | 不变式 | 类型 |
|---|--------|------|
| inv-1 | 顶层 `observability.__all__` 的 backend 子集 = `backend.__all__`（16 个类名，四组 × 四后端） | 结构性 |
| inv-2 | 每个后端名可顶层 import，且与 `backend` 模块中的类身份一致（`is`） | 结构性 |
| inv-3 | `InMemoryTracerBackend` 并发 export/clear/get_spans 不抛异常（尤其迭代中 clear 不 RuntimeError） | 并发属性 |
| inv-4 | 任何时刻 `get_spans()` 返回的 span 集合 ⊆ 已 export 集合（无串数据/无凭空数据） | 并发属性 |
| inv-5 | span 数 ≤ max_spans，超限丢弃最旧、保留最新（裁剪语义） | 确定性 |
| inv-6 | Logger 后端写失败不向调用方传播异常（非 HC4 Fail-Safe 保持） | 属性 |
| inv-7 | Logger 写失败必留痕：`logging.getLogger("pandaren.observability.logger").debug(..., exc_info=True)`，消息 = `"observability logger write failed"` | 副作用属性 |
| inv-8 | markdown.py 三个事件流后端（audit/tracer/logger）对 surrogate 输入（U+D800–U+DFFF）成功落盘：字符被替换（U+FFFD 或 `?`）而非抛错，文件可 utf-8 正常读取 | **全覆盖属性**（当前仅 DC80–DCFF 满足，见 KG-1） |
| inv-9 | 落盘防御不改内存镜像：`get_spans()` 返回的 span 保留原始 surrogate 值（防御只作用于落盘行） | 属性 |
| inv-10 | `write_sync` 成功 = write + flush 原子完成（两者都被调用，且 lock 内） | 属性 |
| inv-11 | write 或 flush 任一失败 → stderr 打印 AUDIT_FALLBACK JSON（含 original_error）→ `raise AuditWriteError`（cause 链指向原始异常） | 属性 |
| inv-12 | `AuditWriteError` 不是 `ObservabilityError` 子类（防上层 `except ObservabilityError` 意外吞掉） | 结构性 |
| inv-13 | `DualAuditBackend.write` 使 primary 与 secondary 都收到同一条记录；`query` 委托 primary | 属性 |
| inv-14 | `query_records` 过滤语义：agent_id 精确、event_type 按 value 字符串精确、时间窗闭区间、结果时间倒序、limit 截断、无匹配返回 `[]` | 属性 |
| inv-15 | `AuditLog(backend=None)` → ValueError（HC4 不可关闭） | 结构性 |

---

## 4. 风险清单（RISK 打分排序）

| # | 风险 | 严重度 S | 可能性 L | 优先级 |
|---|------|:--:|:--:|:--:|
| R1 | 顶层导出面回退：`from pandaren.observability import MarkdownLoggerBackend` 再抛 ImportError（此前真实事故） | 高（import 即崩，全模块不可用） | 中（重构导出面时易漏） | **P0** |
| R2 | `InMemoryTracerBackend` 并发 export+clear+get：迭代中 clear 抛 RuntimeError / 切片赋值丢数据（修复前行为） | 中（观测数据丢失/异常，不影响 run 正确性） | 中（多会话并发场景） | **P1** |
| R3 | Logger 写失败静默吞掉（`except: pass` 回归）：排障零痕迹 | 中（观测降级无痕 → 事故难定位） | 中（后端偶发失败） | **P1** |
| R4 | span.attributes / message / detail 含 surrogate → `UnicodeEncodeError` 丢整条记录（内存有、磁盘无的半丢失） | 中（观测记录丢失） | 中（latin-1/编码误解码上游偶发） | **P1** |
| R5 | 审计写失败被静默吞掉（HC4 破坏：审计「以为记了其实没记」） | 高（合规/取证事故） | 中 | **P0** |
| R6 | 审计写失败无 stderr AUDIT_FALLBACK 留痕（降级必留痕缺失） | 高（失败不可观测） | 中 | **P0** |
| R7 | `AuditWriteError` 被上层 `except ObservabilityError` 意外捕获吞掉（基类回归） | 中 | 低 | **P1** |
| R8 | `DualAuditBackend` 双写丢任一后端（primary/secondary 不同步） | 中（双写冗余失效） | 低 | **P1** |
| R9 | `query_records` 过滤语义回归（agent/event/时间窗/排序/limit） | 中（查询结果错误误导取证） | 中 | **P1** |

**非功能范围声明**：性能（audit 每条 flush 的同步瓶颈）、SQLite 后端、Console 后端渲染、Tracer/Metrics facade 其余功能、hooks_adapter/provider 不在本设计范围。并发压测只验证「无异常/无串数据」属性，不测吞吐。

---

## 5. Oracle 策略

| 用例域 | Oracle 类型 | 依据 |
|--------|------------|------|
| 导出面 16 名清单 / 基类关系 / backend=None | golden value | 规格白纸黑字（`backend/__init__.py:35-44` 为权威清单） |
| InMemoryTracer 顺序、max_spans 裁剪、run_id 过滤 | golden value | 可人工推导（append→slice 语义，实测 s1..s8/max=5 → s4..s8） |
| 并发不变量（无异常、无串数据、终态确定） | property | 对任意交错成立；终态用 golden 收尾（clear 后 export 3 条 → 恰好 3 条） |
| 文件内容（surrogate 替换） | 蜕变关系 | 输出文件不可逐字预知 → 断言「含 U+FFFD / 不含 surrogate / utf-8 可读」，**禁止硬编码抄文件全文** |
| 内存镜像保留原值 | reference（原对象对拍） | export 前的 span 对象即 oracle |
| HC4 传播（异常类型/cause/fallback JSON 字段） | golden value | `AuditWriteError` 类型、`__cause__` 类型、JSON 键值均可人工推导 |
| Logger 留痕（消息/级别/exc_info） | golden value | 源码字符串字面量 `"observability logger write failed"` 即规格 |
| query 过滤 | golden value | 过滤规则 in_memory.py:33-46 可人工推导（闭区间、倒序、截断） |

> **自指 oracle 禁令**：文件内容断言只断言「属性」（含/不含/可读），不把"跑一遍被测实现得到的全文"当 golden value。

---

## 6. Mock / Fake 策略与豁免

- **零 mock（被测逻辑类）**：导出面、InMemory 后端、query 过滤、DualAuditBackend——全部真实对象。
- **Fake（协作者，允许）**：
  - Logger 用例：`BoomBackend`（write_log 抛 RuntimeError）、`RecordBackend`（记录收到的 record）、`CountingBackend`（计数）——纯内存 Fake，非 mock。
  - Audit 用例：`BoomWrite`（write 抛 RuntimeError）、`BoomFlush`（flush 抛 IOError）、`FlushSpy`（包 InMemory 数 flush 调用）——Fake。
- **pytest 内置捕获（非 mock）**：`caplog`（标准库 logging 真实链路）、`capsys`（stderr 被 pytest 替换为内存对象）、`tmp_path`（真实文件系统）。
- **豁免声明**：
  - 副作用验证：E1/E2/I1/I2/I3 无外部副作用（in-memory 纯对象），仅验证返回值/状态；I3 的副作用（线程异常收集）已纳入断言。
  - 回滚/清理：I3 终态用「clear 后 export 3 → 恰好 3」验证无残留；A2（flush 失败）验证 backend 无记录残留（write 成功但 flush 失败，记录已入内存——断言其**仍在**而非回滚，因为 InMemory 无事务，这是设计语义而非缺陷，见 A2 说明）。
  - 故障注入：纯函数/内存类不适用（I 组、E 组、A3/A5/A6/A7/A8）；A1/A2/L1 是故障注入用例（见各用例故障注入声明）。
  - 状态机：本模块无状态机，不适用。
  - 多参数 pairwise：query 过滤各维度相互独立（AND 组合无三方耦合），用「单维 + 组合」两个用例覆盖，不需 pairwise 降维；如未来出现耦合 bug 再升级。

---

## 7. 确定性控制（防 flaky）

| 不确定源 | 设计对策（写进 Given） |
|---------|----------------------|
| 并发交错 | I3 用 `threading.Barrier` 同步起点（party 数 = 线程数），**全程零 `time.sleep`**，迭代次数有界；`join(timeout=30)` + 断言线程全部退出防 CI 挂死；不变量断言对任意交错成立，不依赖特定调度 |
| 时间戳 | A6/A7 的过滤用例**直接 `backend.write()` 注入固定 tz-aware `datetime`**（秒级 0/10/20/30/40），不经 `write_sync` 的 `datetime.now()`；A4 对 write_sync 产生的时间戳只断言 `isinstance(datetime)` + `tzinfo == timezone.utc`，不断言具体值 |
| surrogate 构造 | 一律 `chr(0xD800)` / `chr(0xDC80)` / `'\ud83d\ude00'` 转义构造，**禁止在源码写非法字符字面量** |
| caplog 捕获 | 显式 `caplog.set_level(logging.DEBUG, logger="pandaren.observability.logger")`，钉死目标 logger，不依赖全局级别 |
| 浮点/排序 | 本设计无浮点断言；query 结果断言用 `[(r.timestamp.second, r.event_type.value) ...]` 结构化元组，顺序显式 |
| 文件读取 | 落盘断言用 `Path.read_text(encoding="utf-8")`（读失败即证明文件非法），避免二进制裸读 |

---

## 8. 等价类划分总表

| 维度 | 等价类 → 代表值 |
|------|----------------|
| 导出面 | 固定 16 名清单（四组 × 四后端），无输入空间 → golden 枚举 |
| InMemoryTracer 操作序列 | [export] / [export→get] / [export→get(run_id)] / [export→clear→get] |
| InMemoryTracer run_id | 匹配 / 不匹配（→`[]`）/ None（全量） |
| InMemoryTracer max_spans | 未超限 / 超限 1 条 / 大幅超限（8 条 vs max=5） |
| 并发线程角色 | 写者 / 清除者 / 读者（property 覆盖交错） |
| Logger level vs min_level | 低于（DEBUG<INFO，跳过早退）/ 等于（INFO=INFO，写）/ 高于（写） |
| Logger 后端结果 | 成功 / write_log 抛 RuntimeError（B3 分支） |
| Logger context | 全字段（module/agent_id/run_id/session_id/step_n/extra） |
| Surrogate 输入 | **EC-S1** `chr(0xDC80)`（surrogateescape 解码产物，PEP383 范围）→ 当前已防御、期望 pass；**EC-S2** `chr(0xD800)`（高代理）→ `[known-gap]` xfail；**EC-S3** `chr(0xDFFF)`（低代理其余）→ `[known-gap]` xfail；**EC-S4** surrogate pair `'\ud83d\ude00'` → `[known-gap]` xfail；**EC-S5** 正常 BMP 文本（含 `\|` 与换行）→ 防御不误伤、pass |
| Surrogate 字段位置 | span.attributes 值 / span.name / message / extra JSON / audit detail |
| Audit 失败注入点 | backend.write 抛 RuntimeError / backend.flush 抛 IOError |
| Audit detail surrogate | 无 / 有（facade `_sanitize_surrogates` 全范围 → `'?'`） |
| query 过滤 | agent_id / event_type(value 字符串) / 时间窗闭区间（含端点）/ 组合 / limit / 空结果 |

---

## 9. 覆盖矩阵（用例 × 风险/不变式）

| 用例 | inv-1 | inv-2 | inv-3 | inv-4 | inv-5 | inv-6 | inv-7 | inv-8 | inv-9 | inv-10 | inv-11 | inv-12 | inv-13 | inv-14 | inv-15 | 风险 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|------|
| E1 导出集一致 | ✅ | | | | | | | | | | | | | | | R1 |
| E2 顶层 import + 身份 | | ✅ | | | | | | | | | | | | | | R1 |
| I1 顺序语义 | | | | ✅ | | | | | | | | | | | | —(防回归基座) |
| I2 max_spans 裁剪 | | | | | ✅ | | | | | | | | | | | R2(裁剪) |
| I3 并发 stress | | | ✅ | ✅ | | | | | | | | | | | | R2 |
| L1 写失败不传播+留痕 | | | | | | ✅ | ✅ | | | | | | | | | R3 |
| L2 happy path 结构化记录 | | | | | | ✅ | | | | | | | | | | R3(正面) |
| L3 级别过滤（后端零调用） | | | | | | | | | | | | | | | | R3(邻接) |
| M1 tracer surrogate 落盘 | | | | | | | | ✅ | ✅ | | | | | | | R4 + KG-1 |
| M2 logger surrogate 落盘 | | | | | | | | ✅ | | | | | | | | R4 + KG-1 |
| M3 audit 后端 surrogate 落盘 | | | | | | | | ✅ | | | | | | | | R4 + KG-1 |
| M4 Facade+Markdown 全链路 | | | | | | ✅ | ✅ | ✅ | | | | | | | | R3+R4 + KG-1 |
| A1 write 失败 → fallback+raise | | | | | | | | | | | ✅ | | | | | R5, R6 |
| A2 flush 失败 → fallback+raise | | | | | | | | | | ✅ | ✅ | | | | | R5, R6 |
| A3 AuditWriteError 独立基类 | | | | | | | | | | | | ✅ | | | | R7 |
| A4 成功路径原子性 + facade 清洗 | | | | | | | | | | ✅ | | | | ✅ | | R5(正面) |
| A5 DualAuditBackend 双写/委托 | | | | | | | | | | | | | ✅ | | | R8 |
| A6 query 单维过滤 | | | | | | | | | | | | | | ✅ | | R9 |
| A7 query 组合/排序/limit/空 | | | | | | | | | | | | | | ✅ | | R9 |
| A8 backend=None → ValueError | | | | | | | | | | | | | | | ✅ | HC4 不可关闭 |

---

## 10. 用例展开（20 个）

### Group 1 — 顶层导出面（修复 1）

#### 用例 E1：16 个后端名在顶层与 backend 模块的 `__all__` 完全一致

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 导出集一致 [P0] + R1 ImportError 回归 [P0] |
| 测试层级 | unit |
| 覆盖准则 | N/A（结构性清单比对） |
| Oracle | golden value（`backend/__init__.py:35-44` 为权威清单） |
| Mock | 否 — 纯导入比对，零 mock |

**等价类划分**：导出面为固定 16 名（四组 Console/InMemory/Markdown/SQLite × 四子系统 Audit/Tracer/Metrics/Logger），无输入空间 → golden 枚举。

**Given**（前置条件）：
- `import pandaren.observability as obs`；`import pandaren.observability.backend as bk`
- 黄金清单 `BACKENDS = ["ConsoleAuditBackend","ConsoleTracerBackend","ConsoleMetricsBackend","ConsoleLoggerBackend","InMemoryAuditBackend","InMemoryTracerBackend","InMemoryMetricsBackend","InMemoryLoggerBackend","MarkdownAuditBackend","MarkdownTracerBackend","MarkdownMetricsBackend","MarkdownLoggerBackend","SQLiteAuditBackend","SQLiteTracerBackend","SQLiteMetricsBackend","SQLiteLoggerBackend"]`

**When**（操作/动作）：
- 计算 `obs_backends = [n for n in obs.__all__ if n in BACKENDS]`；`bk_backends = [n for n in bk.__all__ if n in BACKENDS]`

**Then**（预期结果）：
- `set(obs_backends) == set(bk_backends) == set(BACKENDS)`（三集合相等，缺一不可）
- `len(obs_backends) == 16`（不多不少）
- 副作用：无副作用，仅验证导入面

---

#### 用例 E2：每个后端名可顶层 import 且类身份一致（含曾缺失的两个）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 可 import + 身份一致 [P0] + R1 [P0] |
| 测试层级 | unit |
| 覆盖准则 | N/A（结构性） |
| Oracle | golden value（类身份 `is` 比较） |
| Mock | 否 — 零 mock |

**等价类划分**：16 名全枚举（含历史缺失的 `MarkdownLoggerBackend`、`InMemoryLoggerBackend` 为重点断言对象）。

**Given**（前置条件）：
- 同 E1 的 `obs`/`bk` 引用与 `BACKENDS` 清单

**When**（操作/动作）：
- 对每个 `name in BACKENDS`：断言 `hasattr(obs, name)`；`getattr(obs, name) is getattr(bk, name)`
- 额外执行一次直接 import：`from pandaren.observability import MarkdownLoggerBackend, InMemoryLoggerBackend`

**Then**（预期结果）：
- 16 名全部 `hasattr` 为真（顶层可达）
- 16 名全部类身份一致（顶层导出的类对象与 `backend` 模块的是同一对象）
- 直接 import 语句不抛 ImportError（曾缺失的两个可正常导入）
- 副作用：无副作用

---

### Group 2 — InMemoryTracerBackend 线程安全（修复 2）

#### 用例 I1：顺序语义 golden（export / get / get(run_id) / clear）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 返回集 ⊆ 已 export 集 [P1]；防回归基座（锁改动不破坏功能） |
| 测试层级 | unit |
| 覆盖准则 | 语句覆盖（export/get/clear/query_spans 全路径） |
| Oracle | golden value（顺序可人工推导） |
| Mock | 否 — 纯内存对象 |

**等价类划分**：操作序列 [export→get] / [get(run_id) 匹配/不匹配/None] / [clear→get 空]。

**Given**（前置条件）：
- `backend = InMemoryTracerBackend()`
- 构造 `Span`：`span_id="s1", trace_id="t", parent_span_id=None, span_type=SpanType.RUN, name="n1", agent_id="a", run_id="rA"`（s2 → run_id="rB"，s3 → run_id="rA"）

**When**（操作/动作）：
- `backend.export_span(s1)`；`backend.export_span(s2)`；`backend.export_span(s3)`
- `all_spans = backend.get_spans()`；`rA_spans = backend.get_spans("rA")`；`rX_spans = backend.get_spans("rX")`
- `backend.clear()`；`after = backend.get_spans()`

**Then**（预期结果）：
- `all_spans == [s1, s2, s3]`（插入序）
- `[s.span_id for s in rA_spans] == ["s1", "s3"]`（run_id 精确匹配，保序）
- `rX_spans == []`（不匹配返回空）
- `after == []`（clear 后为空）
- 副作用：无副作用（内存对象状态即返回值）

---

#### 用例 I2：max_spans 裁剪语义 golden

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 超限裁剪保留最新 [P1] + R2 切片赋值丢数据回归 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 语句覆盖（append→slice 分支） |
| Oracle | golden value（s1..s8 / max=5 → s4..s8，可人工推导） |
| Mock | 否 — 纯内存对象 |

**等价类划分**：max_spans = 5，导出条数 8（大幅超限）→ 代表值覆盖「未超限、超限、大幅超限」。

**Given**（前置条件）：
- `backend = InMemoryTracerBackend(max_spans=5)`
- 构造 8 个 `Span`（`span_id="s1".."s8"`，run_id="r"）

**When**（操作/动作）：
- 依序 `backend.export_span(s1..s8)`
- `got = backend.get_spans()`

**Then**（预期结果）：
- `len(got) == 5`（不超 max_spans）
- `[s.span_id for s in got] == ["s4", "s5", "s6", "s7", "s8"]`（丢弃最旧 s1-s3，保留最新）
- 副作用：无副作用

---

#### 用例 I3：并发 export + clear + get 无异常、无串数据、终态确定（防回归核心）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 并发不抛异常 [P1] + inv-4 无串数据 [P1] + R2 迭代中 clear 抛 RuntimeError/丢数据 [P1] |
| 测试层级 | unit（真实线程 + 真实 Lock，无外部 I/O） |
| 覆盖准则 | 并发属性（property 式：对任意交错成立） |
| Oracle | property（无异常 / 子集关系）+ golden 终态收尾 |
| Mock | 否 — 真实 threading，零 mock |

**等价类划分**：线程角色 = 写者 ×8、清除者 ×1、读者 ×2；迭代次数有界（不依赖真实 sleep）。

**Given**（前置条件）：
- 全新 `backend = InMemoryTracerBackend()`
- `errors: list`（线程安全追加）；`exported: set[str]`（测试自己的 `threading.Lock` 保护）
- `start = threading.Barrier(11)`（8 写 + 1 清 + 2 读，party 数 = 线程数）

**When**（操作/动作）：
- 写者 w（0..7）：barrier 后，循环 200 次：构造 `Span(span_id=f"w{w}-{i}", run_id=f"r{w}")` → 先加入 `exported` → `backend.export_span(span)`；任何异常 append 进 `errors`
- 清除者：barrier 后，循环 100 次 `backend.clear()`
- 读者 ×2：barrier 后，循环 50 次 `backend.get_spans()`，逐一断言 `s.span_id in exported`，否则记 `("foreign", s.span_id)`
- 全部 `join(timeout=30)`，断言所有线程 `not is_alive()`

**Then**（预期结果）：
- `errors == []`（**修复前此负载下高概率抛 `RuntimeError: list changed size during iteration`**，无异常即回归信号）
- 无 `("foreign", ...)` 记录（无串数据）
- 终态确定性：所有线程退出后 `backend.clear()` → 依序 export 3 个 `Span(span_id="final0..2")` → `get_spans()` 恰为 `["final0", "final1", "final2"]`（结构未被并发破坏）
- 副作用：见上述断言（线程异常收集为唯一外部状态）

---

### Group 3 — Logger 降级留痕（修复 3）

#### 用例 L1：写失败不传播 + caplog 留痕（exc_info=True）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 不传播 [P1] + inv-7 必留痕 [P1] + R3 静默吞回归 [P1] |
| 测试层级 | component(fake)（Logger + 假后端 + caplog 捕获真实标准库日志） |
| 覆盖准则 | branch: `Logger._write` 的 B3 except 分支 |
| Oracle | golden value（消息字面量）+ 副作用断言（caplog record） |
| Mock | 否 — 假后端 `BoomBackend`（Fake） |

**等价类划分**：后端结果 = write_log 抛异常 → 代表值 `RuntimeError("boom write")`。

**Given**（前置条件）：
- `class BoomBackend: def write_log(self, record): raise RuntimeError("boom write")`
- `caplog.set_level(logging.DEBUG, logger="pandaren.observability.logger")`
- `logger = Logger(backend=BoomBackend())`

**When**（操作/动作）：
- `logger.info("hello")`（不捕获异常，若传播则用例直接失败）

**Then**（预期结果）：
- 调用不抛任何异常（inv-6：非 HC4 降级语义保持）
- `caplog` 中恰有 1 条 levelname == "DEBUG"、logger == "pandaren.observability.logger" 的记录
- 该记录 `getMessage() == "observability logger write failed"`（inv-7 字面量）
- 该记录 `record.exc_info is not None`（exc_info=True 留痕，排障可复现异常）
- 故障注入声明：{故障类型: 后端 write_log 抛 RuntimeError}，注入点: `Logger._backend.write_log` → 期望: 不传播 + debug 留痕

---

#### 用例 L2：happy path — 结构化记录字段完整 + 写入后端（副作用）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 正面（成功路径不误伤）[P1] + R3 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: `Logger._write` 的 B2 try 成功分支 |
| Oracle | golden value（记录字段可人工推导） |
| Mock | 否 — 假后端 `RecordBackend`（Fake，记录收到内容） |

**等价类划分**：context 全字段（module/agent_id/run_id/session_id/step_n/自定义 extra）。

**Given**（前置条件）：
- `records = []`；`class RecordBackend: def write_log(self, record): records.append(record)`
- `logger = Logger(backend=RecordBackend(), agent_id="ag9")`

**When**（操作/动作）：
- `logger.info("hi", module="m", run_id="run1", session_id="s1", step_n=3, extra_key="v")`

**Then**（预期结果）：
- `len(records) == 1`
- `r = records[0]`：`r["level"] == "INFO"`、`r["message"] == "hi"`、`r["module"] == "m"`、`r["agent_id"] == "ag9"`（默认取自构造参数）、`r["run_id"] == "run1"`、`r["session_id"] == "s1"`、`r["step_n"] == 3`、`r["extra_key"] == "v"`（自定义 extra 透传）
- `isinstance(r["timestamp"], datetime)` 且 `r["timestamp"].tzinfo == timezone.utc`（不断言具体值）
- `isinstance(r["log_id"], str)` 且非空
- 副作用：`records` 长度与字段即副作用断言

---

#### 用例 L3：级别过滤 — min_level 之下后端零调用

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R3 邻接（防「不过滤直接写」回归）；`_should_log` 契约 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: `Logger._write` 的 B1 早退分支（`if not self._should_log: return`） |
| Oracle | 副作用断言（调用计数） |
| Mock | 否 — `CountingBackend`（Fake） |

**等价类划分**：level vs min_level = 低于（DEBUG=10 < INFO=20）/ 等于（INFO=20 ≥ 20，写）/ 高于。

**Given**（前置条件）：
- `cb = CountingBackend()`（`write_log` 时 `self.calls += 1`）
- `logger = Logger(backend=cb, min_level=LogLevel.INFO)`

**When**（操作/动作）：
- `logger.debug("skipped")`；`logger.info("written")`

**Then**（预期结果）：
- `cb.calls == 1`（DEBUG 被早退拦截，INFO 写入；**恰好 1 次**，非 0 也非 2）
- 副作用：`cb.calls` 即断言对象

---

### Group 4 — Markdown 后端 surrogate 防御（修复 4）

> 通用约定：surrogate 一律用 `chr(0xDC80)` / `chr(0xD800)` / `chr(0xDFFF)` / `'\ud83d\ude00'` 构造，禁止源码字面量。每个用例按 EC-S1..S5 等价类参数化（S2/S3/S4 标 `[known-gap]` → 下游落 `pytest.param(..., marks=pytest.mark.xfail(reason="KG-1: surrogateescape 仅覆盖 U+DC80-DCFF", strict=True))`；**strict=True 保证修复后「意外通过」即报警**）。

#### 用例 M1：tracer — surrogate attributes/name 成功落盘 + 内存镜像保留原值

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 成功落盘 [P1] + inv-9 内存保留原值 [P1] + R4 丢整条记录 [P1] + **KG-1** |
| 测试层级 | integration（真实文件系统 tmp_path） |
| 覆盖准则 | branch: `export_span` 的 B1（非空 attributes）/ B2（空 attributes）全达 |
| Oracle | 蜕变关系（文件内容属性断言，禁止抄全文）+ reference（内存原值对拍） |
| Mock | 否 — 真实写盘 |

**等价类划分**：surrogate 输入 = EC-S1 `chr(0xDC80)`（pass）/ EC-S2 `chr(0xD800)`（xfail）/ EC-S3 `chr(0xDFFF)`（xfail）/ EC-S4 pair `'\ud83d\ude00'`（xfail）；字段位置 = attributes 值 + span.name。

**Given**（前置条件）：
- `tmp = tmp_path`；`backend = MarkdownTracerBackend(tmp)`
- 对每个 surrogate 类构造 `span = Span(span_id="s1", trace_id="t1", parent_span_id=None, span_type=SpanType.LLM_CALL, name=f"llm_{ch}call", agent_id="ag1", run_id="r1", end_time=datetime(2026,1,1,12,0,0,tzinfo=timezone.utc), attributes={"model": f"gpt-{ch}", "input_tokens": 10})`（固定 end_time → 时间列确定）
- 另构造 `span_empty = Span(..., name="empty_attrs", attributes={})`（B2 分支）

**When**（操作/动作）：
- `backend.export_span(span)`；`backend.export_span(span_empty)`
- `text = (tmp / "_no_session" / "traces.md").read_text(encoding="utf-8")`（读失败即失败）
- `mem = backend.get_spans()`

**Then**（预期结果）：
- 不抛异常（EC-S1 pass；EC-S2/S3/S4 当前抛 `UnicodeEncodeError` → `[known-gap]` xfail）
- `text` 含 `"gpt-\ufffd"`、含 `"llm_\ufffdcall"`（字符被替换为 U+FFFD）
- `"\ud800" not in text` 且 `"\udc80" not in text`（文件无任何残留 surrogate；`read_text(utf-8)` 成功即证明）
- `text` 含 `"empty_attrs"` 所在行（B2 空 attributes 正常渲染，防御不误伤）
- `text` 含表头 `"| 时间 | 类型 |"`（整条记录行未丢，仅字符被替换）
- `mem[0].attributes["model"] == f"gpt-{ch}"`（**内存保留原始 surrogate**，inv-9：防御只作用于落盘行）
- `len(mem) == 2`
- 副作用：见上述文件与内存断言

---

#### 用例 M2：logger — surrogate message/extra 成功落盘（含 `|` 换行转义不误伤）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 [P1] + R4 [P1] + **KG-1** |
| 测试层级 | integration（真实文件系统 tmp_path） |
| 覆盖准则 | 语句覆盖（write_log 全路径，含 extra JSON 渲染） |
| Oracle | 蜕变关系（文件属性断言） |
| Mock | 否 — 真实写盘 |

**等价类划分**：surrogate 类 = EC-S1（pass）/ EC-S2 / EC-S3 / EC-S4（xfail）；字段位置 = message + extra JSON 值；另含转义字符 `|`、`\n`（EC-S5 防御不误伤）。

**Given**（前置条件）：
- `tmp = tmp_path`；`backend = MarkdownLoggerBackend(tmp)`
- `record = {"timestamp": datetime(2026,1,1,12,0,0,tzinfo=timezone.utc), "level": "INFO", "module": "m", "message": f"line1\nwith|pipe {ch}", "agent_id": "ag1", "run_id": "run1", "session_id": "", "step_n": 1, "extra": {"model": f"x{ch}"}}`

**When**（操作/动作）：
- `backend.write_log(record)`
- `text = (tmp / "_no_session" / "logs.md").read_text(encoding="utf-8")`

**Then**（预期结果）：
- 不抛异常（S1 pass；S2/S3/S4 `[known-gap]` xfail）
- `text` 含 `"with|pipe \ufffd"`（message 的 surrogate → U+FFFD）
- `text` 含 `"line1"` 与 `"with\\|pipe"` 的转义形态 `\|`（换行被压平、`|` 被转义——防御不破坏既有转义逻辑）
- `text` 含 `"x\ufffd"`（extra JSON 值同样被替换）
- `"\ud800" not in text`（无残留 surrogate）
- 副作用：见文件断言

---

#### 用例 M3：audit 后端 — detail surrogate 直接落盘（后端级防御 parity）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8（三后端一致性）[P1] + R4 [P1] + **KG-1** |
| 测试层级 | integration（真实文件系统 tmp_path） |
| 覆盖准则 | 语句覆盖（write 直线逻辑） |
| Oracle | 蜕变关系（文件属性断言） |
| Mock | 否 — 真实写盘 |

> 本用例**直接调 `MarkdownAuditBackend.write`**（不经 `AuditLog.write_sync`），验证后端级防御与 tracer/logger 对齐——这正是任务不变式「同文件三个事件流后端」的落点。

**等价类划分**：surrogate 类 = EC-S1（pass）/ EC-S2 / EC-S3 / EC-S4（xfail）；字段位置 = detail。

**Given**（前置条件）：
- `tmp = tmp_path`；`backend = MarkdownAuditBackend(tmp)`
- `record = AuditRecord(timestamp=datetime(2026,1,1,12,0,0,tzinfo=timezone.utc), record_id="rid1", event_type=AuditEventType.RUN_STARTED, severity=AuditSeverity.INFO, agent_id="ag1", run_id="run1", detail=f"detail {ch}")`

**When**（操作/动作）：
- `backend.write(record)`
- `text = (tmp / "_no_session" / "audit.md").read_text(encoding="utf-8")`

**Then**（预期结果）：
- 不抛异常（S1 pass；S2/S3/S4 `[known-gap]` xfail）
- `text` 含 `"detail \ufffd"`（或 `"detail ?"`——取决于兜底路径，断言「含 detail 且不含 surrogate 且含替换字符」即可）
- `"\ud800" not in text`、`"\udc80" not in text`
- `text` 含表头 `"| 时间 | 级别 |"`（整行落盘）
- 副作用：见文件断言

---

#### 用例 M4：全链路 — Logger Facade + MarkdownLoggerBackend（R3+R4 交汇）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 不传播 + inv-7 留痕 + inv-8 落盘 [P1] + R3 + R4 + **KG-1** |
| 测试层级 | integration（真实 FS + 真实 Facade + caplog） |
| 覆盖准则 | 路径覆盖：`Logger._write` B2（成功落盘）/ B3（失败留痕）双路径 |
| Oracle | 副作用断言（caplog）+ 蜕变关系（文件） |
| Mock | 否 — 真实组件组合 |

**等价类划分**：surrogate = EC-S1（成功路径：无留痕、有落盘）/ EC-S2（当前失败路径：有留痕、无落盘 → xfail）。

**Given**（前置条件）：
- `tmp = tmp_path`；`logger = Logger(backend=MarkdownLoggerBackend(tmp))`
- `caplog.set_level(logging.DEBUG, logger="pandaren.observability.logger")`

**When**（操作/动作）：
- EC-S1：`logger.info(f"msg {chr(0xDC80)}")`
- EC-S2：`logger.info(f"msg {chr(0xD800)}")`（同 backend，独立 tmp 目录）

**Then**（预期结果）：
- 两调用均不向调用方传播异常（inv-6）
- EC-S1：`caplog` 无 `"write failed"` 记录（成功不误报）；文件 `logs.md` 含 `"msg \ufffd"`（记录落盘）
- EC-S2：`caplog` 有 1 条 `"observability logger write failed"`（inv-7：Fail-Safe 把后端抛错转 debug 留痕——**留痕机制正常，但记录已丢**，正因如此后端防御才是唯一防线）；「文件含该记录」断言 `[known-gap]` xfail
- 副作用：见 caplog 与文件断言

---

### Group 5 — AuditLog HC4 传播语义（修复 5，重点）

#### 用例 A1：write 失败 → stderr AUDIT_FALLBACK + raise AuditWriteError（cause 链）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-11 fallback+raise [P0] + R5 静默吞 [P0] + R6 fallback 缺失 [P0] |
| 测试层级 | component(fake)（AuditLog + 假后端 + capsys 捕获 stderr） |
| 覆盖准则 | branch: `write_sync` 的 B2 except 分支（write 抛故障点） |
| Oracle | golden value（异常类型/cause/JSON 字段） |
| Mock | 否 — 假后端 `BoomWrite`（Fake） |

**等价类划分**：失败注入点 = backend.write → 代表值 `RuntimeError("simulated disk failure")`。

**Given**（前置条件）：
- `class BoomWrite: def write(self, record): raise RuntimeError("simulated disk failure"); def flush(self): pass; def query(self, **kw): return []`
- `al = AuditLog(backend=BoomWrite())`

**When**（操作/动作）：
- `with pytest.raises(AuditWriteError) as ei: al.write_sync(AuditEventType.RUN_STARTED, agent_id="ag1", run_id="r1", detail="d")`

**Then**（预期结果）：
- 异常类型为 `AuditWriteError`；`ei.value.__cause__` 为 `RuntimeError`（`raise ... from e` 链存在）
- `"run_started" in str(ei.value)` 且 `"ag1" in str(ei.value)`（错误消息含定位信息）
- stderr（`capsys.readouterr().err`）中恰有 1 行含 `"AUDIT_FALLBACK"`；`json.loads(该行)`：`["AUDIT_FALLBACK"] is True`、`["event_type"] == "run_started"`、`["agent_id"] == "ag1"`、`["run_id"] == "r1"`、`"simulated disk failure" in ["original_error"]`
- 故障注入声明：{故障类型: 后端 write 抛 RuntimeError}，注入点: `AuditLog._backend.write` → 期望: AUDIT_FALLBACK + AuditWriteError（**不允许静默**）

---

#### 用例 A2：flush 失败（write 成功）→ 同样 fallback + raise（原子性）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-10 write+flush 原子 [P0] + inv-11 [P0] + R5 + R6 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: B2 except 分支（flush 抛故障点，与 A1 区分注入点） |
| Oracle | golden value |
| Mock | 否 — 假后端 `BoomFlush`（Fake） |

**等价类划分**：失败注入点 = backend.flush（write 成功但 flush 失败）→ `IOError("flush boom")`。

**Given**（前置条件）：
- `class BoomFlush: def write(self, record): pass; def flush(self): raise IOError("flush boom"); def query(self, **kw): return []`
- `al = AuditLog(backend=BoomFlush())`

**When**（操作/动作）：
- `with pytest.raises(AuditWriteError) as ei: al.write_sync(AuditEventType.RUN_STARTED, agent_id="a", run_id="r", detail="d")`

**Then**（预期结果）：
- 抛 `AuditWriteError`；`ei.value.__cause__` 为 `IOError`（write 成功 ≠ 提交成功，flush 失败同样必须传播——HC4 原子语义）
- stderr 含 `"AUDIT_FALLBACK"` 且 `"flush boom" in original_error`
- 说明：InMemory 类后端无事务回滚，记录已在内存属预期（该断言对象是「异常+fallback」而非「回滚」）；真实磁盘/SQLite 场景的持久化由各自后端负责，不在本用例范围
- 故障注入声明：{故障类型: 后端 flush 抛 IOError}，注入点: `AuditLog._backend.flush` → 期望: AUDIT_FALLBACK + AuditWriteError

---

#### 用例 A3：AuditWriteError 独立基类（防被上层误吞）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-12 独立基类 [P1] + R7 |
| 测试层级 | unit |
| 覆盖准则 | N/A（结构性） |
| Oracle | golden value（`issubclass` 关系） |
| Mock | 否 |

**等价类划分**：异常继承关系 = 唯一判定。

**Given**（前置条件）：
- `from pandaren.observability.exceptions import AuditWriteError, ObservabilityError`

**When**（操作/动作）：
- 计算 `issubclass(AuditWriteError, ObservabilityError)` 与 `issubclass(AuditWriteError, Exception)`

**Then**（预期结果）：
- `issubclass(AuditWriteError, ObservabilityError) is False`（**核心断言**：上层 `except ObservabilityError` 吞不掉审计错误）
- `issubclass(AuditWriteError, Exception) is True`
- 副作用：无副作用

---

#### 用例 A4：成功路径 — write+flush 原子完成 + facade 全范围 surrogate 清洗

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-10 原子 [P0] + inv-14 查询可达 [P1] + R5 正面（成功路径不误伤） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: B1 try 成功分支 |
| Oracle | golden value（记录字段）+ 副作用断言（flush 计数） |
| Mock | 否 — `FlushSpy` 包 `InMemoryAuditBackend`（Fake） |

**等价类划分**：detail surrogate = 无 / 有（`chr(0xD800)` → facade 清洗为 `"?"`，全范围有效——与后端级 KG-1 对照）。

**Given**（前置条件）：
- `inner = InMemoryAuditBackend()`；`class FlushSpy: def __init__(self, inner): ... 代理 write/query，flush 时 self.flush_calls += 1`
- `al = AuditLog(backend=FlushSpy(inner))`

**When**（操作/动作）：
- `al.write_sync(AuditEventType.RUN_STARTED, agent_id="ag1", run_id="r1", detail="bad\ud800detail")`
- `rec = inner.query(agent_id="ag1")[0]`

**Then**（预期结果）：
- `FlushSpy.flush_calls == 1`（flush 被原子调用）
- `len(inner.query()) == 1`；`rec.event_type == AuditEventType.RUN_STARTED`、`rec.agent_id == "ag1"`、`rec.run_id == "r1"`
- `rec.detail == "bad?detail"`（**facade `_sanitize_surrogates` 全范围生效**：`'\ud800'` → `'?'`，与后端级 KG-1 形成对照——本用例 pass）
- `rec.severity == AuditSeverity.INFO`（`_DEFAULT_SEVERITY` 表查得，golden）
- `isinstance(rec.timestamp, datetime)` 且 `tzinfo == timezone.utc`
- 副作用：`flush_calls` 与 `inner.query()` 状态即断言对象

---

#### 用例 A5：DualAuditBackend 双写 + flush 委托 + query 委托 primary

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-13 双写/委托 [P1] + R8 |
| 测试层级 | unit（纯组合，无 I/O） |
| 覆盖准则 | 语句覆盖（write/flush/query 三方法） |
| Oracle | golden value + 副作用断言 |
| Mock | 否 — 两个 `InMemoryAuditBackend` + `FlushSpy` |

**等价类划分**：双端一致性 = primary/secondary 均收到 / query 只见 primary。

**Given**（前置条件）：
- `p = InMemoryAuditBackend()`；`s = InMemoryAuditBackend()`；`dual = DualAuditBackend(p, s)`
- 构造 `AuditRecord(record_id="rid-dup", ...)`

**When**（操作/动作）：
- `dual.write(record)`；`p.write(other_record)`（仅 primary 有第二条）
- `from_dual = dual.query()`

**Then**（预期结果）：
- `len(p.query()) == 2` 且 `len(s.query()) == 1`（write 使双端都收到）
- `p.query()[0].record_id == s.query()[0].record_id == "rid-dup"`（同一条记录）
- `from_dual` 含 `"rid-dup"` 与 `other_record.record_id`（query 委托 primary：secondary 独有的数据不可见）
- flush 委托：对包了 `FlushSpy` 的双端调用 `dual.flush()` → 两个 spy 的 `flush_calls` 均为 1
- 副作用：双端内容 + flush 计数即断言对象

---

#### 用例 A6：query_records 单维过滤（agent_id / event_type / 时间窗闭区间边界）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-14 [P1] + R9 |
| 测试层级 | component(fake)（AuditLog + InMemoryAuditBackend，时间戳直接注入固定值） |
| 覆盖准则 | 语句覆盖（query 各过滤分支） |
| Oracle | golden value（过滤结果可人工推导） |
| Mock | 否 — 真实 InMemory 后端 |

**等价类划分**：时间窗边界 = 闭区间（`>= start` 且 `<= end`，**端点含**）；agent_id = 精确；event_type = value 字符串。

**Given**（前置条件）：
- `backend = InMemoryAuditBackend()`；`log = AuditLog(backend=backend)`
- 直接 `backend.write(rec(...))` 注入 5 条固定 tz-aware 时间戳记录（**不经 write_sync，钉死时间**）：
  - `(sec=0, RUN_STARTED, alice)`、`(sec=10, RUN_FINISHED, alice)`、`(sec=20, PERMISSION_DENIED, bob)`、`(sec=30, RUN_STARTED, bob)`、`(sec=40, RUN_STARTED, alice)`

**When**（操作/动作）：
- `log.query_records(agent_id="alice")`
- `log.query_records(event_type="run_started")`
- `log.query_records(start_time="2026-01-01T00:00:10+00:00", end_time="2026-01-01T00:00:30+00:00")`
- `log.query_records(agent_id="nobody")`

**Then**（预期结果）：
- agent 过滤：`[(r.timestamp.second, r.event_type.value) for r in ...] == [(40,'run_started'), (10,'run_finished'), (0,'run_started')]`（精确匹配 + 时间倒序）
- event 过滤：`[r.agent_id ...] == ["alice", "bob", "alice"]`（value 字符串匹配）
- 时间窗 `[10,30]`：`== [(30,'run_started'), (20,'permission_denied'), (10,'run_finished')]`（**闭区间：10 与 30 两个端点都被包含**）
- 无匹配：`== []`
- 副作用：无副作用（查询纯读）

---

#### 用例 A7：query_records 组合过滤 + 倒序 + limit + 空结果

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-14 [P1] + R9 |
| 测试层级 | component(fake) |
| 覆盖准则 | 语句覆盖（组合 AND + sort + slice） |
| Oracle | golden value |
| Mock | 否 — 真实 InMemory 后端 |

**等价类划分**：组合维度 = agent × 时间窗（AND 语义）；limit = 截断；结果序 = 倒序。

**Given**（前置条件）：
- 复用 A6 的 5 条记录（同 backend）

**When**（操作/动作）：
- `log.query_records(agent_id="bob", start_time="2026-01-01T00:00:10+00:00", end_time="2026-01-01T00:00:30+00:00")`
- `log.query_records(limit=2)`
- `log.query_records()`

**Then**（预期结果）：
- 组合过滤：`[(r.timestamp.second, r.event_type.value) ...] == [(30,'run_started'), (20,'permission_denied')]`（bob ∩ 时间窗，AND 生效）
- `limit=2`：`[r.timestamp.second ...] == [40, 30]`（全量倒序后截断前 2）
- 全量：`[r.timestamp.second ...] == [40, 30, 20, 10, 0]`（时间倒序，新在前）
- 副作用：无副作用

---

#### 用例 A8：AuditLog(backend=None) → ValueError（HC4 不可关闭）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-15 [P0] + HC4「审计不可关闭」 |
| 测试层级 | unit |
| 覆盖准则 | branch: `AuditLog.__init__` 的 `if backend is None` 分支 |
| Oracle | golden value（ValueError + 消息） |
| Mock | 否 |

**等价类划分**：backend = None（唯一无效输入；其余为 Protocol 实例）。

**Given**（前置条件）：
- 无前置

**When**（操作/动作）：
- `with pytest.raises(ValueError) as ei: AuditLog(backend=None)`

**Then**（预期结果）：
- 抛 `ValueError`；`"不可为 None" in str(ei.value)` 且 `"HC4" in str(ei.value)`（消息含 HC4 说明）
- 副作用：无副作用

---

## 11. Known-Gaps（设计与实现差距）

| # | 用例 | 期望行为（设计） | 实现现状 | 差距原因 | 处置 |
|---|------|----------------|---------|---------|------|
| KG-1 | M1/M2/M3/M4 的 EC-S2/S3/S4 子断言 | surrogate 全范围（U+D800–U+DFFF）成功落盘（inv-8） | markdown.py:155/290/497 单行 `surrogateescape→replace` 仅覆盖 U+DC80–U+DCFF；U+D800–U+DBFF、U+DC00–U+DC7F、U+DD00–U+DFFF（含 surrogate pair）仍抛 `UnicodeEncodeError`，整条记录丢失（tracer 场景内存有、磁盘无） | `str.encode(errors='surrogateescape')` 的 PEP 383 范围限定；后端防御缺少 audit.py facade 级 `_sanitize_surrogates` 的 try/except + `errors='replace'` 兜底（audit.py:34-37） | 测试按期望写，标 `[known-gap]`，落 `xfail(strict=True)`；修复后「意外通过」即报警 |
| — | A4 | facade 级清洗全范围有效 | `_sanitize_surrogates` 兜底路径把 `'\ud800'` 替换为 `'?'`（实测 `"bad?detail"`） | 行为符合设计（替换非抛错）；与后端级替换字符（U+FFFD）不一致属可接受差异 | pass（无 gap） |

> 说明：`docs/代码总结/pandaren-modules/09-observability.md` §9 仍把导出面/logger 留痕/tracer 锁列为 P2/P3 风险——那是文档漂移（未随本次修复更新），非代码差距，不建 xfail。

---

## 12. 交付物与运行方式

- **测试文件**：`pandaren/tests/test_observability_fixes.py`（20 用例，Group 1-5；surrogate 用例按 EC-S1..S5 参数化）
- **运行命令**：`python -m pytest pandaren/tests/test_observability_fixes.py -q`（pytest 全量亦可，testpaths 已含 `pandaren`）
- **当前预期结果**：除 KG-1 相关的 EC-S2/S3/S4 参数化用例 `xfail` 外，其余全部 pass；`strict=True` 保证修复后 xfail 转为失败报警
- **导入约定**：`from pandaren.observability import ...`（对齐 `pandaren/tests/test_cancellation.py` 风格）
