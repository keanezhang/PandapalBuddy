"""pandapal/knowledge_base/tests/test_tree_guard.py — app 层 KB handler 兜底契约。

覆盖设计文档用例 3、34（错误码跨层透传 + 意外异常兜底）。

口径说明：
  ``_kb_guard`` 是 ``PandaPalApp`` 注册 KB handler 时定义的**局部闭包**
  （``pandapal/app.py:718-733``），无法直接 import。设计文档 §用例3 授权「装配
  ``_kb_guard`` 同款逻辑」——本文件**逐分支复刻**该 guard 的三条路径：
    (a) 成功 → 不广播任何事件
    (b) ``KnowledgeBaseError`` → ``global_error(exc.code, exc.detail)``
    (c) 意外 ``Exception`` → ``global_error("kb_handler_error", str(exc))``
  Oracle 使用**真实** ``NormalizedEvent.global_error`` + ``KnowledgeBaseError``，
  故本测试实际校验的是「manager 抛出的错误码，经 guard 后应落在哪个出站字段」这一
  跨层契约；各错误码由 manager 在何种条件下抛出，另由 test_tree_ops.py 覆盖。
"""

from __future__ import annotations

import pytest

from pandapal.events.normalized import EventType, NormalizedEvent
from pandapal.knowledge_base.manager import KnowledgeBaseError


async def _kb_guard(op, call, broadcast):
    """逐分支复刻 app.py:718-733 的 KB handler 兜底守卫（见模块 docstring）。"""
    try:
        await call()
    except KnowledgeBaseError as exc:
        await broadcast.send(NormalizedEvent.global_error(exc.code, exc.detail))
    except Exception as exc:  # noqa: BLE001 - handler 兜底，绝不外抛
        await broadcast.send(
            NormalizedEvent.global_error("kb_handler_error", str(exc))
        )


def _errors(broadcast) -> list:
    return [e for e in broadcast.events if e.event_type == EventType.ERROR]


# ── 用例 3：错误码跨层透传（8 码等价类 + 成功无事件）───────────────────────

# §0.2 码表：manager 抛出的全部业务错误码
_ERROR_CODES = [
    "kb_not_found",
    "kb_busy",
    "invalid_path",
    "invalid_name",
    "path_not_found",
    "invalid_target",
    "name_conflict",
    "io_error",
]


@pytest.mark.parametrize("code", _ERROR_CODES)
async def test_tree3_business_error_code_passthrough(broadcast, code):
    detail = f"detail-{code}"

    async def call():
        raise KnowledgeBaseError(code, detail)

    await _kb_guard("create_folder", call, broadcast)

    errs = _errors(broadcast)
    assert len(errs) == 1
    assert errs[0].payload["error_code"] == code
    assert errs[0].payload["error_message"] == detail          # detail 原文透传


async def test_tree3_success_emits_no_event(broadcast):
    async def call():
        return None

    await _kb_guard("create_folder", call, broadcast)

    assert broadcast.events == []


# ── 用例 34：意外异常 → kb_handler_error（不外抛）──────────────────────────


async def test_tree34_unexpected_exception_falls_back(broadcast):
    async def call():
        raise RuntimeError("unexpected boom")

    await _kb_guard("create_folder", call, broadcast)           # 关键：不外抛

    errs = _errors(broadcast)
    assert len(errs) == 1
    assert errs[0].payload["error_code"] == "kb_handler_error"
    assert errs[0].payload["error_message"] == "unexpected boom"
