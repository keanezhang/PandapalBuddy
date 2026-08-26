# 设计文档：pandaren/memory 风险修复测试（风险 1 / 2 / 4）

> 技术栈：pytest >= 7.0（pyproject 已配 `asyncio_mode=auto`，本批全部同步用例，无需 async）
> 测试落点：`pandaren/memory/tests/`（全新目录，已有 `__init__.py`）
> 被测代码锚点：
> - `pandaren/memory/backends/sqlite_raw_log.py`（唯一索引防线 / load_within_budget）
> - `pandaren/memory/reinject/sources.py`（文件字节上限防护）

## 1. 范围声明

| 变更 | 位置 | 是否需测试 | 说明 |
|------|------|:--:|------|
| 唯一索引防线（风险2） | sqlite_raw_log.py:46 / :59 / :162 | ✅ 行为变化 | 两个 `CREATE UNIQUE INDEX` + `_init_schema` 异常包装 |
| load_within_budget "至少保留一条"（风险1） | sqlite_raw_log.py:284 | ✅ 语义锁定 | 行为未变，但语义已文档化，需回归锁死 |
| 文件字节上限防护（风险4） | sources.py:72 / :110 / :380 | ✅ 行为变化 | `_read_file_with_size_limit` + 两个 source 的 max_bytes 参数 |
| 纯文档/注释更新（风险1 注释、_next_seq 注释等） | 各 docstring | ❌ 不测 | 无行为变化，仅注释 |

## 2. 依赖与 Mock/Fake 决策

| 依赖 | 决策 | 理由 |
|------|------|------|
| SQLite 数据库文件 | 真实现（`tmp_path` 落盘） | backend 显式拒绝 `:memory:`；SQLite 是嵌入式真 DB（真实 I/O） |
| TokenEstimator | 真实现 `CharBasedTokenEstimator`（backend 默认） | 零 mock；公式 `max(1, int(chars/4))` 可人工手算 token，作 golden oracle |
| `_next_seq`（唯一例外） | monkeypatch | 并发竞态窗口依赖 WAL 读快照时序，真并发无法确定性复现（flaky）；固定返回值等价模拟"两个写者算出相同 seq"的结果（见 6.1） |
| WorkingMemory | Fake（dict 实现 get/set） | 满足 `WorkingMemoryAccessor` 协议即可，内存实现替代 |
| 文件系统（sources） | 真实现（`tmp_path` 真实小文件） | 读取行为即被测对象本身，tmp_path 隔离生产路径 |
| 日志断言 | pytest `caplog` | 官方 fixture，确定性 |

**层级标注约定**：SQL/LDB 组标 **integration**（真实 SQLite 文件）；RD/RFS/PSS 组标 **integration**（真实文件系统 tmp_path 真实文件）；RFS-3 为纯构造断言标 **unit**。

**导入路径（供 test-coder）**：
- `from pandaren.memory.backends.sqlite_raw_log import SQLiteRawLogBackend`
- `from pandaren.memory.reinject.sources import RecentFilesSource, PlanStateSource, _read_file_with_size_limit`
- `from pandaren.memory.models import PostCompactContext, MessageDict, CompactBoundaryDict`
- `from pandaren.memory.constants import DEFAULT_POST_COMPACT_MAX_BYTES_PER_FILE, RECENT_FILE_READS_WM_KEY`

## 3. 风险与不变式清单

### 3.1 sqlite_raw_log.py 唯一索引防线（风险2）

| ID | 内容 | 级别 |
|----|------|------|
| inv-S1 | 全新库构造成功且两表各含唯一索引 (session_id, seq)；重复构造同库幂等成功 | — |
| inv-S2 | raw_messages 与 compact_boundaries **各自**内部 (session_id, seq) 唯一（两条防线都在） | — |
| inv-S3 | 重复写入同 session 同 seq → 显式 `sqlite3.IntegrityError`（fail-fast，不静默错位） | — |
| inv-S4 | 历史脏库（已存在重复 (session_id, seq)）构造时抛 RuntimeError 且消息含 single-writer 引导（不静默放行） | — |
| Risk-S1 | 多写者并发窗口下同 seq 静默写入 → 数据错位 [P0] | P0 |
| Risk-S2 | 脏库构造时异常无引导信息 → 应用层无法定位"单写者"约束 [P0] | P0 |
| Risk-S3 | 建索引失败被吞 → 防线形同虚设（异常必须传播）[P1] | P1 |
| Risk-S4 | 唯一索引误伤正常顺序 append（连续 append 同 session 必须各自拿到递增 seq）[P1] | P1 |

### 3.2 load_within_budget "至少保留一条"（风险1）

| ID | 内容 | 级别 |
|----|------|------|
| inv-L1 | 返回列表按时间从旧到新（正序） | — |
| inv-L2 | 返回消息全部来自最新 boundary 之后（`seq > boundary_seq`） | — |
| inv-L3 | **至少保留一条**：预算放不下任何一条时（首条即超、keep_count==0）不 break，返回最新一条而非空 | — |
| inv-L4 | 除首条豁免外，累计估算 token ≤ budget（超预算即停） | — |
| inv-L5 | `token_budget <= 0` 或空 session_id → 返回 `[]` | — |
| inv-L6 | 确定性：同库同 (session, budget) 多次调用返回相同列表 | — |
| Risk-L1 | 首条超预算被错误 break → 恢复出空 STM → 调用方逻辑异常 [P0] | P0 |
| Risk-L2 | 截断方向错（从前往后截）→ 保留最旧而非最新 [P1] | P1 |
| Risk-L3 | 翻回正序失败 → 返回倒序，LLM 上下文乱序 [P1] | P1 |
| Risk-L4 | 边界：恰好等于预算（`>` 而非 `>=`），等于时不应 break [P2] | P2 |
| Risk-L5 | boundary 起点语义：早于最新 boundary 的消息不应回读 [P2] | P2 |

### 3.3 sources.py 文件字节上限防护（风险4）

| ID | 内容 | 级别 |
|----|------|------|
| inv-R1 | 文件 size > max_bytes → 返回 None（stat 后跳过，不整读）+ warning 日志 | — |
| inv-R2 | 文件不存在 / stat 失败 / 读取失败 → 返回 None + info 日志（E4 降级不崩溃） | — |
| inv-R3 | 文件 size ≤ max_bytes（含**恰好相等**）→ 正常返回全文 | — |
| inv-R4 | RecentFilesSource 超限文件被跳过不回注；正常文件照常回注（混用互不污染）；全超限返回空列表 | — |
| inv-R5 | PlanStateSource 超限 / 不存在 / meta 缺失 → 返回空列表（不崩溃）；正常 → 单附件 | — |
| inv-R6 | 两个 source 构造默认 `max_bytes(_per_file)` = 1 MiB = `DEFAULT_POST_COMPACT_MAX_BYTES_PER_FILE` | — |
| Risk-R1 | 超大文件被整读进内存白耗 IO（修复动机）[P0] | P0 |
| Risk-R2 | stat/read 失败时 source 崩溃 → 回注链路整体失败（E4 要求降级）[P1] | P1 |
| Risk-R3 | 恰好等于上限的文件被误跳过（`>` vs `>=` 边界）[P2] | P2 |
| Risk-R4 | 混用场景一个超限文件连累正常文件回注 [P2] | P2 |
| Risk-R5 | 默认 1 MiB 被误改（构造参数默认值回归）[P3] | P3 |

## 4. 覆盖矩阵

### 4.1 SQL 组（唯一索引防线）

| 用例 | inv-S1 | inv-S2 | inv-S3 | inv-S4 | Risk-S1 | Risk-S2 | Risk-S3 | Risk-S4 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| SQL-1 新库初始化+索引存在 | ✅ | ✅ | | | | | ✅ | |
| SQL-2 连续 append 递增 seq | | | | | | | | ✅ |
| SQL-3 重复 seq append → IntegrityError | | | ✅ | | ✅ | | | |
| SQL-4 脏库 raw_messages → RuntimeError | | | | ✅ | | ✅ | ✅ | |
| SQL-5 脏库 compact_boundaries → RuntimeError | | ✅ | | ✅ | ✅ | | | |

### 4.2 LDB 组（load_within_budget）

| 用例 | inv-L1 | inv-L2 | inv-L3 | inv-L4 | inv-L5 | inv-L6 | Risk-L1 | Risk-L2 | Risk-L3 | Risk-L4 | Risk-L5 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| LDB-1 首条即超仍返回 | | | ✅ | | | | ✅ | | | | |
| LDB-2 预算极小多条→最新一条 | ✅ | | ✅ | | | | ✅ | | | | |
| LDB-3 从尾向前截断+正序 | ✅ | | | ✅ | | | | ✅ | ✅ | | |
| LDB-4 恰好等于预算不 break | | | | ✅ | | | | | | ✅ | |
| LDB-5 budget<=0 → 空 | | | | | ✅ | | | | | | |
| LDB-6 boundary 起点不回读 | | ✅ | | | | | | | | | ✅ |
| LDB-7 空 session + 确定性 | | | | | ✅ | ✅ | | | | | |

### 4.3 Sources 组（字节上限防护）

| 用例 | inv-R1 | inv-R2 | inv-R3 | inv-R4 | inv-R5 | inv-R6 | Risk-R1 | Risk-R2 | Risk-R3 | Risk-R4 | Risk-R5 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| RD-1 helper 正常读 | | | ✅ | | | | | | | | |
| RD-2 helper 恰好等于上限 | | | ✅ | | | | | | ✅ | | |
| RD-3 helper 超限→None+warning | ✅ | | | | | | ✅ | | | | |
| RD-4 helper 不存在→None+info | | ✅ | | | | | | ✅ | | | |
| RD-5 helper 读失败→None+info | | ✅ | | | | | | ✅ | | | |
| RFS-1 混用：超限跳过+正常回注 | ✅ | | | ✅ | | | | | | ✅ | |
| RFS-2 全超限→空列表 | ✅ | | | ✅ | | | ✅ | | | | |
| RFS-3 默认 1 MiB 回归 | | | | | | ✅ | | | | | ✅ |
| PSS-1 正常 plan 回注单附件 | | | ✅ | | ✅ | | | | | | |
| PSS-2 plan 超限→空列表 | ✅ | | | | ✅ | | ✅ | | | | |
| PSS-3 meta 缺失/文件不存在→空 | | ✅ | | | ✅ | | | ✅ | | | |
| PSS-4 空白 plan 文件→空 | | | | | ✅ | | | | | | |

## 5. 用例详细设计

### SQL 组：唯一索引防线（sqlite_raw_log.py）

#### 用例 SQL-1：全新库构造成功，两表唯一索引存在，重复构造幂等

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-S1 [P0] + Risk-S3（建索引失败不能被吞，成功即防线就位） |
| 测试层级 | integration（真实 SQLite 文件，tmp_path 落盘） |
| 覆盖准则 | `_init_schema` happy 分支（executescript 成功） |
| Oracle | golden value（查询 sqlite_master 的确定结果） |
| Mock | 否 — 零 mock |

**等价类划分**：db_path 输入 = 全新不存在路径 → 代表值 = `tmp_path / "raw.db"`

**Given**：
- `db = tmp_path / "raw.db"`（不存在）

**When**：
- `backend = SQLiteRawLogBackend(db_path=db)`；随后 `backend.close()`

**Then**：
- 构造不抛异常
- 副作用（DB 状态）：`SELECT name FROM sqlite_master WHERE type='index'` 结果含 `uq_raw_session_seq` 与 `uq_boundary_session_seq` 两条
- 幂等：`SQLiteRawLogBackend(db_path=db)` 再次构造成功（`IF NOT EXISTS` 不冲突）
- 无异常即证明 Risk-S3 不存在（索引创建失败未被静默吞掉）

---

#### 用例 SQL-2：连续 append 同 session 拿到递增 seq（唯一索引不误伤正常路径）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-S4（索引只拦重复，不拦正常顺序）[P1] |
| 测试层级 | integration（真实 SQLite 文件） |
| 覆盖准则 | append_raw_message happy 分支 |
| Oracle | golden value（seq=1,2,3 可独立推导：`MAX(seq)+1`） |
| Mock | 否 |

**等价类划分**：同 session 连续写 3 条 → 代表消息 content = `'m1'` / `'m2'` / `'m3'`

**Given**：
- `backend = SQLiteRawLogBackend(db_path=tmp_path / "raw.db")`

**When**：
- `backend.append_raw_message({"role": "user", "content": "m1"}, session_id="s")`
- `backend.append_raw_message({"role": "user", "content": "m2"}, session_id="s")`
- `backend.append_raw_message({"role": "user", "content": "m3"}, session_id="s")`

**Then**：
- `backend.load_all("s")` 返回 3 条，顺序 `['m1','m2','m3']`（正序）
- 副作用（DB 状态）：直接查 `SELECT seq FROM raw_messages WHERE session_id='s' ORDER BY seq` = `[1, 2, 3]`（唯一索引未导致任何一次 append 失败）
- 无异常抛出

---

#### 用例 SQL-3：重复 seq 写入 → sqlite3.IntegrityError（并发窗口 fail-fast 防线）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-S3 [P0] + Risk-S1（多写者同 seq 竞态） |
| 测试层级 | integration（真实 SQLite 文件） |
| 覆盖准则 | 唯一索引约束分支（INSERT 违反唯一性 → IntegrityError） |
| Oracle | golden value（期望异常类型为 `sqlite3.IntegrityError`，规格明确） |
| Mock | 是（唯一例外）— monkeypatch `backend._next_seq` 固定返回 `1`，等价模拟"并发窗口内两个写者各自算出相同 seq"的结果 |

**等价类划分**：写入冲突形态 = 两次 append 算出相同 seq → 代表值 = `_next_seq` 固定返回 1

**Given**：
- `backend = SQLiteRawLogBackend(db_path=tmp_path / "raw.db")`
- `monkeypatch.setattr(backend, "_next_seq", lambda session_id: 1)`（模拟竞态：两个写者都读到 MAX=0 → 各自算出 seq=1）

**When**：
- 第一次 `backend.append_raw_message({"role": "user", "content": "a"}, session_id="s")`（落库 (s,1)）
- 第二次 `backend.append_raw_message({"role": "user", "content": "b"}, session_id="s")`（再次尝试 (s,1)）

**Then**：
- 第一次 append 成功，无异常
- 第二次 append 抛 `sqlite3.IntegrityError`（fail-fast，**不**静默写入错位 seq）
- 副作用（DB 状态）：`raw_messages` 中该 session 仅 1 行（重复行未被写入，无脏数据残留）

> 为什么不用真并发：竞态窗口依赖 WAL 读快照时序与真实线程调度，无法确定性复现（flaky）。monkeypatch 固定 `_next_seq` 等价于"竞态已发生、两写者拿到相同 seq"的**结果**，验证的是防线的本质——唯一索引必须拦截重复行。

---

#### 用例 SQL-4：历史脏库（raw_messages 有重复）→ RuntimeError + single-writer 引导

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-S4 [P0] + Risk-S2（无引导信息则无法定位） + Risk-S3 |
| 测试层级 | integration（真实 SQLite 文件） |
| 覆盖准则 | `_init_schema` 的 `except sqlite3.IntegrityError` 分支（唯一索引创建失败） |
| Oracle | golden value（异常类型 + 消息引导词，规格明确） |
| Mock | 否 — 脏数据由裸 sqlite3 连接手工构造 |

**等价类划分**：脏数据形态 = 同 session 同 seq 两行 → 代表值 = `('s', 1)` 插两行

**Given**：
- 用裸 `sqlite3.connect(tmp_path / "dirty.db")` 手工建 `raw_messages` 表（**不含**唯一索引，DDL 同源码建表语句去掉 UNIQUE INDEX 行）
- 插入两行重复数据：`INSERT INTO raw_messages (session_id, seq, role, content, ts) VALUES ('s', 1, 'user', 'a', 't')` ×2，commit 后 close

**When**：
- `SQLiteRawLogBackend(db_path=tmp_path / "dirty.db")`（构造触发 `_init_schema` → 建 `uq_raw_session_seq` 失败）

**Then**：
- 抛 `RuntimeError`
- 异常消息包含引导词：`"concurrent"` 与 `"single writer"`（或同义 `"exactly one writer"`）——可独立推导自源码 `_init_schema` 的文案
- 异常链：`exc.__cause__` 是 `sqlite3.IntegrityError`（`from exc` 保留原始错误）
- 副作用：数据库文件保持原状，无部分索引残留

---

#### 用例 SQL-5：历史脏库（compact_boundaries 有重复）→ 同样 RuntimeError（两条防线一致）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-S2 [P0] + Risk-S1 + inv-S4 |
| 测试层级 | integration（真实 SQLite 文件） |
| 覆盖准则 | `_init_schema` 中 `_SCHEMA_COMPACT_BOUNDARIES` 的建索引失败分支 |
| Oracle | golden value（与 SQL-4 同构） |
| Mock | 否 |

**等价类划分**：脏数据落点 = compact_boundaries 表（对称于 SQL-4）→ 代表值 = `('s', 1)` 两行

**Given**：
- 裸 `sqlite3.connect(tmp_path / "dirty2.db")` 手工建 `compact_boundaries` 表（无唯一索引），插入 `('s', 1)` ×2，commit 后 close

**When**：
- `SQLiteRawLogBackend(db_path=tmp_path / "dirty2.db")`

**Then**：
- 抛 `RuntimeError`，消息含 `"single writer"` 引导词（与 SQL-4 同一文案）
- `exc.__cause__` 是 `sqlite3.IntegrityError`
- 结论：`uq_boundary_session_seq` 与 `uq_raw_session_seq` 两条防线**都在**（inv-S2）

---

### LDB 组：load_within_budget（sqlite_raw_log.py）

> **Oracle 基线**：content 全 ASCII 定长，`CharBasedTokenEstimator`（backend 默认）公式 = `max(1, int(len/4))`。40 字符 = 10 tokens、200 字符 = 50 tokens，**人工可推导**，非跑实现抄来。
> **覆盖准则（复合判定）**：循环内 `accumulated + t > token_budget and keep_count > 0` 是两子条件复合判定，本组覆盖全部三种有效取值组合：`(T, F)` 首条超且 keep=0 不 break（LDB-1/2）；`(T, T)` 超且已有保留 break（LDB-3）；`(F, *)` 不超继续（LDB-3/4）。

#### 用例 LDB-1：首条消息即超预算 → 仍返回该条（keep_count==0 不 break）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-L3 [P0] + Risk-L1 |
| 测试层级 | integration（真实 SQLite 文件） |
| 覆盖准则 | 判定 `(T, F)`：超预算但 keep_count==0 → 不 break |
| Oracle | golden value（token 手算：200 字符 = 50 tokens） |
| Mock | 否 — 用默认 CharBasedTokenEstimator |

**等价类划分**：单条消息 token 估算 vs 预算 = 首条即超（50 > 10）→ 代表值 = content `'x' * 200`、`token_budget=10`

**Given**：
- `backend = SQLiteRawLogBackend(db_path=tmp_path / "raw.db")`（默认 estimator）
- `backend.append_raw_message({"role": "user", "content": "x" * 200}, session_id="s")`（估算 50 tokens）

**When**：
- `result = backend.load_within_budget("s", token_budget=10)`

**Then**：
- 返回长度 = 1（非空——STM 不为空）
- `result[0]["content"] == "x" * 200`（保留的是那条超预算消息本身）
- 副作用：无（只读操作，DB 无变更）

---

#### 用例 LDB-2：预算极小 + 多条消息 → 只保留最新一条而非空

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-L3 [P0] + Risk-L1 + inv-L1 |
| 测试层级 | integration（真实 SQLite 文件） |
| 覆盖准则 | 判定 `(T, F)`（首条即超）+ 复合判定的二次 break（后续消息超且 keep=1） |
| Oracle | golden value（每条 10 tokens，budget=5 装不下任何一条） |
| Mock | 否 |

**等价类划分**：多条消息 × 预算装不下任何一条 → 代表值 = 3 条各 40 字符、`token_budget=5`

**Given**：
- backend（默认 estimator）；依次 append content `'a'*40`（seq=1）、`'b'*40`（seq=2）、`'c'*40`（seq=3）

**When**：
- `result = backend.load_within_budget("s", token_budget=5)`

**Then**：
- 返回长度 = 1
- `result[0]["content"] == "c" * 40`（保留**最新**那条，不是最旧）
- 列表正序（单元素恒正序）

---

#### 用例 LDB-3：正常多条按 budget 从尾向前截断，翻回正序

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-L4 [P1] + inv-L1 + Risk-L2（截断方向）+ Risk-L3（正序） |
| 测试层级 | integration（真实 SQLite 文件） |
| 覆盖准则 | 判定 `(T, T)` 触发 break；`(F, *)` 前两条继续 |
| Oracle | golden value（手算：10+10=20 ≤ 25，+10=30 > 25 → 停） |
| Mock | 否 |

**等价类划分**：消息数 × 预算 = 4 条 × 25 → 代表值 = `'a'*40, 'b'*40, 'c'*40, 'd'*40`（各 10 tokens）

**Given**：
- backend；依次 append 4 条（seq=1..4，content 分别为 `'a'*40`/`'b'*40`/`'c'*40`/`'d'*40`）

**When**：
- `result = backend.load_within_budget("s", token_budget=25)`

**Then**：
- 返回长度 = 2
- `result[0]["content"] == "c" * 40`、`result[1]["content"] == "d" * 40`（从尾向前保留 seq=4、3；seq=2 时累计 30 > 25 → break）
- 顺序为正序（`c` 在前 `d` 在后，非倒序——Risk-L3）

---

#### 用例 LDB-4：边界——累计恰好等于预算时不 break（`>` 而非 `>=`）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-L4 [P2] + Risk-L4 |
| 测试层级 | integration（真实 SQLite 文件） |
| 覆盖准则 | 判定 `(F, *)`：`accumulated + t == budget` 不满足 `>` → 继续 |
| Oracle | golden value（10 + 10 == 20，边界值） |
| Mock | 否 |

**等价类划分**：累计 vs 预算 = 恰好相等 → 代表值 = 2 条各 10 tokens、`token_budget=20`

**Given**：
- backend；append `'a'*40`（seq=1）、`'b'*40`（seq=2）

**When**：
- `result = backend.load_within_budget("s", token_budget=20)`

**Then**：
- 返回长度 = 2（累计 20 == 预算 20，不 break，两条全保留）
- `result[0]["content"] == "a" * 40`、`result[1]["content"] == "b" * 40`

---

#### 用例 LDB-5：边界——token_budget <= 0 → 空列表

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-L5 [P2] |
| 测试层级 | integration（真实 SQLite 文件） |
| 覆盖准则 | 入口守卫分支 `token_budget <= 0` |
| Oracle | golden value（规格明确：`[]`） |
| Mock | 否 |

**等价类划分**：预算输入域 = {0, 负数} → 代表值 = `0`、`-1`

**Given**：
- backend；已 append 1 条消息（确保库非空，排除"空库返回空"的混淆）

**When**：
- `r0 = backend.load_within_budget("s", token_budget=0)`
- `rn = backend.load_within_budget("s", token_budget=-1)`

**Then**：
- `r0 == []` 且 `rn == []`（不读任何消息）

---

#### 用例 LDB-6：boundary 起点语义——早于最新 boundary 的消息不回读

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-L2 [P2] + Risk-L5 |
| 测试层级 | integration（真实 SQLite 文件） |
| 覆盖准则 | `boundary_seq` 过滤分支（`WHERE seq > boundary_seq`） |
| Oracle | golden value（seq 可独立推导：boundary 也占用 seq，见 Given 推导） |
| Mock | 否 |

**等价类划分**：数据形态 = boundary 前后都有消息 → 代表值 = 3 条消息 + 1 个 boundary + 1 条新消息

**Given**：
- backend；append `'a'*40`/`'b'*40`/`'c'*40`（seq=1,2,3）
- `backend.append_compact_boundary({"type": "compact_boundary", "timestamp": "t", "tokens_before": 30, "tokens_after": 10, "kept_message_count": 3}, session_id="s")`（`_next_seq` 读 MAX(seq)=3 → boundary seq=4）
- `backend.append_raw_message({"role": "user", "content": "d" * 40}, session_id="s")`（seq=5）

**When**：
- `result = backend.load_within_budget("s", token_budget=1000)`

**Then**：
- 返回长度 = 1（boundary_seq=4，只读 `seq > 4` 的消息）
- `result[0]["content"] == "d" * 40`（早于 boundary 的 `a/b/c` 不回读——Risk-L5）

---

#### 用例 LDB-7：边界——空 session_id → 空列表；确定性 property

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-L5 [P2] + inv-L6（确定性 [property]） |
| 测试层级 | integration（真实 SQLite 文件） |
| 覆盖准则 | 入口守卫分支 `not session_id` |
| Oracle | golden value + property（`f(x) === f(x)` 两次调用一致） |
| Mock | 否 |

**等价类划分**：session_id 输入域 = 空串 → 代表值 = `''`；确定性 = 任意预算重复调用

**Given**：
- backend；已 append 若干消息

**When**：
- `r_empty = backend.load_within_budget("", token_budget=100)`
- `r1 = backend.load_within_budget("s", token_budget=30)`；`r2 = backend.load_within_budget("s", token_budget=30)`

**Then**：
- `r_empty == []`
- 确定性：`r1 == r2`（内容与顺序逐条相等，inv-L6）

---

### Sources 组：文件字节上限防护（reinject/sources.py）

> 日志断言基线：logger name 前缀 `pandaren.memory.reinject.sources`（caplog 用 `caplog.at_level(logging.WARNING, logger="pandaren.memory.reinject.sources")` 或按级别过滤）。info 日志用 `caplog.at_level(logging.INFO, ...)`。
> `max_bytes` 一律传小值（64 等）构造超限文件，避免写 1 MiB 大文件。

#### 用例 RD-1：helper 正常文件 → 返回全文

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R3 [P1] |
| 测试层级 | integration（真实文件系统，tmp_path 真实小文件） |
| 覆盖准则 | helper happy 分支（stat 通过 + open 成功） |
| Oracle | golden value（文件内容已知） |
| Mock | 否 |

**等价类划分**：文件大小 vs 上限 = 远小于 → 代表值 = 20 字节文件、`max_bytes=1024`

**Given**：
- `f = tmp_path / "small.txt"`；`f.write_text("hello world, read me", encoding="utf-8")`（20 字节）

**When**：
- `text = _read_file_with_size_limit(str(f), max_bytes=1024, source_name="test_src")`

**Then**：
- `text == "hello world, read me"`

---

#### 用例 RD-2：helper 边界——文件恰好等于上限 → 仍返回全文

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R3 [P2] + Risk-R3（`>` vs `>=`，恰好相等不误杀） |
| 测试层级 | integration（真实文件系统） |
| 覆盖准则 | 边界条件 `size == max_bytes`（`size > max_bytes` 为 False） |
| Oracle | golden value（64 字节 == 上限 64） |
| Mock | 否 |

**等价类划分**：文件大小 vs 上限 = 恰好相等 → 代表值 = 64 字节文件、`max_bytes=64`

**Given**：
- `f = tmp_path / "exact.txt"`；`f.write_text("a" * 64, encoding="utf-8")`（size == 64）

**When**：
- `text = _read_file_with_size_limit(str(f), max_bytes=64, source_name="test_src")`

**Then**：
- `text == "a" * 64`（不返回 None；`64 > 64` 为 False → 不跳过）
- 无 warning 日志（Risk-R3：恰好相等不属于超限）

---

#### 用例 RD-3：helper 超限 → 返回 None + warning 日志（stat 后跳过）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R1 [P0] + Risk-R1 |
| 测试层级 | integration（真实文件系统） |
| 覆盖准则 | helper 超限分支（`size > max_bytes` → warning + None） |
| Oracle | golden value（返回 None + 日志文案） |
| Mock | 否 — caplog 断言日志 |

**等价类划分**：文件大小 vs 上限 = 超过 → 代表值 = 100 字节文件、`max_bytes=64`

**Given**：
- `f = tmp_path / "big.txt"`；`f.write_text("y" * 100, encoding="utf-8")`（100 > 64）

**When**：
- `text = _read_file_with_size_limit(str(f), max_bytes=64, source_name="test_src")`

**Then**：
- `text is None`（未整读进内存）
- 副作用（日志）：caplog 捕获 1 条 WARNING 记录，消息同时含 `"exceeds limit"`、`"100"`、`"64"`、文件名 `"big.txt"`

---

#### 用例 RD-4：helper 文件不存在 → 返回 None + info 日志（stat 失败降级）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R2 [P1] + Risk-R2（E4 降级不崩溃） |
| 测试层级 | integration（真实文件系统） |
| 覆盖准则 | helper stat 失败分支（`os.path.getsize` OSError → info + None） |
| Oracle | golden value（返回 None + 日志文案） |
| Mock | 否 |

**等价类划分**：路径存在性 = 不存在 → 代表值 = `tmp_path / "nope.txt"`（未创建）

**Given**：
- `missing = tmp_path / "nope.txt"`（不存在）

**When**：
- `text = _read_file_with_size_limit(str(missing), max_bytes=1024, source_name="test_src")`

**Then**：
- `text is None`
- 副作用（日志）：caplog 捕获 1 条 INFO 记录，消息含 `"cannot stat"` 与路径

---

#### 用例 RD-5：helper 读取失败 → 返回 None + info 日志（open 失败降级）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R2 [P1] + Risk-R2 |
| 测试层级 | integration（真实文件系统） |
| 覆盖准则 | helper 读取失败分支（open 抛 OSError/IOError → info + None） |
| Oracle | golden value（返回 None + 日志文案） |
| Mock | 是 — monkeypatch `builtins.open` 仅对目标路径抛 `OSError`（其余调用走原实现），保证 stat 成功、read 失败分支被确定触发 |

**等价类划分**：失败形态 = stat 成功但读取失败 → 代表值 = 真实存在的小文件 + open 被注入 OSError

**Given**：
- `f = tmp_path / "readable_stat.txt"`；`f.write_text("x" * 16, encoding="utf-8")`（stat 必然成功）
- monkeypatch `builtins.open`：当 `args[0] == str(f)` 时抛 `OSError("simulated read failure")`，否则调用原 open（避免破坏 caplog 等内部 open）

**When**：
- `text = _read_file_with_size_limit(str(f), max_bytes=1024, source_name="test_src")`

**Then**：
- `text is None`
- 副作用（日志）：caplog 捕获 1 条 INFO 记录，消息含 `"failed to read"` 与路径
- 不抛异常（E4：读取失败是降级而非崩溃）

---

#### 用例 RFS-1：RecentFilesSource 混用场景——超限文件被跳过，正常文件照常回注

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R4 [P2] + Risk-R4（一个坏文件不连累正常文件） |
| 测试层级 | integration（真实文件系统 + Fake working_memory） |
| 覆盖准则 | `collect` 主循环：raw is None → continue（超限跳过）；raw 非 None → 构建 attachment |
| Oracle | golden value（附件数、source_name、title 内容确定） |
| Mock | 否 — working_memory 用 dict 包装的 Fake（实现 get/set 满足协议） |

**等价类划分**：文件集 = 正常 + 超限混合 → 代表值 = `small.txt`（20 字节）+ `big.txt`（100 字节）、`max_bytes_per_file=64`

**Given**：
- `small = tmp_path / "small.txt"`（`"hello world"`，11 字节）；`big = tmp_path / "big.txt"`（`"y" * 100`）
- Fake working memory：`class FakeWM: get/set 读写内部 dict`；`fake_wm.set(RECENT_FILE_READS_WM_KEY, [{"path": str(small), "timestamp": 2.0}, {"path": str(big), "timestamp": 1.0}])`
- `source = RecentFilesSource(max_files=5, max_tokens_per_file=1000, total_token_budget=10000, max_bytes_per_file=64)`
- `ctx = PostCompactContext(session_id="s", run_id="r", working_memory=fake_wm, skill_registry=None, session_meta={})`

**When**：
- `attachments = source.collect(ctx)`

**Then**：
- 返回长度 = 1（big.txt 被跳过，不回注）
- `attachments[0]["source_name"] == "recent_files"`
- `"small.txt" in attachments[0]["title"]`
- `attachments[0]["content"] == "hello world"`（正常文件全文回注）
- 副作用（日志）：caplog 捕获 WARNING 含 `"big.txt"` 与 `"exceeds limit"`；INFO 记录 `"reinjected 1 file(s)"`

---

#### 用例 RFS-2：RecentFilesSource 全超限 → 空列表（E4 不崩溃）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R4 [P0] + Risk-R1 |
| 测试层级 | integration（真实文件系统 + Fake working_memory） |
| 覆盖准则 | 主循环全部 continue → attachments 为空 |
| Oracle | golden value（`[]`） |
| Mock | 否 |

**等价类划分**：文件集 = 全部超限 → 代表值 = 两个 >64 字节文件、`max_bytes_per_file=64`

**Given**：
- 两个文件各 100 字节；fake_wm 记录两路径（timestamp 2.0 / 1.0）；`RecentFilesSource(max_bytes_per_file=64, ...)`

**When**：
- `attachments = source.collect(ctx)`

**Then**：
- `attachments == []`（无附件，不抛异常）
- 无 INFO `"reinjected"` 日志（未回注任何文件）

---

#### 用例 RFS-3：两个 source 构造默认 max_bytes = 1 MiB（默认值回归）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R6 [P3] + Risk-R5 |
| 测试层级 | unit（纯构造断言，无 I/O） |
| 覆盖准则 | N/A（无分支） |
| Oracle | golden value（`DEFAULT_POST_COMPACT_MAX_BYTES_PER_FILE == 1_048_576`） |
| Mock | 否 |

**等价类划分**：构造参数 = 不传 max_bytes → 默认值断言

**Given**：
- 无前置

**When**：
- `rfs = RecentFilesSource()`；`pss = PlanStateSource()`

**Then**：
- `rfs._max_bytes_per_file == 1_048_576 == DEFAULT_POST_COMPACT_MAX_BYTES_PER_FILE`
- `pss._max_bytes == 1_048_576 == DEFAULT_POST_COMPACT_MAX_BYTES_PER_FILE`

---

#### 用例 PSS-1：PlanStateSource 正常 plan 文件 → 回注单个附件

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R5 [P1] |
| 测试层级 | integration（真实文件系统） |
| 覆盖准则 | `collect` happy 分支（读成功 + strip 非空 + truncate） |
| Oracle | golden value（content 未截断，14 字符 < 20000 字符上限） |
| Mock | 否 — session_meta 用真实 dict |

**等价类划分**：plan 文件 = 非空且未超字节/字符上限 → 代表值 = `plan.md`（`"plan body text"`，14 字符）、`max_bytes=1024`

**Given**：
- `plan = tmp_path / "plan.md"`；`plan.write_text("plan body text", encoding="utf-8")`
- `ctx = PostCompactContext(session_id="s", run_id="r", working_memory=fake_wm, skill_registry=None, session_meta={"plan_file_path": str(plan)})`
- `source = PlanStateSource(max_bytes=1024)`（max_tokens 默认 5000）

**When**：
- `attachments = source.collect(ctx)`

**Then**：
- 返回长度 = 1
- `attachments[0]["source_name"] == "plan_state"`
- `"plan.md" in attachments[0]["title"]`
- `attachments[0]["content"] == "plan body text"`（14 字符，未截断）

---

#### 用例 PSS-2：PlanStateSource plan 文件超限 → 空列表

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R5 [P0] + Risk-R1 |
| 测试层级 | integration（真实文件系统） |
| 覆盖准则 | helper 超限分支（None → return []） |
| Oracle | golden value（`[]`） |
| Mock | 否 |

**等价类划分**：plan 文件大小 vs 上限 = 超过 → 代表值 = 100 字节文件、`max_bytes=64`

**Given**：
- `plan = tmp_path / "plan.md"`；`plan.write_text("y" * 100, encoding="utf-8")`
- `ctx.session_meta = {"plan_file_path": str(plan)}`；`source = PlanStateSource(max_bytes=64)`

**When**：
- `attachments = source.collect(ctx)`

**Then**：
- `attachments == []`（超限跳过，不抛异常）
- 副作用（日志）：caplog 捕获 WARNING 含 `"exceeds limit"`

---

#### 用例 PSS-3：PlanStateSource meta 缺失 / 文件不存在 → 空列表

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R5 [P1] + Risk-R2 |
| 测试层级 | integration（真实文件系统） |
| 覆盖准则 | meta key 缺失分支 + helper stat 失败分支（两路都返回 []） |
| Oracle | golden value（`[]`） |
| Mock | 否 |

**等价类划分**：输入形态 = {meta 无 key, meta 指向不存在文件} → 代表值 = `{}`、`tmp_path/"ghost.md"`

**Given**：
- 子场景 A：`ctx.session_meta = {}`
- 子场景 B：`ctx.session_meta = {"plan_file_path": str(tmp_path / "ghost.md")}`（不存在）
- `source = PlanStateSource(max_bytes=1024)`

**When**：
- `a = source.collect(ctx_a)`；`b = source.collect(ctx_b)`

**Then**：
- `a == []`（不在 plan 模式）
- `b == []`（文件不存在，降级不崩溃）
- 副作用（日志）：子场景 B 的 caplog 捕获 INFO 含 `"cannot stat"` 或 `"failed to read"`

---

#### 用例 PSS-4：PlanStateSource 空白 plan 文件 → 空列表

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-R5 [P2]（空文件不回注的边界语义） |
| 测试层级 | integration（真实文件系统） |
| 覆盖准则 | `not raw.strip()` 分支 |
| Oracle | golden value（`[]`） |
| Mock | 否 |

**等价类划分**：plan 文件内容 = 仅空白 → 代表值 = `"   \n\t"`（strip 后为空）

**Given**：
- `plan = tmp_path / "plan.md"`；`plan.write_text("   \n\t", encoding="utf-8")`
- `ctx.session_meta = {"plan_file_path": str(plan)}`；`source = PlanStateSource(max_bytes=1024)`

**When**：
- `attachments = source.collect(ctx)`

**Then**：
- `attachments == []`（空白内容不回注）

## 6. 确定性控制与关键取舍

### 6.1 为什么 SQL-3 用 monkeypatch 而非真并发

真实并发（两个线程/进程同写一个 db 文件）触发 `IntegrityError` 依赖 WAL 读快照时序与线程调度，结果不确定（可能不冲突）→ flaky。monkeypatch 固定 `_next_seq` 返回 `1`，把"竞态已发生、两个写者拿到相同 seq"的**结果**确定性地喂给唯一索引，验证的是防线本质（重复行必须被拒）。这是本批用例唯一允许 mock 的点，理由在 SQL-3 属性表内已声明。

### 6.2 其他不确定源

| 不确定源 | 对策（已写入 Given） |
|---------|---------------------|
| token 估算 | content 全 ASCII 定长 + 默认 CharBasedTokenEstimator，token 人工可算（LDB 组） |
| 文件大小 | 纯 ASCII 写入 + 小上限值（64/1024），避免多字节编码歧义（RD/RFS/PSS 组） |
| 时间戳 | 文件记录 timestamp 由测试显式给定（2.0/1.0），不依赖真实时钟（RFS-1/2） |
| 目录 size 的平台差异 | RD-5 不依赖"目录 getsize 行为"，改用 monkeypatch open 保证 stat 成功、read 失败分支确定触发 |
| 日志 | caplog 按 logger name 前缀过滤，断言文案关键词 |

### 6.3 未覆盖维度及理由

- **故障注入（网络/第三方）**：本批被测对象无网络/第三方依赖（SQLite 嵌入式、本地文件），不适用；数据层故障（脏库 SQL-4/5）与写冲突（SQL-3）已充当等价注入。
- **ActiveSkillsSource**：非本次变更（无字节上限改动），不在范围。
- **性能/安全**：字节上限本身是 IO 防护（非功能动机），本批只测其**行为正确性**（超限即跳过），不测 IO 吞吐。

## 7. Known-Gap 记录

无。源码实现与本设计预期一致（唯一索引、RuntimeError 包装、`_read_file_with_size_limit`、两 source 的 max_bytes 参数均已就位），无需 xfail。

## 8. 衔接 test-coder 的实现提示（非代码）

1. **fixture 组织**：`tmp_path` 由 pytest 提供；SQL 组用 `SQLiteRawLogBackend(db_path=...)` 时注意每次用例独立 tmp_path（隔离库文件）。
2. **SQL-4/5 脏库构造**：裸 `sqlite3.connect` 手工建表时，DDL 必须**去掉** UNIQUE INDEX 行（否则插不进重复数据）；建表语句其余列结构照抄源码 `_SCHEMA_RAW_MESSAGES` / `_SCHEMA_COMPACT_BOUNDARIES`。
3. **日志断言**：`caplog.at_level(logging.WARNING/INFO, logger="pandaren.memory.reinject.sources")`；SQL 组异常断言用 `pytest.raises(RuntimeError, match="single writer")`、`pytest.raises(sqlite3.IntegrityError)`。
4. **FakeWM**：实现 `get(key)` / `set(key, value)` 即可满足 `WorkingMemoryAccessor` 协议（runtime_checkable 不强制 set 存在，但建议都实现）。
5. **确定性**：所有用例禁止 `sleep`、禁止 `threading` 真并发；`_next_seq` 仅 SQL-3 一处 monkeypatch。
