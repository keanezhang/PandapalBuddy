"""pandapal.session_id — session_id 单一真相源（命根子）。

★ 刻意放在顶层 pandapal 包（而非 pandapal.session 子包）：本模块零重依赖，
  任何层都可安全 `from pandapal import session_id` 导入，不会触发 storage/aiosqlite
  等重依赖的加载，也不会引入循环导入。

session_id 是「数据归属」的唯一凭证：凭证一旦伪造 / 丢失 / 被替代，数据就跨会话污染。
因此**所有 session_id 的「创建 / 校验 / 一致性断言」必须经由本模块**，严禁在业务代码里
散落 uuid 生成、空值兜底（`or ""` / `or "unknown"`）、跨 id 替代（`session_id or other_id`）。

完整契约见 CLAUDE.md「SESSION_ID 契约」及 docs/session-id-契约.md。核心红线：
  1. 创建权只属于「发起方」，消费方（中间层/工具/resume）只读透传，绝不创建。
  2. 使用时 0 容忍空值 —— 空即报错（`require`）。
  3. 有权威记录时必须「相等」才放行（`assert_consistent` / RunState 复合键）。
  4. 绝不降级、绝不用另一个语义不同的 id 兜底替代。
  5. 违反必留痕（本模块抛 SessionIdError，调用方须记录/广播，不得静默吞掉）。

本模块**无任何重依赖**（仅 uuid），可被任意层安全导入，不会引入循环依赖。
"""

from __future__ import annotations

import uuid

# ── 前缀：按「发起方」分类，一眼辨识归属与隔离边界 ──────────────────────────────
PREFIX_INTERACTIVE = "sess-"   # 前端交互会话（用户点新增 / 首消息 / Rust 冷启动 mint）
PREFIX_TASK = "task-"          # 定时 / 自主任务的隔离会话（绝不复用用户交互会话）

_MAX_LEN = 200


class SessionIdError(ValueError):
    """session_id 违反契约：为空 / 格式非法 / 两层不一致。

    调用方捕获后必须留痕（log/audit/广播 error 事件），不得静默吞掉——
    静默的降级比崩溃更可怕（崩溃看得见，污染看不见）。
    """


# ── 创建（只有发起方能调用）────────────────────────────────────────────────────

def new_interactive() -> str:
    """新建一个前端交互会话 id。仅供会话生命周期 Owner（SessionListManager）调用。"""
    return f"{PREFIX_INTERACTIVE}{uuid.uuid4().hex}"


def new_task(task_id: str = "", execution_id: str = "") -> str:
    """新建一个定时/自主任务的**隔离**会话 id。

    用 task_id + execution_id 派生稳定 id：同一次执行的日志聚在一处，又与任何
    用户可见会话物理隔离，避免污染用户实时会话的记忆/raw_log。
    """
    if task_id and execution_id:
        return f"{PREFIX_TASK}{task_id}-{execution_id}"
    if execution_id:
        return f"{PREFIX_TASK}{execution_id}"
    return f"{PREFIX_TASK}{uuid.uuid4().hex[:12]}"


# ── 校验 / 断言（所有消费方入口必须调用）──────────────────────────────────────

def is_wellformed(sid: object) -> bool:
    """结构是否合法：非空 str、无首尾空白、长度受限。不校验前缀（兼容遗留格式）。"""
    return (
        isinstance(sid, str)
        and sid != ""
        and sid.strip() == sid
        and len(sid) <= _MAX_LEN
    )


def require(sid: object, *, where: str) -> str:
    """0 容忍非空校验：为空 / 非法即抛 SessionIdError，绝不返回兜底值。

    Args:
        where: 调用点标识（用于报错定位），如 "stdio_ipc.SEND_MESSAGE"。
    """
    if not is_wellformed(sid):
        raise SessionIdError(f"[{where}] session_id 非法或为空: {sid!r}")
    return sid  # type: ignore[return-value]  # is_wellformed 已保证是 str


def assert_consistent(primary: object, secondary: object, *, where: str) -> str:
    """从「同一真相的两层信封」取值（如 content.session_id 与 msg 头 session_id）。

    ★ 这是唯一被允许的「二选一」：二者本应是同一个 session 的不同承载层。
      —— 都存在则**必须相等**（不等即污染，抛错）；
      —— 只有一个则取那个；
      —— 都没有则抛错。
    它 **不是** 用另一个语义不同的 id 兜底替代（那是被禁止的降级）。

    Returns:
        权威 session_id（primary 优先）。
    """
    p = primary if is_wellformed(primary) else None
    s = secondary if is_wellformed(secondary) else None
    if p is not None and s is not None and p != s:
        raise SessionIdError(
            f"[{where}] session_id 两层不一致: primary={p!r} secondary={s!r}"
        )
    return require(p if p is not None else s, where=where)
