"""Markdown RawLogBackend — 实现 pandaren SDK 的 RawLogBackend Protocol。

以 Markdown 文件存储对话原始日志，每个 session 对应一个独立的 .md 文件。
路径由调用方显式指定，不提供默认值。

文件路径（数据隔离改造后）：
    {base_dir}/sessions/{session_id}/raw_log.md

其中 base_dir 由 StorageManager 传入时已经是 user-scoped（{data_dir}/users/{uid}/），
所以 backend 本身不再感知 user_id，符合分层：user_id 是应用层的事情。

文件内容格式：
    每条消息以 ## Turn {index} 为标题，使用 YAML front matter 风格的元数据块 +
    Markdown 正文组合。compact_boundary 使用 ## [Compact] 标记。

设计约束：
- 同步方法（符合 SDK Protocol 要求）
- 零外部依赖（仅使用 stdlib：json, os, datetime）
- 路径由调用方显式传入，不提供默认值
- backend **不知道** user_id；user_id 由上层 StorageManager 埋进 base_dir
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

# 真正的 section 头（行首）：`## Turn 5` 或 `## [Compact] Turn 5`。
# 只匹配「Turn <数字>」形式，故消息正文里的 `## 1.` / `## 搜索结果` 等
# markdown 标题不会被误判为 section 边界（web_search 结果就含大量此类标题）。
_SECTION_HEADER_RE = re.compile(r"^## (\[Compact\] )?Turn (\d+)\s*$", re.MULTILINE)

if TYPE_CHECKING:
    from pandaren.memory.models import CompactBoundaryDict, MessageDict

# 安全上限：防止极端情况 OOM（默认 5000，可由构造参数覆盖）
_DEFAULT_MAX_LOAD_MESSAGES = 5000


class MarkdownRawLogBackend:
    """pandaren SDK RawLogBackend 的 Markdown 文件实现。

    每个 session 对应一个 Markdown 文件，消息以 Markdown section 追加写入。
    支持从最新 compact_boundary 恢复。

    Args:
        base_dir: 存储根目录（**必须已经是 user-scoped**，由 StorageManager 拼装）
    """

    def __init__(
        self, base_dir: str, max_load_messages: int | None = None,
    ) -> None:
        if not base_dir:
            raise ValueError(
                "base_dir is required and cannot be empty. "
                "Markdown backend requires an explicit storage path."
            )
        self._base_dir = base_dir
        self._max_load_messages = max_load_messages or _DEFAULT_MAX_LOAD_MESSAGES
        # 会话根目录：{base_dir}/sessions/
        self._sessions_root = os.path.join(base_dir, "sessions")
        os.makedirs(self._sessions_root, exist_ok=True)

    @staticmethod
    def _sanitize(value: str) -> str:
        """清理路径组件，防止路径遍历。

        注意：冒号 `:` 也做显式替换（→ `-`），避免 `user:kkzhang` 变成 `userkkzhang`
        而与 user_id 为 `userkkzhang` 的情况冲突。
        """
        safe = value.replace("/", "_").replace("\\", "_").replace(":", "-")
        safe = "".join(c for c in safe if c.isalnum() or c in "-_.")
        return safe if safe else "unknown"

    def _get_session_path(self, session_id: str) -> str:
        """获取 session 对应的 Markdown 文件路径。

        新路径：{base_dir}/sessions/{sid}/raw_log.md
        每个 session 一个目录，便于「一 session 一次性打包/删除」。
        """
        safe_session = self._sanitize(session_id)
        session_dir = os.path.join(self._sessions_root, safe_session)
        os.makedirs(session_dir, exist_ok=True)
        return os.path.join(session_dir, "raw_log.md")

    def _meta_path(self, session_id: str) -> str:
        """session 元数据文件路径（保存原始 session_id 等）。"""
        safe_session = self._sanitize(session_id)
        return os.path.join(self._sessions_root, safe_session, "meta.json")

    def _ensure_meta(self, session_id: str) -> None:
        """确保 meta.json 存在（保存**原始** session_id，供 list_sessions 反查）。"""
        meta_path = self._meta_path(session_id)
        if os.path.exists(meta_path):
            return
        os.makedirs(os.path.dirname(meta_path), exist_ok=True)
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"session_id": session_id}, f, ensure_ascii=False)
        except OSError:
            pass

    def _get_next_index(self, session_id: str) -> int:
        """获取下一个 turn_index（通过解析文件内容）。"""
        path = self._get_session_path(session_id)
        if not os.path.exists(path):
            return 0

        max_index = -1
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("## Turn ") or line.startswith("## [Compact] Turn "):
                        # 提取 turn index
                        parts = line.strip().split("Turn ")
                        if len(parts) >= 2:
                            try:
                                idx = int(parts[-1])
                                max_index = max(max_index, idx)
                            except ValueError:
                                pass
        except OSError:
            pass

        return max_index + 1

    def append_raw_message(
        self, message: "MessageDict", session_id: str,
        run_id: str = "", step: int | None = None,
    ) -> None:
        """追加一条消息到原始日志。

        run_id / step 作为独立元数据字段落盘（不写进 message_json，保持其为纯净的
        LLM 消息），供离线分析按 (run_id, step) 与 traces 的 llm_call 做 key join。
        """
        now = datetime.now(timezone.utc).isoformat()
        turn_index = self._get_next_index(session_id)
        path = self._get_session_path(session_id)

        role = message.get("role", "unknown")
        content = message.get("content", "")

        # 会话首次写入时落一份 meta.json，保存**原始** session_id。
        # 目录名经 _sanitize 后有损（`:`/`/` 被替换），list_sessions 无法从目录名
        # 还原原始 id；meta.json 让 list_sessions 返回可反查的原始 id（与 SQLite 对齐）。
        self._ensure_meta(session_id)

        # ── 人类可读区（仅供调试阅读，**不是**解析真相源）──
        section = f"\n## Turn {turn_index}\n\n"
        section += f"- **role**: {role}\n"
        section += f"- **timestamp**: {now}\n"
        section += "- **type**: message\n"
        # run_id / step：与 traces 的 (run_id, step) 对齐的 join key。仅在提供时写出，
        # 兼容不带 run 上下文的历史/SDK 调用。step 可能为 None（run 起始的 user 消息）。
        if run_id:
            section += f"- **run_id**: {run_id}\n"
            section += f"- **step**: {'' if step is None else step}\n"
        section += "\n"

        if isinstance(content, str):
            section += f"```\n{content}\n```\n"
        else:
            section += f"```json\n{json.dumps(content, ensure_ascii=False, indent=2)}\n```\n"

        tool_calls = message.get("tool_calls")
        if tool_calls:
            section += f"\n**tool_calls**:\n```json\n{json.dumps(tool_calls, ensure_ascii=False, indent=2)}\n```\n"

        # ── 规范 JSON（解析真相源）──
        # 完整、无损地保存整个 MessageDict（与 SQLite 的 json.dumps(message) 语义一致）。
        # 单行 JSON：字符串内的换行被转义，物理上不含独立的 ``` 行，故不会破坏围栏。
        # load 时**只**读这一块 → 任意字段（含未来新增）自动保真，content 类型不被误判。
        section += f"\n**message_json**:\n```json\n{json.dumps(message, ensure_ascii=False)}\n```\n"

        # 追加写入
        with open(path, "a", encoding="utf-8") as f:
            f.write(section)

    def append_compact_boundary(
        self, boundary: "CompactBoundaryDict", session_id: str
    ) -> None:
        """追加一条压缩边界标记。"""
        now = datetime.now(timezone.utc).isoformat()
        turn_index = self._get_next_index(session_id)
        path = self._get_session_path(session_id)

        section = f"\n## [Compact] Turn {turn_index}\n\n"
        section += "- **type**: compact_boundary\n"
        section += f"- **timestamp**: {now}\n"
        section += f"- **tokens_before**: {boundary.get('tokens_before', 'N/A')}\n"
        section += f"- **tokens_after**: {boundary.get('tokens_after', 'N/A')}\n"
        section += f"- **kept_message_count**: {boundary.get('kept_message_count', 'N/A')}\n\n"
        section += "---\n"

        with open(path, "a", encoding="utf-8") as f:
            f.write(section)

    def load_within_budget(
        self, session_id: str, token_budget: int
    ) -> list["MessageDict"]:
        """从最新 compact_boundary 向后读取消息，直到 token_budget 用尽。

        返回的消息列表按时间从旧到新排列。
        """
        path = self._get_session_path(session_id)
        if not os.path.exists(path):
            return []

        # 解析文件，提取所有 sections
        sections = self._parse_sections(path)

        # 找到最后一个 compact_boundary 的位置
        last_compact_idx = -1
        for i, sec in enumerate(sections):
            if sec["type"] == "compact_boundary":
                last_compact_idx = i

        # 从 compact_boundary 之后开始读取 message 类型的 section
        start_idx = last_compact_idx + 1
        messages: list["MessageDict"] = []
        token_used = 0

        for sec in sections[start_idx:]:
            if sec["type"] != "message":
                continue
            if len(messages) >= self._max_load_messages:
                break

            msg = sec["message"]
            # 简化 token 估算（中文场景近似 1 字符 ≈ 1.5 token）
            content = msg.get("content", "")
            if isinstance(content, str):
                estimated_tokens = int(len(content) * 1.5)
            else:
                estimated_tokens = int(len(json.dumps(content)) * 1.5)

            if token_used + estimated_tokens > token_budget and messages:
                break

            messages.append(msg)
            token_used += estimated_tokens

        return messages

    def delete_turns(self, session_id: str) -> None:
        """删除指定 session 的所有日志（删除文件，含 meta.json）。"""
        path = self._get_session_path(session_id)
        if os.path.exists(path):
            os.remove(path)
        # meta.json 属于该 session 的 payload，一并清理，避免残留导致空目录无法回收
        meta_path = self._meta_path(session_id)
        if os.path.exists(meta_path):
            os.remove(meta_path)

    # ── v1.4 新增：离线分析数据源 ──

    def load_all(self, session_id: str) -> list["MessageDict"]:
        """加载指定 session 的最近 N 条历史消息（离线分析用）。

        返回所有 message 类型的 section，按 turn_index 升序排列（旧→新）。
        取「最新」N 条而非最早 N 条，避免超长会话丢最新上下文。
        """
        path = self._get_session_path(session_id)
        if not os.path.exists(path):
            return []

        sections = self._parse_sections(path)

        messages: list["MessageDict"] = []
        for sec in sections:
            if sec["type"] != "message":
                continue
            messages.append(sec["message"])

        # 取末尾（最新）N 条，保持旧→新顺序
        return messages[-self._max_load_messages:]

    def list_sessions(self) -> list[str]:
        """枚举所有已存在的 session_id。

        新路径布局下，每个 session 是 `sessions/{sid}/raw_log.md`——
        枚举 sessions/ 下的子目录名即可。
        """
        if not os.path.exists(self._sessions_root):
            return []

        sessions: list[str] = []
        for name in os.listdir(self._sessions_root):
            sub = os.path.join(self._sessions_root, name)
            if not (os.path.isdir(sub) and os.path.exists(os.path.join(sub, "raw_log.md"))):
                continue
            # 优先从 meta.json 还原**原始** session_id（sanitize 有损，目录名不可反查）；
            # 无 meta.json 的旧目录退回 sanitized 目录名。
            sid = name
            meta_path = os.path.join(sub, "meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        sid = json.load(f).get("session_id") or name
                except (OSError, json.JSONDecodeError, ValueError):
                    sid = name
            sessions.append(sid)

        return sorted(sessions)

    def search_messages(
        self, query: str, limit: int = 60
    ) -> list[tuple[str, str, str]]:
        """按关键词全文搜索消息（命令面板 ⌘K，Markdown 模式）。

        无 SQL，逐 session 加载 raw_log.md 并在正文中匹配。返回与 SQLite 版
        同构的 (session_id, content_json, timestamp)，content_json 为 MessageDict
        的 json.dumps，供调用方统一用 _extract_text 解析。

        与 SQLite 版对齐：跨 session 汇总所有命中后按 timestamp 倒序截断到 limit。
        """
        q = query.strip().lower()
        if not q:
            return []
        # (timestamp, sid, content_json)；timestamp 用于全局倒序
        hits: list[tuple[str, str, str]] = []
        for sid in self.list_sessions():
            try:
                sections = self._parse_sections(self._get_session_path(sid))
            except Exception:
                continue
            for sec in sections:
                if sec.get("type") != "message":
                    continue
                msg = sec["message"]
                content = msg.get("content", "")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts: list[str] = []
                    for blk in content:
                        if isinstance(blk, dict):
                            t = blk.get("text")
                            if isinstance(t, str):
                                parts.append(t)
                        elif isinstance(blk, str):
                            parts.append(blk)
                    text = " ".join(parts)
                else:
                    text = ""
                if q not in text.lower():
                    continue
                ts = str(sec.get("timestamp", "") or "")
                hits.append((ts, sid, json.dumps(msg, ensure_ascii=False)))

        # ISO8601 时间戳按字典序即时间序，倒序取最新的 limit 条
        hits.sort(key=lambda h: h[0], reverse=True)
        return [(sid, cjson, ts) for ts, sid, cjson in hits[:limit]]

    # ──────────────────────────────────────────────
    # 内部解析方法
    # ──────────────────────────────────────────────

    def _parse_sections(self, path: str) -> list[dict]:
        """解析 Markdown 文件中的所有 section。

        Returns:
            list of dicts with keys:
                - turn_index: int
                - type: "message" | "compact_boundary"
                - message: MessageDict (only for type="message")
        """
        sections = []

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            return []

        # 只在**真正的** section 头（行首 `## Turn N` / `## [Compact] Turn N`）处切分。
        # 用正文里的 `## 1.` 之类 markdown 标题不会误切（否则含 markdown 的工具结果
        # 会被撕碎、其 message_json 块与 Turn 头分离而整条丢失——web_search 结果即如此）。
        headers = list(_SECTION_HEADER_RE.finditer(content))
        for i, mt in enumerate(headers):
            is_compact = mt.group(1) is not None
            turn_index = int(mt.group(2))
            body = content[mt.end(): headers[i + 1].start() if i + 1 < len(headers) else len(content)]

            if is_compact:
                sections.append({
                    "turn_index": turn_index,
                    "type": "compact_boundary",
                })
                continue

            # 解析 message 内容
            msg = self._parse_message_section(body)
            if msg:
                timestamp = ""
                for line in body.split("\n"):
                    if line.startswith("- **timestamp**: "):
                        timestamp = line[len("- **timestamp**: "):].strip()
                        break
                sections.append({
                    "turn_index": turn_index,
                    "type": "message",
                    "message": msg,
                    "timestamp": timestamp,
                })

        return sections

    def _parse_message_section(self, raw: str) -> "MessageDict | None":
        """从 section 文本中解析出 MessageDict。

        唯一真相源是 **message_json** 规范块（无损，写入时由 append_raw_message 保证）。
        其上方的 plain 正文 / tool_calls 块仅供人肉调试阅读，**不参与解析**。
        无 message_json 块的 section 视为无效，返回 None。
        """
        json_raw = self._extract_code_block(raw, after_marker="**message_json**:")
        if not json_raw:
            return None
        try:
            msg = json.loads(json_raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(msg, dict) and "role" in msg:
            return msg  # type: ignore[return-value]
        return None

    @staticmethod
    def _extract_code_block(text: str, after_marker: str) -> str | None:
        """提取 after_marker 之后第一个 ``` code block 的内容。"""
        marker_pos = text.find(after_marker)
        if marker_pos == -1:
            return None
        lines = text[marker_pos:].split("\n")

        in_block = False
        block_lines: list[str] = []
        for line in lines:
            if not in_block and line.strip().startswith("```"):
                in_block = True
                continue
            if in_block and line.strip() == "```":
                return "\n".join(block_lines)
            if in_block:
                block_lines.append(line)

        # 未正常闭合 code block：返回已读取的内容
        return "\n".join(block_lines) if block_lines else None

    def close(self) -> None:
        """关闭（Markdown 后端无需显式关闭，保持接口一致）。"""
        pass
