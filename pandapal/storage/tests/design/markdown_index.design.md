# Markdown 存储层「内存索引」改造 — 测试用例设计

> 被测对象：
> - `pandapal/storage/repositories/_markdown_base.py`（基类索引基建）
> - `pandapal/storage/repositories/markdown_session_repo.py`（session 布局 / legacy 兼容 / 时间解析）
>
> 目标：证明「精确 glob 清单 + 进程内内存索引」根治了「按字段过滤 = 全目录扫描 + 误读附属大文件」，
> 且写穿透 / 懒加载 / 并发 / invalidate / legacy 兼容五项契约全部成立。

---

## 1. 分层与依赖策略

| 层级 | 用例 | 依赖处理 |
|------|------|---------|
| **unit** | U1–U9 | 纯 getter / 纯函数 / 纯内存状态；`_build_index` 用 stub 隔离 |
| **integration** | I1–I16 | 真实文件系统（`tmp_path`），**零 mock**；仅故障注入用 monkeypatch |

> 说明：本改造的核心外部 I/O 边界就是文件系统本身，因此所有「读盘构建索引 / 写穿透」用例
> 直接以 `tmp_path` 为真实依赖做 integration。无网络 / DB / MQ 等可替换协作者，故**无 component(fake) 用例**。
>
> 注：`MarkdownBaseRepository.__init__` 会执行一次 `os.makedirs`（落 `tmp_path`）。U 系列用例的
> **断言对象**（getter / 内存索引 / 控制流）均为纯逻辑，该构造副作用不影响断言，故仍归为 unit。

**Mock / Fake 决策表**

| 依赖 | 决策 | 理由 |
|------|------|------|
| 文件系统（`glob` / `open` / `os.remove`） | 真实（`tmp_path`） | 被测对象本质就是文件存储层，fake 掉 fs 会失去证明力 |
| `_build_index`（U6/U7） | stub（计数 + 慢速返回） | 隔离构建 I/O，只测 `_ensure_index` 的懒加载 / double-check 控制流 |
| `_sync_read_entity`（I1/I13） | 计数 wrapper（不改行为） | 证明「不误读附属文件」与「二次查询不读盘」 |
| `os.remove`（I12） | monkeypatch 抛 OSError | 删除故障注入，验证索引不脏 |

**确定性控制**

| 不确定源 | 对策 |
|---------|------|
| 本地时区（`astimezone()`） | U8 / I16 固定 `TZ=Asia/Shanghai`（UTC+8，无 DST）+ `time.tzset()`，测后恢复 |
| 文件系统状态 | 每个用例独立 `tmp_path`；不依赖全局数据 |
| 壁钟 | 除 I16 外不依赖真实时间；I16 用固定日期字符串，`before` 用显式 tz-aware datetime |

---

## 2. 不变式与风险清单

### 不变式（invariant）

- **inv-1 索引内容 == 磁盘记录真值**：`_build_index` 后，`_index` 的 key 集合 == 精确 glob 匹配到、且可解析为 dict 的记录文件集合；不含附属文件（raw_log / run_states / approvals / agent_tasks / meta.json），不含嵌套/非 `.md`。
- **inv-2 写穿透一致性**：索引已构建时，`_write_entity` / `_delete_entity` 成功后索引与磁盘同步；落盘/删盘失败时索引不动（保持与磁盘旧值一致）。
- **inv-3 懒加载**：构造后 `_index is None`；首次读触发一次构建；后续读不再读盘。
- **inv-4 幂等构建**：`_ensure_index` 多次 / 并发调用只构建一次（double-check + lock）。
- **inv-5 legacy 兼容**：旧布局 `{sid}.md` 可被索引与点查；`save_session` 后 legacy 文件被清理、新文件进索引、不出现重复记录。
- **inv-6 invalidate 语义**：`invalidate()` 后 `_index is None`，下次读重建并读到磁盘最新真值。
- **inv-7 路径键规范化**：索引 key 使用 `normpath`，等价路径字符串的 set/del 命中同一键。
- **inv-8 时间解析语义**：`_parse_datetime` 对 naive 本地时间补**本地时区**（非 UTC）；aware ISO 保持原 tzinfo；非法 / 空返回 `None`。

### 风险清单（按严重度排序）

| ID | 风险 | S | L | 优先级 |
|----|------|---|---|--------|
| R1 | 误纳附属大文件（raw_log 等）→ 性能回退 + 脏记录混入索引 | 高 | 高 | **P0** |
| R2 | 写后索引漂移（save/update/delete 后查询陈旧） | 高 | 高 | **P0** |
| R3 | 落盘失败却污染索引（写失败但索引已更新） | 高 | 低 | **P0** |
| R4 | 并发 `_ensure_index` 竞态（重复构建 / 部分构建 / 锁竞争） | 中 | 中 | **P1** |
| R5 | legacy 布局漏读，或 save 后 legacy 未清理（双份/丢失） | 中 | 中 | **P1** |
| R6 | invalidate 后未真正重建（返回陈旧数据） | 中 | 低 | **P1** |
| R7 | 时间比较仍按字符串 / naive 补 UTC（过期删除误判） | 中 | 中 | **P1** |
| R8 | 路径 key 未规范化导致写删不命中（幽灵记录残留） | 中 | 低 | **P2** |
| R9 | 不可解析 / 非 dict front matter 使构建崩溃或误纳 | 中 | 低 | **P2** |
| R10 | `_list_entities` 返回可变 dict 引用，被调用方原地修改导致漂移 | 低 | 低 | **P3**（当前无调用方原地修改，仅提示） |

---

## 3. Oracle 策略

| Oracle | 用于 |
|--------|------|
| golden value | glob 模式字符串（U1/U2）、内存索引状态（U3/U4/U5）、固定 TZ 下 datetime 值（U8/U9/I16）、记录集合（I1–I9/I14/I15） |
| 计数断言 | 构建次数（U6/U7）、读盘次数（I1/I13） |
| 蜕变关系 | `_parse_datetime` 的 naive 语义通过「固定 TZ 下 wall-clock 不变 + offset 正确」判别，不依赖机器本地 TZ |
| 参考实现 | 无（本改造无独立可信实现） |

> 所有 golden value 均可人工独立推导（glob 拼接、JSON front matter 字段、固定 TZ 换算），
> 不采用「跑一遍被测实现抄输出」的自指 oracle。

---

## 4. 汇总：用例 × 风险覆盖矩阵

| 用例 | R1 | R2 | R3 | R4 | R5 | R6 | R7 | R8 | R9 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| U1 基类 glob 平铺/分区契约 | ✓ | | | | | | | | |
| U2 session glob 新+legacy | | | | | ✓ | | | | |
| U3 _index_set/del None 跳过 | | ✓ | ✓ | | | | | | |
| U4 _index_set/del 已构建 + normpath | | ✓ | | | | | | ✓ | |
| U5 invalidate 清空 | | | | | | ✓ | | | |
| U6 _ensure_index 顺序只构建一次 | | | | ✓ | | | | | |
| U7 _ensure_index 并发只构建一次 | | | | ✓ | | | | | |
| U8 _parse_datetime naive→本地 | | | | | | | ✓ | | |
| U9 _parse_datetime aware/非法/空 | | | | | | | ✓ | | |
| I1 session 每sid索引构建正确 | ✓ | | | | | | | | |
| I2 legacy 平铺被索引(混合) | | | | | ✓ | | | | |
| I3 基类平铺非递归 | ✓ | | | | | | | | |
| I4 分区布局只索引本实体 | ✓ | | | | | | | | |
| I5 不可解析/非dict跳过 | | | | | | | | | ✓ |
| I6 save 写穿透 | | ✓ | | | | | | | |
| I7 update 写穿透 | | ✓ | | | | | | | |
| I8 soft_delete 写穿透 | | ✓ | | | | | | | |
| I9 hard delete 写穿透 | | ✓ | | | | | | | |
| I10 写于索引未构建→首次构建含写 | | ✓ | | | | | | | |
| I11 写失败索引不变 | | | ✓ | | | | | | |
| I12 删失败索引不变 | | | ✓ | | | | | | |
| I13 懒加载读盘计数 | ✓ | | | | | | | | |
| I14 legacy 端到端 | | | | | ✓ | | | | |
| I15 invalidate 重建 | | | | | | ✓ | | | |
| I16 delete_expired 本地时区边界 | | | | | | | ✓ | | |

---

## 5. 用例设计

### 单元测试（unit）

#### U1：基类 `_record_glob_patterns` 平铺/分区契约

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 索引内容 [P0] + R1 误纳附属文件 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: `session_partitioned` True/False 两分支 |
| Oracle | golden value（glob 拼接可人工推导） |
| Mock | 否 — 纯 getter 无 I/O |

**等价类划分**：`session_partitioned ∈ {False, True}` → 代表值 `False`、`True`

**Given**：
- `base = tmp_path`
- `flat = MarkdownBaseRepository(str(base), "devices")`（`session_partitioned=False`）
- `part = MarkdownBaseRepository(str(base), "run_states", session_partitioned=True)`

**When**：
- `flat._record_glob_patterns()`
- `part._record_glob_patterns()`

**Then**：
- `flat` 返回 `[os.path.join(str(base), "devices", "*.md")]`
- `part` 返回 `[os.path.join(str(base), "sessions", "*", "run_states", "*.md")]`（等于 `_entity_glob_pattern()` 输出）
- 副作用：无（构造时的 `makedirs` 落 `tmp_path`，与断言无关）

---

#### U2：session `_record_glob_patterns` 覆盖为「新布局 + legacy」

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 legacy 兼容 [P1] + R5 legacy 漏读 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: session 子类 override 覆盖基类默认 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：session 布局 ∈ {新布局, legacy} → 两个 glob 元素都要有

**Given**：`base = tmp_path`；`repo = MarkdownSessionRepository(str(base))`（`entity_dir = base/sessions`）

**When**：`repo._record_glob_patterns()`

**Then**：返回集合为
- `os.path.join(str(base), "sessions", "*", "session.md")`（新布局：每 sid 一目录）
- `os.path.join(str(base), "sessions", "*.md")`（legacy 平铺兼容）
- 副作用：无

---

#### U3：`_index_set` / `_index_del` 在索引为 None 时跳过

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 写穿透一致性 [P0] + R2/R3 漂移/污染 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: `if self._index is None: return` 两方法 |
| Oracle | golden value（None 状态保持） |
| Mock | 否 |

**Given**：`repo = MarkdownBaseRepository(str(tmp_path), "devices")`，此时 `repo._index is None`（未触发任何查询/构建）

**When**：
- `repo._index_set("/tmp/x.md", {"a": 1})`
- `repo._index_del("/tmp/x.md")`

**Then**：
- `repo._index` 仍为 `None`（不抛异常、不创建 dict）
- 副作用：无

---

#### U4：`_index_set` / `_index_del` 已构建时更新 + normpath 规范化

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 写穿透一致性 + inv-7 路径键规范化 + R8 幽灵记录 [P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: `_index is not None` 增删路径；等价路径命中同一 key |
| Oracle | golden value（内存 dict 状态可推导） |
| Mock | 否 |

**等价类划分**：路径表示 ∈ {含 `..`/`./` 的非规范路径, 规范路径} → 代表值
`p = "/tmp/base/sessions/s1/../s1/session.md"`（normpath = `/tmp/base/sessions/s1/session.md`）

**Given**：`repo._index = {}`（模拟已构建）

**When**：
- `repo._index_set(p, {"session_id": "s1"})`
- `repo._index_del("/tmp/base/sessions/s1/session.md")`

**Then**：
- set 后 `repo._index == {"/tmp/base/sessions/s1/session.md": {"session_id": "s1"}}`（key 已 `normpath`）
- del 后 `repo._index == {}`（用规范化等价路径可删除；`pop` 缺 key 不抛异常）
- 副作用：无

---

#### U5：`invalidate()` 清空索引

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 invalidate 语义 + R6 未重建 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 单语句，无分支 |
| Oracle | golden value |
| Mock | 否 |

**Given**：`repo._index = {"k": {"a": 1}}`（已构建）

**When**：`repo.invalidate()`

**Then**：`repo._index is None`

---

#### U6：`_ensure_index` 顺序调用只构建一次（fast path）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 懒加载 + inv-4 幂等构建 + R4 并发竞态 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: `if self._index is not None: return` fast path |
| Oracle | 计数断言（构建次数） |
| Mock | 是 — stub `_build_index`，理由：隔离构建 I/O，只测控制流 |

**Given**：
- `repo = MarkdownSessionRepository(str(tmp_path))`（不触发查询）
- `calls = 0`；monkeypatch `repo._build_index = async fake()`：`calls += 1; return {"k": {"session_id": "s1"}}`

**When**：
- `await repo._ensure_index()`（第 1 次）
- `await repo._ensure_index()`（第 2 次）

**Then**：
- 第 1 次后 `repo._index == {"k": {"session_id": "s1"}}`、`calls == 1`
- 第 2 次后 `calls` 仍为 `1`（走 fast path，未再构建）

---

#### U7：`_ensure_index` 并发调用只构建一次（double-check + lock）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 幂等构建 + R4 并发竞态 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: fast path + lock 内 double-check 两路径（慢速 stub 制造竞争窗口） |
| Oracle | 计数断言 |
| Mock | 是 — stub 慢速 `_build_index`，理由：制造真实竞争窗口 |

**Given**：
- `repo._index = None`
- `calls = 0`；stub `_build_index`：`calls += 1; await asyncio.sleep(0.05); return {"k": {"session_id": "s1"}}`

**When**：`await asyncio.gather(*[repo._ensure_index() for _ in range(50)])`

**Then**：
- `calls == 1`（double-check + `_index_lock` 保证只构建一次）
- `repo._index is not None` 且内容为 stub 返回值

---

#### U8：`_parse_datetime` naive 本地时间 → aware 本地时区

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 时间解析语义 + R7 时间比较误判 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: `fromisoformat` 失败 → `strptime` 成功 → naive `astimezone()` |
| Oracle | golden value（固定 TZ 下可人工推导） |
| Mock | 否 |

**等价类划分**：时间字符串格式 ∈ {naive `%Y-%m-%d %H:%M:%S`} → 代表值 `"2024-01-01 12:00:00"`

**Given**（确定性控制）：
- 固定 `os.environ["TZ"] = "Asia/Shanghai"` 并 `time.tzset()`（UTC+8，无 DST），测后恢复

**When**：`result = MarkdownSessionRepository._parse_datetime("2024-01-01 12:00:00")`

**Then**：
- `result == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))`
- 判别性：若 naive 被补 UTC，`result.utcoffset()` 会是 `0` → 断言失败，证明本地时区修复生效

---

#### U9：`_parse_datetime` aware ISO 不变 + 非法/空 → None

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 时间解析语义 |
| 测试层级 | unit |
| 覆盖准则 | branch: aware ISO 直接返回；`fromisoformat` 与 `strptime` 双失败 → None；空值 → None |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：输入 ∈ {aware ISO, 非法字符串, 空串, None} → 代表值
`"2024-01-01T12:00:00+00:00"` / `"not-a-date"` / `""` / `None`

**When**：分别调用 `_parse_datetime(...)`

**Then**：
- aware ISO → `datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)`（tzinfo 保持 UTC，未被 relocalize）
- 非法、`""`、`None` 均返回 `None`

---

### 集成测试（integration，真实文件系统 tmp_path）

> 说明：以下用例的 front matter 文件统一用 JSON 格式（`_write_entity` 的产出格式）：
> `---\n{json}\n---`。手写 fixture 时保证 `session_id` / `run_id` 等关键字段非空。
>
> 统一约定：除 I1（`base = tmp_path / "md"`）外，其余 integration 用例 Given 中的 `base` 均指 `tmp_path`，
> 路径前缀 `sessions/` / `devices/` 为相对 `base` 的简写，实现时用 `base / "sessions" / ...` 拼接。

#### I1：session 每 sid 一目录索引构建正确（排除附属文件 + 读盘计数）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 索引内容 + R1 误纳附属大文件 [P0] |
| 测试层级 | integration |
| 覆盖准则 | `_build_index` 两个 glob 的精确匹配；`_sync_read_entity` 只读 record 文件 |
| Oracle | golden value（记录集合可人工列举）+ 计数 |
| Mock | 否（真实 fs）；`_sync_read_entity` 用计数 wrapper（不改行为） |

**Given**：
- `base = tmp_path / "md"`；`repo = MarkdownSessionRepository(str(base))`
- 创建：
  - `sessions/s1/session.md` → `{"session_id":"s1","user_id":"u1"}`
  - `sessions/s2/session.md` → `{"session_id":"s2","user_id":"u1"}`
  - `sessions/s1/raw_log.md`（大文本，非 record）
  - `sessions/s1/run_states/r1.md`、`sessions/s1/approvals/a1.md`、`sessions/s1/agent_tasks/t1.md`
  - `sessions/s1/meta.json`
- 用计数 wrapper 替换 `repo._sync_read_entity`（调用真实逻辑 + 计数）

**When**：`await repo._list_entities()`

**Then**：
- 返回记录的 `session_id` 集合 == `{"s1", "s2"}`（2 条）
- `repo._index` 的所有 key 经 `normpath` 后均以 `session.md` 结尾
- 索引不含 raw_log / run_states / approvals / agent_tasks / meta.json 对应路径
- `_sync_read_entity` 被调用次数 == 2（只读 2 个 `session.md`，附属文件**零读取**）

---

#### I2：legacy 平铺 `{sid}.md` 被索引（含混合布局）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 legacy 兼容 + R5 legacy 漏读 [P1] |
| 测试层级 | integration |
| 覆盖准则 | `_record_glob_patterns` 第二 glob `{entity_dir}/*.md` 命中 legacy |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `repo = MarkdownSessionRepository(str(base))`
- 创建 `sessions/s_legacy.md` → `{"session_id":"s_legacy","user_id":"u1"}`
- 创建 `sessions/s_new/session.md` → `{"session_id":"s_new","user_id":"u1"}`

**When**：`await repo._list_entities()`

**Then**：
- 返回记录的 `session_id` 集合 == `{"s_legacy", "s_new"}`（legacy 由 `{entity_dir}/*.md` 命中，新布局由 `{entity_dir}/*/session.md` 命中）

---

#### I3：基类平铺布局非递归（嵌套 `.md` 排除）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 索引内容 + R1 误纳 [P0] |
| 测试层级 | integration |
| 覆盖准则 | 默认 glob `{entity_dir}/*.md` 不递归子目录 |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `repo = MarkdownBaseRepository(str(base), "devices")`
- 创建：
  - `devices/d1.md` → `{"device_id":"d1"}`
  - `devices/nested/d2.md` → `{"device_id":"d2"}`（嵌套子目录，应被排除）
  - `devices/notes.txt`（非 `.md`，排除）

**When**：`await repo._list_entities()`

**Then**：
- 返回长度 1，`[0]["device_id"] == "d1"`
- 嵌套 `d2.md` 与非 `.md` 均不在索引

---

#### I4：分区布局只索引本实体 `*.md`

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 索引内容 + R1 误纳 [P0] |
| 测试层级 | integration |
| 覆盖准则 | `session_partitioned=True` 分支 → `_entity_glob_pattern()` 精确匹配 |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `repo = MarkdownBaseRepository(str(base), "run_states", session_partitioned=True)`
- 创建：
  - `sessions/s1/run_states/r1.md` → `{"run_id":"r1","session_id":"s1"}`
  - `sessions/s1/session.md`、`sessions/s1/raw_log.md`、`sessions/s1/approvals/a1.md`

**When**：`await repo._list_entities()`

**Then**：
- 返回长度 1，`[0]["run_id"] == "r1"`
- `session.md` / `raw_log.md` / `approvals` 均不在索引

---

#### I5：不可解析 / 非 dict front matter 跳过且不崩溃

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 解析崩溃/误纳 [P2] |
| 测试层级 | integration |
| 覆盖准则 | `_sync_read_entity` 各失败分支：parse 失败 / 非 dict / 无 `---` / 空 dict |
| Oracle | golden value（跳过行为） |
| Mock | 否 |

**Given**：
- `repo = MarkdownBaseRepository(str(base), "devices")`
- 创建：
  - `devices/good.md` → `{"device_id":"good"}`
  - `devices/bad_json.md` → `---\n{invalid json\n---`（parse 失败）
  - `devices/list_fm.md` → `---\n[1,2,3]\n---`（非 dict）
  - `devices/no_fm.md` → 纯文本无 `---`
  - `devices/empty_dict.md` → `---\n{}\n---`（空 dict）

**When**：`await repo._list_entities()`

**Then**：
- 不抛异常；返回长度 1，仅 `good` 在索引
- `bad_json` / `list_fm` / `no_fm` 因 `_sync_read_entity` 返回 `None` 被跳过
- `empty_dict` 因 `{}` falsy 被 `if data:` 跳过（当前实现行为；若未来需索引空 dict 记录，改判定为 `if data is not None` 即可，见 known-gap）

---

#### I6：save_session 写穿透（新值可查 + 磁盘一致）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 写穿透一致性 + R2 写后漂移 [P0] |
| 测试层级 | integration |
| 覆盖准则 | `_write_entity` 成功后 `_index_set` 命中已构建索引 |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `repo = MarkdownSessionRepository(str(base))`
- 先 `await repo._list_entities()` 触发空索引构建（`_index` 非 None）
- `now = datetime.now(timezone.utc)`

**When**：
- `await repo.save_session(Session(session_id="s1", user_id="u1", device_id="d1", last_active=now, created_at=now))`

**Then**：
- 再次 `await repo._list_entities()` 长度 1，`[0]["session_id"] == "s1"`（索引同步更新，无需重建）
- `await repo.find_sessions_by_user("u1")` 返回 `s1`
- 磁盘 `sessions/s1/session.md` 存在，front matter 与写入 data 一致
- `repo._index` 含 `normpath(sessions/s1/session.md)` 键

---

#### I7：update_session_meta 写穿透（title 更新命中）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 写穿透一致性 + R2 写后漂移 [P0] |
| 测试层级 | integration |
| 覆盖准则 | 已构建索引上 in-place `_index_set` 更新 |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `repo = MarkdownSessionRepository(str(base))`
- `await repo.save_session(Session(session_id="s1", user_id="u1", device_id="d1", title="old", last_active=now, created_at=now))`
- `await repo._list_entities()`（**先触发索引构建**，使后续 update 走 in-place 更新路径）

**When**：`await repo.update_session_meta("s1", title="New Title")`

**Then**：
- `await repo.find_sessions_by_user("u1")` 返回的 `s1.title == "New Title"`
- 磁盘 `sessions/s1/session.md` front matter `title == "New Title"`（索引与磁盘一致）

---

#### I8：soft_delete 写穿透（count_visible_sessions 减一）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 写穿透一致性 + R2 写后漂移 [P0] |
| 测试层级 | integration |
| 覆盖准则 | `soft_delete` 内部 `_write_entity` 更新 `is_deleted` → 索引即时可见 |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `repo = MarkdownSessionRepository(str(base))`
- 保存 s1、s2（均 `is_empty=False`、`is_deleted=False`，同用户 u1）
- `await repo.count_visible_sessions("u1")` == 2（首次查询同时触发索引构建）

**When**：`await repo.soft_delete_session("s1")`

**Then**：
- `await repo.count_visible_sessions("u1")` == 1
- `await repo.find_session("s1")` 的 `is_deleted is True`

---

#### I9：hard delete_session 写穿透（索引与磁盘同时移除）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 写穿透一致性 + R2 写后漂移 [P0] |
| 测试层级 | integration |
| 覆盖准则 | `_delete_entity` 成功 → `_index_del` 命中已构建索引 |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `repo = MarkdownSessionRepository(str(base))`
- 保存 s1、s2；`await repo._list_entities()` 触发索引构建（含 s1、s2）

**When**：`await repo.delete_session("s1")`

**Then**：
- `await repo._list_entities()` 长度 1，仅 `s2`
- `repo._index` 不含 `normpath(sessions/s1/session.md)`
- 磁盘 `sessions/s1/session.md` 不存在

---

#### I10：索引未构建时写 → 首次查询从磁盘构建包含该写

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 写穿透一致性 + inv-3 懒加载 |
| 测试层级 | integration |
| 覆盖准则 | `_index_set` 的 `if self._index is None: return` 分支（写不碰索引） |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `repo = MarkdownSessionRepository(str(base))`（**不触发任何查询**，`_index is None`）

**When**：
- `await repo.save_session(Session(session_id="s1", user_id="u1", device_id="d1", last_active=now, created_at=now))`
- `await repo._list_entities()`（首次查询）

**Then**：
- 返回包含 s1（`_build_index` 读磁盘真值，写未丢失）
- `repo._index` 非 None 且含 s1

---

#### I11：写失败 → 索引不变（fault injection）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 写穿透一致性 + R3 落盘失败污染 [P0] |
| 测试层级 | integration |
| 覆盖准则 | `_write_entity` 落盘 raise 时 `_index_set` 不可达 |
| Oracle | golden value（索引保持旧值） |
| Mock | 否（用真实文件系统制造 `os.makedirs` 失败） |

**Given**：
- `repo = MarkdownBaseRepository(str(base), "devices")`
- 写入并索引 `devices/d1.md` → `{"device_id":"d1"}`；`await repo._list_entities()` 触发索引（`_index` 含 d1）
- 制造故障：在 `devices/` 下创建**名为 `blocked` 的文件**，使目标路径 `devices/blocked/d2.md` 的父目录是文件

**When**：
- `await repo._write_entity(str(base / "devices" / "blocked" / "d2.md"), {"device_id": "d2"})` → 期望抛异常

**Then**：
- 抛 `FileExistsError`（或 `OSError`）
- `repo._index` 仍只含 d1（`_index_set` 未执行，索引保持旧值 == 磁盘旧值）
- 磁盘不存在 `devices/blocked/d2.md`

---

#### I12：删失败 → 索引不变（fault injection）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 写穿透一致性 + R3 落盘失败污染 [P0] |
| 测试层级 | integration |
| 覆盖准则 | `_delete_entity` 异常捕获 → 返回 False → `_index_del` 不可达 |
| Oracle | golden value |
| Mock | 是 — monkeypatch `os.remove` 抛 `OSError`，理由：注入删除故障 |

**Given**：
- `repo = MarkdownBaseRepository(str(base), "devices")`
- 写入并索引 `devices/d1.md` → `{"device_id":"d1"}`（`_index` 含 d1）
- monkeypatch `os.remove` 抛 `OSError("disk error")`

**When**：`result = await repo._delete_entity(str(base / "devices" / "d1.md"))`

**Then**：
- `result is False`（异常被捕获，返回 False）
- `repo._index` 仍含 d1（`_index_del` 未执行）
- 磁盘 `devices/d1.md` 仍存在

---

#### I13：懒加载读盘计数（首次读、二次不读）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 懒加载 + R1 误纳附属文件 [P0] |
| 测试层级 | integration |
| 覆盖准则 | 构造后 `_index None`；首次 `_list_entities` 触发构建；二次走 fast path |
| Oracle | 计数断言 |
| Mock | 否（真实 fs）；`_sync_read_entity` 用计数 wrapper |

**Given**：
- `repo = MarkdownSessionRepository(str(base))`
- 创建 `sessions/s1/session.md`、`sessions/s2/session.md`、`sessions/s1/raw_log.md`
- 用计数 wrapper 包裹 `_sync_read_entity`
- 断言 `repo._index is None`（构造后未构建）

**When**：
- 第 1 次 `await repo._list_entities()`
- 记录 `reads_after_first = count`
- 第 2 次 `await repo._list_entities()`

**Then**：
- `reads_after_first == 2`（只读 2 个 record，不含 raw_log）
- 第 2 次后 `count == reads_after_first`（无新增读盘）
- `repo._index is not None`

---

#### I14：legacy 端到端（旧文件可查 + save 清理 legacy + 新文件进索引）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 legacy 兼容 + R5 legacy 未清理 [P1] |
| 测试层级 | integration |
| 覆盖准则 | `_read_session_entity` 回退 legacy；`save_session` 后 `_delete_legacy_session_file` 清 legacy + 新路径进索引 |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `repo = MarkdownSessionRepository(str(base))`
- 手动创建旧布局 `sessions/s_old.md` → `{"session_id":"s_old","user_id":"u1","is_empty":false,"is_deleted":false,"title":"legacy"}`

**When**：
- `await repo.find_session("s_old")`（点查回退 legacy）
- `await repo.find_sessions_by_user("u1")`（索引含 legacy）
- `await repo.save_session(Session(session_id="s_old", user_id="u1", device_id="d1", last_active=now, created_at=now))`

**Then**：
- save 前：`find_session("s_old")` 非 None；`find_sessions_by_user("u1")` 含 s_old
- save 后：legacy 文件 `sessions/s_old.md` 被删除；新文件 `sessions/s_old/session.md` 存在
- `await repo._list_entities()` 中 s_old 只出现一次（无 legacy 残留重复），且 key 为新路径

---

#### I15：invalidate 后重建读到磁盘新值

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 invalidate 语义 + R6 未重建 [P1] |
| 测试层级 | integration |
| 覆盖准则 | `invalidate()` 清空 → 下次 `_list_entities` 重建读磁盘真值 |
| Oracle | golden value（外部改写的值已知） |
| Mock | 否 |

**Given**：
- `repo = MarkdownSessionRepository(str(base))`
- 保存 s1（title="old"）；`await repo._list_entities()` 触发索引
- 绕过 repo 直接改写磁盘 `sessions/s1/session.md` 的 front matter title="new"（模拟外部手改）

**When**：
- `await repo._list_entities()` → s1 title 仍 "old"（索引漂移，符合文档约束）
- `repo.invalidate()`
- `await repo._list_entities()`

**Then**：
- `repo.invalidate()` 后 `repo._index is None`
- 重建后 s1 `title == "new"`（从磁盘读到新值）

---

#### I16：delete_expired_sessions 本地时区边界（naive 本地语义判别）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 时间解析语义 + R7 时间比较误判 [P1] |
| 测试层级 | integration |
| 覆盖准则 | `delete_expired_sessions` 的 datetime 比较（naive 补本地时区） |
| Oracle | golden value（固定 TZ 下边界可推导） |
| Mock | 否 |

**等价类划分**：`last_active` 存储格式 ∈ {naive 本地 `%Y-%m-%d %H:%M:%S`}，`before` 为 aware UTC；
取一个「本地 vs UTC 解释会翻转过期判断」的边界值

**Given**（确定性控制）：
- 固定 `os.environ["TZ"] = "Asia/Shanghai"` 并 `time.tzset()`（UTC+8，无 DST），测后恢复
- `repo = MarkdownSessionRepository(str(base))`
- 直接写两个 session 文件（绕过 save，控制 `last_active` 为 naive 本地格式）：
  - `sessions/s_expired/session.md` → `{"session_id":"s_expired","user_id":"u1","last_active":"2024-01-01 12:00:00"}`
  - `sessions/s_keep/session.md` → `{"session_id":"s_keep","user_id":"u1","last_active":"2024-01-01 20:00:00"}`
- `before = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)`（= 16:00 上海）

**When**：`deleted = await repo.delete_expired_sessions(before)`

**Then**：
- `deleted == 1`
- `s_expired` 被删（12:00 上海 = 04:00 UTC < 08:00 UTC → 过期）
- `s_keep` 保留（20:00 上海 = 12:00 UTC >= 08:00 UTC → 未过期）
- 判别性：若 naive 被补 UTC，`"2024-01-01 12:00:00"` 会被当 12:00 UTC >= 08:00 UTC → s_expired 不删，`deleted == 0` → 测试失败，证明本地时区语义修复生效

---

## 6. 覆盖准则声明

| 被测方法 | 目标准则 | 说明 |
|---------|---------|------|
| `_record_glob_patterns` | 分支覆盖 | True/False + session override 全部走到 |
| `_ensure_index` | 分支覆盖 | fast path + lock 内 double-check（U6/U7） |
| `_build_index` | 分支覆盖 | 空目录、单/多 record、parse 失败、非 dict、空 dict（I1/I3/I4/I5） |
| `_sync_read_entity` | 分支覆盖 | JSON 成功 / YAML 回退 / parse 失败 / 非 dict / 无 `---` / 文件不存在（I1/I5） |
| `_index_set` / `_index_del` | 分支覆盖 | `_index is None` 跳过 / 非 None 增删（U3/U4） |
| `_write_entity` / `_delete_entity` | 分支覆盖 | 成功路径 + 失败路径（I6/I7/I8/I9/I11/I12） |
| `_parse_datetime` | 分支覆盖 | naive 本地 / aware ISO / 非法 / 空（U8/U9） |
| `delete_expired_sessions` | 分支覆盖 | 无 session_id / last_active None / 过期 / 未过期（I16） |

---

## 7. Known-Gap 清单

| 用例 | 期望行为 | 实际现状 | 原因 |
|------|---------|---------|------|
| I5 | 空 dict front matter 是否应入索引？ | 当前不入索引（`_build_index` 用 `if data:` falsy 判断） | 正常 session 记录必有 `session_id`，空 dict 属退化/损坏文件，当前视为合理排除；若未来需支持，改为 `if data is not None` |
| —（非本次引入） | 点查 `_read_entity` 与索引 `_sync_read_entity` 对非 dict front matter 的返回值应一致 | `_read_entity` 返回 `{}`，`_sync_read_entity` 返回 `None` | 历史遗留不一致，非本次改造引入；影响面极小（仅损坏文件点查 vs 列表可见性差异），建议后续统一 |

---

## 8. 豁免声明

- **性能/负载测试**：不在本设计范围（索引构建 O(记录数) 的性能预期由 §6 覆盖准则的读盘计数间接证明，不单独压测）。
- **安全测试**：`_sanitize_id` 路径遍历防护非本次改动范围，不在本设计覆盖。
- **R10（可变引用漂移）**：当前无调用方原地修改 `_list_entities` 返回的 dict，仅作风险提示，不单列用例。
