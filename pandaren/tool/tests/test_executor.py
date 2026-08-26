"""pandaren/tool/tests/test_executor.py — ToolExecutor 执行生命周期测试。

风险映射：
  Risk-1（P0）：filter_extra_args 移除 schema 外参数（公开方法，原私有 API 回归）
  Risk-2（P0）：coerce_args 字符串 → int/float/bool 转换（公开方法回归）
  Risk-3（P0）：execute 永远返回 ToolResult，工具异常不向外抛（O3 精神）
  Risk-4（P1）：error_formatter 自身异常 → 留痕回落默认格式（不静默吞）
  Risk-5（P1）：同步工具走线程池不阻塞事件循环（行为回归）
  Risk-6（P1）：Phase3 结果格式化（dict 原样 / 其余 str()）
  Risk-7（P2）：Phase4 输出截断（max_output_bytes）
"""

from __future__ import annotations

import asyncio
from typing import Any

from pandaren.tool.definition.tool_lifecycle import ToolLifecycle
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.execution.executor import ToolExecutor
from pandaren.tool.types import SensitivityLevel

from .conftest import make_ctx, make_tool


def _num_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
        },
    }


class TestFilterExtraArgs:
    def test_extra_args_removed(self):
        # Risk-1
        tool = make_tool("calc", input_schema={"type": "object", "properties": {"a": {"type": "integer"}}})
        filtered, removed = ToolExecutor().filter_extra_args(tool, {"a": 1, "bogus": 2})
        assert filtered == {"a": 1}
        assert removed == ["bogus"]

    def test_no_schema_properties_all_removed(self):
        # Risk-1：properties 缺失 → 全部过滤（防御行为）
        tool = make_tool("calc", input_schema={"type": "object"})
        filtered, removed = ToolExecutor().filter_extra_args(tool, {"a": 1})
        assert filtered == {}
        assert removed == ["a"]

    def test_all_args_allowed_noop(self):
        tool = make_tool("calc", input_schema={"type": "object", "properties": {"a": {"type": "integer"}}})
        filtered, removed = ToolExecutor().filter_extra_args(tool, {"a": 1})
        assert filtered == {"a": 1}
        assert removed == []


class TestCoerceArgs:
    def test_string_int_coerced(self):
        # Risk-2
        tool = make_tool("calc", input_schema=_num_schema())
        coerced, changed = ToolExecutor().coerce_args(tool, {"count": "20"})
        assert coerced["count"] == 20
        assert isinstance(coerced["count"], int)
        assert changed == ["count"]

    def test_string_number_coerced(self):
        tool = make_tool("calc", input_schema=_num_schema())
        coerced, _ = ToolExecutor().coerce_args(tool, {"ratio": "3.14"})
        assert coerced["ratio"] == 3.14
        assert isinstance(coerced["ratio"], float)

    def test_boolean_strings_coerced(self):
        tool = make_tool("calc", input_schema=_num_schema())
        coerced, changed = ToolExecutor().coerce_args(tool, {"flag": "true", "count": "0"})
        assert coerced["flag"] is True
        assert coerced["count"] == 0
        assert "flag" in changed and "count" in changed

    def test_float_to_int_when_integral(self):
        tool = make_tool("calc", input_schema=_num_schema())
        coerced, _ = ToolExecutor().coerce_args(tool, {"count": 20.0})
        assert coerced["count"] == 20
        assert isinstance(coerced["count"], int)

    def test_unparseable_string_kept(self):
        # Risk-2：无法解析 → 保留原值（不抛异常）
        tool = make_tool("calc", input_schema=_num_schema())
        coerced, changed = ToolExecutor().coerce_args(tool, {"count": "abc"})
        assert coerced["count"] == "abc"
        assert changed == []

    def test_unknown_prop_kept(self):
        # Risk-2：schema 外参数在 coerce 中不报错
        tool = make_tool("calc", input_schema=_num_schema())
        coerced, _ = ToolExecutor().coerce_args(tool, {"unknown": "x"})
        assert coerced == {"unknown": "x"}


class TestExecute:
    async def test_sync_tool_executed(self):
        # Risk-5：同步工具正常执行，结果 success
        def _exec(ctx, **kw):
            return "result-data"

        tool = make_tool("calc", executor=_exec)
        result = await ToolExecutor().execute(tool, {}, make_ctx())
        assert result.success is True
        assert result.data == "result-data"

    async def test_sync_tool_in_threadpool_not_blocking(self):
        # Risk-5：同步工具在 executor 线程池执行——用「调用线程 ≠ 事件循环线程」佐证
        loop_ident: list[int] = []

        def _exec(ctx, **kw):
            import threading
            loop_ident.append(threading.get_ident())
            return "done"

        tool = make_tool("calc", executor=_exec)
        await ToolExecutor().execute(tool, {}, make_ctx())
        assert loop_ident and loop_ident[0] != asyncio.get_running_loop()._thread_id

    async def test_async_tool_executed(self):
        async def _exec(ctx, **kw):
            return "async-data"

        tool = make_tool("calc", executor=_exec)
        result = await ToolExecutor().execute(tool, {}, make_ctx())
        assert result.success is True
        assert result.data == "async-data"

    async def test_executor_exception_returns_failure_result(self):
        # Risk-3：异常 → ToolResult(success=False)，不向外抛
        def _exec(ctx, **kw):
            raise ValueError("boom")

        tool = make_tool("calc", executor=_exec)
        result = await ToolExecutor().execute(tool, {}, make_ctx())
        assert result.success is False
        assert "ValueError" in result.error

    async def test_executor_exception_never_propagates(self):
        # Risk-3：O3 契约——外层不捕获也不崩
        def _exec(ctx, **kw):
            raise RuntimeError("should be contained")

        tool = make_tool("calc", executor=_exec)
        result = await ToolExecutor().execute(tool, {}, make_ctx())
        assert result.success is False

    async def test_lifecycle_validate_input_failure(self):
        # 输入校验失败 → 直接失败结果，不执行 executor
        calls: list[str] = []

        def _exec(ctx, **kw):
            calls.append("executed")
            return "ok"

        def _validate(args, ctx):
            from pandaren.tool.definition.tool_result import ValidationResult
            return ValidationResult(valid=False, message="参数不合法")

        tool = make_tool("calc", executor=_exec, lifecycle=ToolLifecycle(validate_input=_validate))
        result = await ToolExecutor().execute(tool, {}, make_ctx())
        assert result.success is False
        assert "参数不合法" in result.error
        assert calls == []  # executor 未被调用

    async def test_error_formatter_used(self):
        def _fmt(exc, name):
            return f"[定制] {name} 出错: {exc}"

        def _exec(ctx, **kw):
            raise KeyError("k")

        tool = make_tool(
            "calc",
            executor=_exec,
            lifecycle=ToolLifecycle(error_formatter=_fmt),
        )
        result = await ToolExecutor().execute(tool, {}, make_ctx())
        assert result.success is False
        assert result.error == "[定制] calc 出错: 'k'"

    async def test_error_formatter_raising_falls_back(self):
        # Risk-4：formatter 自身异常 → 回落默认格式，且仍返回 ToolResult
        def _bad_formatter(exc, name):
            raise RuntimeError("formatter broke")

        def _exec(ctx, **kw):
            raise ValueError("orig")

        tool = make_tool(
            "calc",
            executor=_exec,
            lifecycle=ToolLifecycle(error_formatter=_bad_formatter),
        )
        result = await ToolExecutor().execute(tool, {}, make_ctx())
        assert result.success is False
        assert "ValueError" in result.error
        assert "orig" in result.error

    async def test_result_formatting_dict_preserved(self):
        # Risk-6：dict 结果原样保留
        async def _exec(ctx, **kw):
            return {"plan_path": "/x", "plan_content": "y"}

        tool = make_tool("calc", executor=_exec)
        result = await ToolExecutor().execute(tool, {}, make_ctx())
        assert result.data == {"plan_path": "/x", "plan_content": "y"}

    async def test_result_formatting_custom(self):
        # Risk-6：format_result_for_llm 钩子生效
        def _fmt(data, name):
            return f"formatted:{data}"

        async def _exec(ctx, **kw):
            return 123

        tool = make_tool("calc", executor=_exec, lifecycle=ToolLifecycle(format_result_for_llm=_fmt))
        result = await ToolExecutor().execute(tool, {}, make_ctx())
        assert result.data == "formatted:123"

    async def test_output_truncation(self):
        # Risk-7：max_output_bytes 截断 + truncated 标记
        async def _exec(ctx, **kw):
            return "x" * 1000

        tool = make_tool(
            "calc",
            executor=_exec,
            policy=ToolPolicy(sensitivity=SensitivityLevel.LOW, max_output_bytes=10),
        )
        result = await ToolExecutor().execute(tool, {}, make_ctx())
        assert result.truncated is True
        assert len(result.data.encode("utf-8")) <= 10

    async def test_fix_hint_injected_on_extra_args(self):
        # 参数修正提示注入（LLM 学习信号）
        def _exec(ctx, **kw):
            return "ok"

        tool = make_tool(
            "calc",
            executor=_exec,
            input_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
        )
        result = await ToolExecutor().execute(tool, {"a": 1, "bogus": 2}, make_ctx())
        assert result.success is True
        assert "[参数修正]" in result.data
