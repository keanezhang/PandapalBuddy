"""pandaren/llm/protocol.py — LLMClient Protocol（最小接口契约）

第三方实现自定义 LLM 客户端时，只需引用此文件，
无需依赖 httpx 或任何网络实现细节。

最小契约：
  - model_name: str（只读属性）
  - async call(messages, tools, settings) -> LLMResponse
  - stream_response 和 aclose 为可选扩展
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Protocol, runtime_checkable

from .types import LLMResponse, LLMStreamChunk, ModelSettings


@runtime_checkable
class LLMClient(Protocol):
    """LLM 调用接口 Protocol（最小契约）。

    第三方只需实现 model_name + async call() 即可注入。
    stream_response 和 aclose 为可选扩展，Protocol 不强制运行时检查。
    """

    @property
    def model_name(self) -> str:
        """模型名，只读。实现类用 @property 或 ClassVar[str] 均可。"""
        ...

    async def call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        settings: ModelSettings | None = None,
    ) -> LLMResponse:
        """非流式调用，返回完整 LLMResponse。"""
        ...

    def stream_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        """流式调用。

        实现为 async generator 函数（async def + yield），
        调用方直接 `async for chunk in client.stream_response(...)` 消费，
        无需 await。Protocol 声明为普通函数返回 AsyncGenerator 以匹配此调用约定。
        """
        ...

    async def aclose(self) -> None:
        """关闭连接池，释放资源。"""
        ...
