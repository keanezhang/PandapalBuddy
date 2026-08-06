"""Markdown Repository 基类，提供文件读写和 YAML front matter 解析。

设计约束：
- 异步 IO（使用 asyncio.to_thread 包装同步 IO）
- 接口与 SQLite Repository 保持一致（async 方法）
- 使用 YAML front matter 存储结构化数据
- 每条记录对应一个 .md 文件
- 文件命名规则：{id}.md
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    # 仅供 _from_iso 的返回注解解析：运行时各方法内自行 import datetime
    from datetime import datetime

import yaml

logger = logging.getLogger(__name__)


class MarkdownBaseRepository:
    """Markdown 存储基类。

    目录结构：
    base_dir/
        sessions/
            {session_id}.md
        tasks/
            {task_id}.md
        ...

    文件格式（YAML front matter）：
    ---
    field1: value1
    field2: value2
    ---

    # Title
    Optional markdown body for human readability
    """

    # 分区模式下 session_id 为空时的兜底分区名（如渠道来源的审批无 session）
    _NO_SESSION_PARTITION = "_no_session"

    def __init__(
        self,
        base_dir: str,
        entity_name: str,
        timeout: float = 5.0,
        *,
        session_partitioned: bool = False,
    ) -> None:
        """
        Args:
            base_dir: Markdown 存储的根目录（如 "data/markdown"）
            entity_name: 实体名称（如 "sessions", "tasks"），用于创建子目录
            timeout: 操作超时秒数（保留参数，兼容接口）
            session_partitioned: True 时文件按 session 分区，落到
                ``{base_dir}/sessions/{sid}/{entity_name}/{id}.md``，与该 session 的
                session.md / audit.md 同目录内聚。id-only 查询走跨 session 扫描降级。
                False（默认）时保持平铺 ``{base_dir}/{entity_name}/{id}.md``。
        """
        self._base_dir = base_dir
        self._entity_name = entity_name
        self._timeout = timeout
        self._session_partitioned = session_partitioned

        if session_partitioned:
            # 分区模式：文件散落在各 session 目录下，_entity_dir 指向 sessions 根，
            # 仅用于日志/os.walk 兜底；真实路径由 _partition_path 计算。
            self._entity_dir = os.path.join(base_dir, "sessions")
        else:
            self._entity_dir = os.path.join(base_dir, entity_name)

        # 确保实体目录存在
        os.makedirs(self._entity_dir, exist_ok=True)

    # ──────────────────────────────────────────────
    # 文件操作方法（异步接口）
    # ──────────────────────────────────────────────

    def _get_file_path(self, entity_id: str) -> str:
        """获取实体对应的 Markdown 文件路径（非分区模式）。"""
        safe_id = self._sanitize_id(entity_id)
        return os.path.join(self._entity_dir, f"{safe_id}.md")

    # ──────────────────────────────────────────────
    # 分区模式辅助方法（session_partitioned=True 时使用）
    # ──────────────────────────────────────────────

    def _partition_path(self, session_id: str, entity_id: str) -> str:
        """分区模式下的文件路径：{base_dir}/sessions/{sid}/{entity_name}/{id}.md。

        session_id 为空/None 时归入 ``_NO_SESSION_PARTITION`` 兜底分区。
        """
        if not session_id:
            # ★ 空 session_id 会坍缩到共享兜底分区（所有空 session 写同一目录），
            #   是潜在的跨会话污染点。正常链路 session_id 必非空——出现即暴露源头。
            logger.warning(
                "[%s] _partition_path 收到空 session_id，落入共享兜底分区 %s "
                "(entity=%s)，请检查上游是否漏传 session_id。",
                self._entity_name, self._NO_SESSION_PARTITION, entity_id,
            )
        safe_sid = self._sanitize_id(session_id) if session_id else self._NO_SESSION_PARTITION
        safe_id = self._sanitize_id(entity_id)
        return os.path.join(
            self._base_dir, "sessions", safe_sid, self._entity_name, f"{safe_id}.md"
        )

    def _entity_glob_pattern(self, id_glob: str = "*") -> str:
        """匹配本实体全部（或指定 id）文件的 glob 模式，跨所有 session 分区。"""
        return os.path.join(
            self._base_dir, "sessions", "*", self._entity_name, f"{id_glob}.md"
        )

    async def _find_path_by_id(self, entity_id: str) -> str | None:
        """分区模式：仅凭 id 跨所有 session 目录定位文件（id-only 查询降级路径）。

        id 全局唯一（run_id/approval_id/task_id），故至多命中一个文件；无则返回 None。
        """
        safe_id = self._sanitize_id(entity_id)

        def _find() -> str | None:
            matches = glob.glob(self._entity_glob_pattern(safe_id))
            return matches[0] if matches else None

        return await asyncio.to_thread(_find)

    @staticmethod
    def _sanitize_id(entity_id: str) -> str:
        """清理实体 ID，防止路径遍历攻击。

        注意：冒号 `:` 也做显式替换（→ `-`），避免 `user:kkzhang` 变成 `userkkzhang`
        而与 user_id 为 `userkkzhang` 的情况冲突。
        """
        safe = entity_id.replace("/", "_").replace("\\", "_").replace(":", "-")
        safe = "".join(c for c in safe if c.isalnum() or c in "-_.")
        return safe if safe else "unknown"

    async def _read_entity(self, file_path: str) -> dict[str, Any] | None:
        """从 Markdown 文件读取实体数据（解析 YAML front matter）。"""

        def _read() -> dict[str, Any] | None:
            if not os.path.exists(file_path):
                return None

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                # 解析 front matter（兼容 JSON 和 YAML 两种格式）
                if content.startswith("---"):
                    # 查找独占一行的闭合 ---（\n---），避免匹配 JSON 字符串值内部的 ---
                    end_idx = content.find("\n---", 3)
                    if end_idx != -1:
                        front_matter = content[3:end_idx].strip()
                        # 优先尝试 JSON 解析（新格式）
                        try:
                            data = json.loads(front_matter)
                        except (json.JSONDecodeError, ValueError):
                            # 回退到 YAML 解析（旧格式兼容）
                            data = yaml.safe_load(front_matter)
                        return data if isinstance(data, dict) else {}

                return {}

            except Exception as e:
                logger.error("Failed to read entity from %s: %s", file_path, e)
                return None

        return await asyncio.to_thread(_read)

    async def _write_entity(self, file_path: str, data: dict[str, Any], title: str = "") -> None:
        """将实体数据写入 Markdown 文件（使用 YAML front matter）。"""

        def _write() -> None:
            try:
                # 使用 JSON 序列化（比 YAML 更安全，不会被特殊字符破坏）
                json_str = json.dumps(data, ensure_ascii=False, indent=2)
                content = f"---\n{json_str}\n---\n"

                # 添加可选的 Markdown body
                if title:
                    content += f"\n# {title}\n\n"
                    for key, value in data.items():
                        # body 部分截断显示，避免过长
                        display_val = str(value)
                        if len(display_val) > 200:
                            display_val = display_val[:200] + "..."
                        content += f"- **{key}**: {display_val}\n"

                os.makedirs(os.path.dirname(file_path), exist_ok=True)

                # 写入文件
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            except Exception as e:
                logger.error("Failed to write entity to %s: %s", file_path, e)
                raise

        await asyncio.to_thread(_write)

    async def _delete_entity(self, file_path: str) -> bool:
        """删除实体文件。"""

        def _delete() -> bool:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    return True
                return False
            except Exception as e:
                logger.error("Failed to delete entity at %s: %s", file_path, e)
                return False

        return await asyncio.to_thread(_delete)

    async def _list_entities(self) -> list[dict[str, Any]]:
        """列出本实体所有记录（读取所有 .md 文件）。

        分区模式：只 glob 本实体的分区文件（{base}/sessions/*/{entity_name}/*.md），
        不会误读同 session 目录下的 session.md / audit.md / 其他实体文件。
        非分区模式：递归遍历 _entity_dir。
        """

        def _list() -> list[dict[str, Any]]:
            entities = []
            try:
                if self._session_partitioned:
                    paths = glob.glob(self._entity_glob_pattern())
                else:
                    paths = []
                    for root, _, filenames in os.walk(self._entity_dir):
                        for filename in filenames:
                            if filename.endswith(".md"):
                                paths.append(os.path.join(root, filename))
                for fp in paths:
                    data = self._sync_read_entity(fp)
                    if data:
                        entities.append(data)
            except Exception as e:
                logger.error("Failed to list entities in %s: %s", self._entity_dir, e)
            return entities

        return await asyncio.to_thread(_list)

    def _sync_read_entity(self, file_path: str) -> dict[str, Any] | None:
        """同步读取实体（在 to_thread 中使用）。"""
        if not os.path.exists(file_path):
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            if content.startswith("---"):
                # 查找独占一行的闭合 ---（\n---），避免匹配 JSON 字符串值内部的 ---
                end_idx = content.find("\n---", 3)
                if end_idx != -1:
                    front_matter = content[3:end_idx].strip()
                    # 优先尝试 JSON 解析（新格式）
                    try:
                        data = json.loads(front_matter)
                    except (json.JSONDecodeError, ValueError):
                        # 回退到 YAML 解析（旧格式兼容）
                        data = yaml.safe_load(front_matter)
                    return data if isinstance(data, dict) else None

            return None

        except Exception as e:
            logger.error("Failed to read entity from %s: %s", file_path, e)
            return None

    async def _filter_entities(self, **kwargs) -> list[dict[str, Any]]:
        """根据字段值过滤实体（简单的内存过滤）。"""
        all_entities = await self._list_entities()
        filtered = []

        for entity in all_entities:
            match = True
            for key, value in kwargs.items():
                if entity.get(key) != value:
                    match = False
                    break
            if match:
                filtered.append(entity)

        return filtered

    # ──────────────────────────────────────────────
    # 工具方法（兼容 BaseRepository 接口）
    # ──────────────────────────────────────────────

    @staticmethod
    def _now_iso() -> str:
        """返回当前本地时间字符串（'YYYY-MM-DD HH:MM:SS'）。

        调试阶段：所有时间字段统一用本地时间，与 cron_expression 对齐方便排查。
        """
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _to_iso(dt: Any) -> str | None:
        """datetime → 本地时间字符串。tz-aware 会先转本地。"""
        from datetime import datetime

        if dt is None:
            return None
        if isinstance(dt, str):
            return dt
        if isinstance(dt, datetime):
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return str(dt)

    @staticmethod
    def _from_iso(value: str | None) -> datetime | None:
        """字符串转 datetime，兼容 ISO 和本地时间两种格式。

        与 SQLite 版 _from_iso 语义对齐：输入字符串，返回 datetime。
        Markdown 存储用 "%Y-%m-%d %H:%M:%S" 格式（_to_iso 产出），
        同时也兼容标准 ISO 格式（历史数据或外部输入）。
        """
        from datetime import datetime

        if value is None:
            return None
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            try:
                return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                return None
