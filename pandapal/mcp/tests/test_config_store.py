"""pandapal/mcp/tests/test_config_store.py — servers.toml 读写不变量。

守住三条契约（见 config_store.py 模块 docstring）：
  - 读写往返等值（含可选键 args/env/cwd/headers，不得在途中丢失）
  - 读取缺失 / 单条坏数据不算错（一条坏配置不吞掉整份文件）
  - 写入原子、按 name upsert、delete 幂等；非法配置 fail-fast 不落盘
"""

from __future__ import annotations

import pytest

from pandapal.mcp.config_store import McpConfigStore
from pandaren.mcp.config import McpConfigError, McpServerConfig, McpTransport


@pytest.fixture
def store(tmp_path) -> McpConfigStore:
    """指向临时目录下的 servers.toml，绝不碰真实用户数据目录。"""
    return McpConfigStore(tmp_path / "mcp" / "servers.toml")


def _stdio(name: str = "fs", **kw) -> McpServerConfig:
    kw.setdefault("command", "npx")
    return McpServerConfig(name=name, transport=McpTransport.STDIO, **kw)


# ── Risk-1 可选键丢失：save → load_all 逐字段等值 ─────────────────────────


@pytest.mark.parametrize(
    "cfg",
    [
        _stdio(
            "fs",
            args=("-y", "@modelcontextprotocol/server-filesystem", "/tmp"),
            env={"GITHUB_TOKEN": "abc"},
            cwd="/work",
        ),
        McpServerConfig(
            name="remote",
            transport=McpTransport.HTTP,
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer x"},
        ),
    ],
)
def test_round_trip_preserves_all_fields(store, cfg):
    store.save(cfg)
    assert store.load_all() == [cfg]


# ── 读取缺失不算错 ────────────────────────────────────────────────────────


def test_missing_file_returns_empty(store):
    assert not store.path.exists()
    assert store.load_all() == []


# ── Risk-2 单条坏数据：跳过该条，其余保留 ─────────────────────────────────


def test_bad_item_skipped_others_kept(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        "[[servers]]\n"
        'name = "broken"\n'
        'transport = "stdio"\n'  # 缺 command → 非法
        "\n"
        "[[servers]]\n"
        'name = "ok"\n'
        'transport = "stdio"\n'
        'command = "npx"\n',
        encoding="utf-8",
    )
    assert [c.name for c in store.load_all()] == ["ok"]


# ── 原子写 / upsert：save 不吞他条，delete 只删目标 ───────────────────────


def test_save_keeps_other_entries_and_delete_removes_one(store):
    store.save(_stdio("a"))
    store.save(_stdio("b"))
    assert [c.name for c in store.load_all()] == ["a", "b"]

    store.delete("a")
    assert [c.name for c in store.load_all()] == ["b"]


def test_delete_missing_name_is_noop(store):
    store.save(_stdio("a"))
    store.delete("ghost")  # 不存在：不抛、不影响他条
    assert [c.name for c in store.load_all()] == ["a"]


# ── Risk-3 非法配置 fail-fast：抛错且不落盘 ───────────────────────────────


def test_save_invalid_config_raises_and_does_not_write(store):
    store.save(_stdio("a"))
    before = store.path.read_bytes()

    invalid = _stdio("b")
    object.__setattr__(invalid, "command", None)  # 绕开构造校验，构造「缺 command」

    with pytest.raises(McpConfigError):
        store.save(invalid)
    assert store.path.read_bytes() == before
