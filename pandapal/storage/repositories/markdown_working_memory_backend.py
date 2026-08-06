"""Markdown WorkingMemoryBackend — 实现 pandaren SDK 的 WorkingMemoryBackend Protocol。

以 Markdown 文件存储 WorkingMemory 的 KV 条目，每个 session 对应一个 .md 文件。
路径由调用方显式指定，不提供默认值。

文件格式：
    {base_dir}/working_memory/{user_id}/{session_id}.md

文件内容格式（YAML front matter + Markdown body）：
    ---
    {"key1": {"json": "value1"}, "key2": "value2"}
    ---

    # Working Memory: {session_id}

    - **key1**: value1
    - **key2**: value2

设计约束：
- 同步方法（符合 SDK WorkingMemoryBackend Protocol 要求）
- 零外部依赖（仅使用 stdlib：json, os, datetime）
- 路径由调用方显式传入，不提供默认值
- user_id 在构造时绑定，数据按用户隔离
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("pandapal.storage.repositories.markdown_working_memory_backend")


class MarkdownWorkingMemoryBackend:
    """pandaren SDK WorkingMemoryBackend 的 Markdown 文件实现。

    每个 session 对应一个 Markdown 文件，KV 条目以 JSON 格式存储在
    YAML front matter 中，同时生成人类可读的 Markdown body。

    user_id 在构造时绑定，数据按用户隔离：
      {base_dir}/working_memory/{user_id}/{session_id}.md

    Args:
        base_dir: 存储根目录（必须由调用方显式指定）
        user_id:  用户标识，用于数据隔离
    """

    def __init__(self, base_dir: str) -> None:
        if not base_dir:
            raise ValueError(
                "base_dir is required and cannot be empty. "
                "Markdown backend requires an explicit storage path."
            )
        self._base_dir = base_dir
        self._sessions_root = os.path.join(base_dir, "sessions")
        os.makedirs(self._sessions_root, exist_ok=True)

    @staticmethod
    def _sanitize(value: str) -> str:
        """清理路径组件，防止路径遍历。"""
        safe = value.replace("/", "_").replace("\\", "_").replace(":", "-")
        safe = "".join(c for c in safe if c.isalnum() or c in "-_.")
        return safe if safe else "unknown"

    def _get_session_path(self, session_id: str) -> str:
        """获取 session 对应的 Markdown 文件路径。

        新路径：{base_dir}/sessions/{sid}/working_memory.md
        与 raw_log.md 并排在同一 session 目录下，一 session 一次性打包/删除。
        """
        safe_session = self._sanitize(session_id)
        session_dir = os.path.join(self._sessions_root, safe_session)
        os.makedirs(session_dir, exist_ok=True)
        return os.path.join(session_dir, "working_memory.md")

    # ─────────────────────────────────────────
    # WorkingMemoryBackend Protocol 方法
    # ─────────────────────────────────────────

    def save(self, key: str, value: Any, session_id: str) -> None:
        """持久化单个 KV 条目（增量更新）。"""
        if not session_id:
            return

        data = self._load_data(session_id)
        data[key] = value
        self._save_data(session_id, data)

    def load(self, session_id: str) -> dict[str, Any]:
        """加载指定 session 的所有 KV 条目，返回 {key: value} 字典。"""
        if not session_id:
            return {}
        return self._load_data(session_id)

    def delete_key(self, key: str, session_id: str) -> None:
        """删除单个 KV 条目。"""
        if not session_id:
            return

        data = self._load_data(session_id)
        if key in data:
            del data[key]
            if data:
                self._save_data(session_id, data)
            else:
                # 全部删完，删除文件
                path = self._get_session_path(session_id)
                if os.path.exists(path):
                    os.remove(path)

    def delete_session(self, session_id: str) -> None:
        """删除指定 session 的所有条目。"""
        if not session_id:
            return

        path = self._get_session_path(session_id)
        if os.path.exists(path):
            os.remove(path)

    def save_all(self, data: dict[str, Any], session_id: str) -> None:
        """一次性保存整个 WorkingMemory 快照（覆盖写入）。"""
        if not session_id:
            return

        if not data:
            # 空数据等同于 delete_session
            self.delete_session(session_id)
            return

        self._save_data(session_id, data)

    # ─────────────────────────────────────────
    # 资源管理
    # ─────────────────────────────────────────

    def close(self) -> None:
        """关闭（Markdown 后端无需显式关闭，保持接口一致）。"""
        pass

    # ─────────────────────────────────────────
    # 内部方法
    # ─────────────────────────────────────────

    def _load_data(self, session_id: str) -> dict[str, Any]:
        """从 Markdown 文件加载 KV 数据。"""
        path = self._get_session_path(session_id)
        if not os.path.exists(path):
            return {}

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            # 解析 JSON front matter
            if content.startswith("---"):
                end_idx = content.find("\n---", 3)
                if end_idx != -1:
                    front_matter = content[3:end_idx].strip()
                    try:
                        data = json.loads(front_matter)
                        if isinstance(data, dict):
                            return data
                    except (json.JSONDecodeError, ValueError):
                        logger.warning(
                            "MarkdownWorkingMemoryBackend: failed to parse front matter for session=%s",
                            session_id,
                        )

            return {}
        except OSError as e:
            logger.warning(
                "MarkdownWorkingMemoryBackend: failed to read session=%s: %s",
                session_id, e,
            )
            return {}

    def _save_data(self, session_id: str, data: dict[str, Any]) -> None:
        """将 KV 数据写入 Markdown 文件。"""
        path = self._get_session_path(session_id)
        now = datetime.now(timezone.utc).isoformat()

        # JSON front matter
        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        content = f"---\n{json_str}\n---\n"

        # 人类可读的 Markdown body
        safe_session = self._sanitize(session_id)
        content += f"\n# Working Memory: {safe_session}\n\n"
        content += f"- **updated_at**: {now}\n\n"

        for key, value in data.items():
            display_val = str(value)
            if len(display_val) > 200:
                display_val = display_val[:200] + "..."
            content += f"- **{key}**: {display_val}\n"

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            logger.warning(
                "MarkdownWorkingMemoryBackend: failed to write session=%s: %s",
                session_id, e,
            )
