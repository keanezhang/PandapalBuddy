"""Config 测试共享 Fixtures。"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_environ():
    """每个测试后还原 os.environ，杜绝跨测试污染。

    ⚠️ 必需：``ConfigManager.load_config()`` 用 ``load_dotenv`` 把 env 文件灌进
    ``os.environ``，而 ``load_dotenv`` 默认**不覆盖**已有值、也不会在测试间清理。
    没有本 fixture 时，先跑的加载用例会把 PANDAPAL_RELAY_URL 等留在进程环境里，
    导致后跑的用例读到残留值而不报错——看起来像「配置门禁失效」，实则是测试污染
    （生产进程启动时环境是干净的）。
    """
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture
def config_dir(tmp_path):
    """返回一个临时配置目录（不含 env 文件）。"""
    return str(tmp_path)
