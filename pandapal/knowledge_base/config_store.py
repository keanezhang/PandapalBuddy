"""pandapal/knowledge_base/config_store.py — 知识库配置持久化（``kbs.toml``）。

范式对齐 ``pandapal/mcp/config_store.py``：
- 读取缺失 / 解析失败不算错（返回空表 / 跳过单条坏项）
- 写入原子（临时文件 + os.replace）
- 单条非法只跳过该条，不吞掉整份文件
"""

from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from pandapal.knowledge_base.models import KBConfig

logger = logging.getLogger(__name__)

_KBS_KEY = "knowledge_bases"


class KnowledgeBaseConfigStore:
    """``kbs.toml`` 的读写门面（无状态，可多次实例化）。"""

    def __init__(self, toml_path: str | Path) -> None:
        self._path = Path(toml_path)

    @property
    def path(self) -> Path:
        return self._path

    # ── 读 ──────────────────────────────────────────────────────────────

    def load_all(self) -> list[KBConfig]:
        """读取全部知识库配置；文件缺失/损坏返回空表，单条非法跳过。"""
        raw = self._read_raw()
        items = raw.get(_KBS_KEY, [])
        if not isinstance(items, list):
            logger.warning("知识库配置 %s 的 [[%s]] 不是数组，忽略", self._path, _KBS_KEY)
            return []

        configs: list[KBConfig] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            try:
                configs.append(KBConfig.from_dict(item))
            except Exception as exc:  # noqa: BLE001 - 单条非法只跳过
                logger.warning("跳过第 %d 条非法知识库配置（%s）: %s", index, self._path, exc)
        return configs

    def get(self, name: str) -> KBConfig | None:
        for cfg in self.load_all():
            if cfg.name == name:
                return cfg
        return None

    # ── 写 ──────────────────────────────────────────────────────────────

    def save(self, cfg: KBConfig) -> None:
        """按 name upsert 单条配置并原子落盘（校验不通过直接抛，不落盘）。"""
        cfg.validate()
        ordered: dict[str, KBConfig] = {c.name: c for c in self.load_all()}
        # 保留原状态，除非调用方已显式更新（避免 save 覆盖 building→ready 等运行时迁移）
        ordered[cfg.name] = cfg
        self._write_atomic(self._serialize(list(ordered.values())))

    def delete(self, name: str) -> None:
        """删除指定 name 的配置（幂等）。"""
        remaining = [c for c in self.load_all() if c.name != name]
        self._write_atomic(self._serialize(remaining))

    def update_status(self, name: str, status: str) -> None:
        """仅更新状态字段（幂等；不存在则 no-op）。"""
        cfg = self.get(name)
        if cfg is None:
            return
        cfg.status = status
        self.save(cfg)

    # ── 私有：文件 I/O ───────────────────────────────────────────────────

    def _read_raw(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            with open(self._path, "rb") as f:
                data = tomllib.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning("无法解析知识库配置文件 %s: %s", self._path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_atomic(self, content: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".toml", prefix=".kbs_tmp_", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, str(self._path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ── 私有：TOML 序列化 ────────────────────────────────────────────────

    @staticmethod
    def _serialize(configs: list[KBConfig]) -> str:
        lines = ["# 知识库配置 —— 由 PandaPal 自动生成，请通过 UI 编辑。", ""]
        for cfg in configs:
            lines.append(f"[[{_KBS_KEY}]]")
            lines.append(f"name = {_s(cfg.name)}")
            if cfg.description:
                lines.append(f"description = {_s(cfg.description)}")
            lines.append(f"documents_dir = {_s(cfg.documents_dir)}")
            lines.append(f"embedding_api_key = {_s(cfg.embedding_api_key)}")
            lines.append(f"embedding_model = {_s(cfg.embedding_model)}")
            lines.append(f"embedding_api_type = {_s(cfg.embedding_api_type)}")
            lines.append(f"embedding_api_url = {_s(cfg.embedding_api_url)}")
            lines.append(f"embedding_dimension = {int(cfg.embedding_dimension)}")
            lines.append(f"llm_provider = {_s(cfg.llm_provider)}")
            lines.append(f"llm_api_key = {_s(cfg.llm_api_key)}")
            lines.append(f"llm_model = {_s(cfg.llm_model)}")
            lines.append(f"llm_api_url = {_s(cfg.llm_api_url)}")
            lines.append(f"enable_bm25 = {'true' if cfg.enable_bm25 else 'false'}")
            lines.append(f"enabled_in_chat = {'true' if cfg.enabled_in_chat else 'false'}")
            lines.append(f"auto_rebuild = {'true' if cfg.auto_rebuild else 'false'}")
            lines.append(f"top_k = {int(cfg.top_k)}")
            lines.append(f"status = {_s(cfg.status)}")
            lines.append("")
        return "\n".join(lines)


def _s(value: str) -> str:
    """字符串转 TOML 双引号字面量。"""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
