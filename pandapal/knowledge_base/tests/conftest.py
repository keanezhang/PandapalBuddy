"""pandapal/knowledge_base/tests/conftest.py — 知识库测试共享替身。

对齐 ``pandapal/mcp/tests`` 的替身范式：
  - ``FakeBroadcast``：收集出站 ``NormalizedEvent``（``MessageBroadcast.send`` 的纯观测替身）
  - ``FakeConfigStore``：内存版 ``KnowledgeBaseConfigStore``（同 upsert / 幂等语义 + 记录调用）

真实文件系统行为（``documents_dir`` / ``db_path`` 的增删复制）一律走 ``tmp_path``，不 mock。
重量依赖（NexusRAG / chromadb / numpy / 网络）在各用例内用 ``monkeypatch`` 或 ``sys.modules`` 注入替身。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pandapal.knowledge_base.manager import KnowledgeBaseManager
from pandapal.knowledge_base.models import KBConfig


class FakeBroadcast:
    """收集出站事件（对齐 ``MessageBroadcast.send`` 的可 await 接口）。"""

    def __init__(self) -> None:
        self.events: list = []

    async def send(self, event, origin_channel_id=None) -> None:
        self.events.append(event)


class FakeConfigStore:
    """内存版 ``KnowledgeBaseConfigStore``。

    与真实 store 同语义（按 name upsert、删除幂等、status 单字段更新），
    但落在内存并**逐次记录调用**——便于断言「未落盘 / 未触发状态迁移」这类
    调用级副作用（比读最终状态更直接，也免掉真实 toml I/O 噪声）。
    """

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._configs: dict[str, KBConfig] = {}
        self.calls: list[tuple] = []

    @property
    def path(self) -> Path:
        return self._path

    def seed(self, cfg: KBConfig) -> None:
        """预置一条配置（不计入 save 调用记录）。"""
        self._configs[cfg.name] = KBConfig.from_dict(cfg.to_dict())

    def load_all(self) -> list[KBConfig]:
        self.calls.append(("load_all",))
        return [KBConfig.from_dict(c.to_dict()) for c in self._configs.values()]

    def get(self, name: str) -> KBConfig | None:
        self.calls.append(("get", name))
        cfg = self._configs.get(name)
        return KBConfig.from_dict(cfg.to_dict()) if cfg is not None else None

    def save(self, cfg: KBConfig) -> None:
        self.calls.append(("save", cfg.name))
        self._configs[cfg.name] = KBConfig.from_dict(cfg.to_dict())

    def delete(self, name: str) -> None:
        self.calls.append(("delete", name))
        self._configs.pop(name, None)

    def update_status(self, name: str, status: str) -> None:
        self.calls.append(("update_status", name, status))
        cfg = self._configs.get(name)
        if cfg is not None:
            cfg.status = status

    def calls_of(self, op: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == op]


@pytest.fixture
def broadcast() -> FakeBroadcast:
    return FakeBroadcast()


@pytest.fixture
def store(tmp_path) -> FakeConfigStore:
    return FakeConfigStore(tmp_path / "kbs.toml")


@pytest.fixture
def manager(store: FakeConfigStore, broadcast: FakeBroadcast) -> KnowledgeBaseManager:
    return KnowledgeBaseManager(config_store=store, broadcast=broadcast)
