"""pandapal/knowledge_base/rag_llm_provider.py — RAGLLMProvider 适配器。

把 pandaren 的 LLM client（``OpenAICompatibleClient`` / ``LLMRouter``，满足
``LLMClient`` 协议：``call(messages)`` / ``stream_response(messages)``）桥接成
NexusRAG 的 ``RAGLLMProvider`` 协议（``run(prompt, instructions)``）。

用途：知识库建库抽取（实体/关系/keywords/分词）与检索分词。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


class PandaPalRAGLLMProvider:
    """把 pandaren LLM client 包装为 NexusRAG 的 RAGLLMProvider。

    构造时注入一个满足 ``LLMClient`` 协议的对象（拥有 ``call`` 与
    ``stream_response``）。``call`` 返回 LLMResponse（TypedDict，含 ``content``），
    ``stream_response`` yield LLMStreamChunk（含 ``delta_content``）。
    """

    def __init__(self, client: Any, *, default_model: str = "") -> None:
        self._client = client
        self._default_model = default_model

    @staticmethod
    def _build_messages(prompt: str, instructions: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        if prompt:
            messages.append({"role": "user", "content": prompt})
        return messages

    def _settings(self, temperature: Optional[float], max_tokens: Optional[int]) -> Any:
        """构造 pandaren ModelSettings（延迟导入，避免硬依赖）。"""
        from pandaren.llm.types import ModelSettings  # 延迟导入

        return ModelSettings(
            temperature=temperature if temperature is not None else 0.3,
            max_tokens=max_tokens if max_tokens is not None else 4096,
        )

    async def run(
        self,
        prompt: str,
        instructions: str,
        *,
        agent_name: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        """非流式调用，返回模型输出文本；失败返回空串。"""
        messages = self._build_messages(prompt, instructions)
        if not messages:
            return ""
        try:
            resp = await self._client.call(messages, settings=self._settings(temperature, max_tokens))
        except Exception as e:  # noqa: BLE001 - RAG 内部 LLM 失败不阻断主链路
            logger.warning("RAG LLM run 失败: %s", e)
            return ""
        if isinstance(resp, dict):
            return (resp.get("content") or "").strip()
        return (getattr(resp, "content", None) or "").strip()

    async def run_stream(
        self,
        prompt: str,
        instructions: str,
        *,
        agent_name: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """流式调用，逐 delta yield；异常时提前结束。"""
        messages = self._build_messages(prompt, instructions)
        if not messages:
            return
        try:
            async for chunk in self._client.stream_response(
                messages, settings=self._settings(temperature, max_tokens)
            ):
                if isinstance(chunk, dict):
                    delta = chunk.get("delta_content")
                else:
                    delta = getattr(chunk, "delta_content", None)
                if delta:
                    yield delta
        except Exception as e:  # noqa: BLE001
            logger.warning("RAG LLM run_stream 失败: %s", e)


def build_provider(
    *,
    provider: str,
    api_key: str,
    model: str,
    base_url: str = "",
) -> "PandaPalRAGLLMProvider":
    """根据知识库**独立** LLM 配置构建 RAGLLMProvider。

    不复用对话 LLM client：每个知识库用自己填写的 provider / api_key / model
    独立构建 ``OpenAICompatibleClient``（重量依赖延迟导入）。provider 白名单与
    base_url 兜底都走 provider catalog，与对话凭据完全解耦。
    """
    from pandaren.llm.client import OpenAICompatibleClient

    from pandapal.config.llm.provider_catalog import resolve_base_url

    resolved_base_url = resolve_base_url(provider.strip(), base_url.strip() or None)
    client = OpenAICompatibleClient.for_provider(
        provider=provider.strip(),
        api_key=api_key.strip(),
        model_name=model.strip(),
        base_url=resolved_base_url,
        # 实体/关系抽取 prompt 较长、生成结构化 JSON 耗时久，
        # 默认 60s 不够，放宽到 5 分钟。
        timeout=300.0,
    )
    return PandaPalRAGLLMProvider(client)
