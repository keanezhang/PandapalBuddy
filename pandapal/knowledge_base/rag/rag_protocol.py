#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG 对外协议/类型定义。

本模块定义 RAG SDK 对外暴露的接口协议，不依赖任何具体实现。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class RAGLLMProvider(Protocol):
    """
    RAG 系统与大语言模型 (LLM) 之间的统一抽象接口。

    **为什么需要这个协议？**

    RAG 系统需要在多个环节调用 LLM（意图分类、实体抽取、问答生成、LLM 重排等），
    但 RAG 作为纯 SDK 不应直接依赖任何具体的 LLM 框架（如 OpenAI Agents SDK、LangChain 等）。
    因此定义了此协议：RAG 内部只依赖 ``RAGLLMProvider.run()``，具体的 LLM 调用实现由**调用方注入**。

    **如何使用？**

    调用方只需提供一个实现了 ``run()`` 方法的对象，传给 ``RAGConfig`` 的以下字段：

    - ``intent_llm_provider``：意图分类用的 LLM（QueryIntentClassifier 使用）
    - ``qa_llm_provider``：问答生成用的 LLM（RAGInstance.query() 最终生成答案时使用）

    **实现方式（三选一）：**

    1. **使用宿主项目的 AgentManager**（推荐已有 OpenAI Agents SDK 的项目）::

        from agent.integrations.rag_integration import AgentManagerRAGLLMProvider
        from agent.agents.agent_manager import AgentManager

        business_name = os.environ.get("RAG_LLM_BUSINESS_NAME", "qwen-flash")
        agent_manager = AgentManager(business_name=business_name)
        provider = AgentManagerRAGLLMProvider(agent_manager)

        engine = RAGEngine(
            qa_llm_provider=provider,
            intent_llm_provider=provider,
        )

    2. **自定义实现**（适配任意 LLM 后端）::

        class MyLLMProvider:
            async def run(self, prompt: str, instructions: str, *,
                    agent_name=None, temperature=None, max_tokens=None, **kwargs) -> str:
                # 调用你自己的 LLM API（OpenAI、Anthropic、本地模型等）
                response = await my_llm_client.chat(
                    system=instructions,
                    user=prompt,
                    temperature=temperature or 0.3,
                )
                return response.text

        engine = RAGEngine(qa_llm_provider=MyLLMProvider())

    3. **不注入**（纯检索模式，不调用 LLM）::

        engine = RAGEngine()  # 所有 *_llm_provider 默认为 None
        # query() 仍可执行检索 + 重排，但 answer 字段为空

    **run() 参数说明：**

    - ``prompt``：用户问题 + 检索结果组装后的完整提示词
    - ``instructions``：系统指令（角色设定、约束等）
    - ``agent_name``：可选标识，用于日志/区分不同调用场景
    - ``temperature``：采样温度（建议默认 0.3）
    - ``max_tokens``：最大输出 token 数（建议默认 4096）
    - 返回值：模型输出的纯文本字符串，失败时返回空字符串 ``""``
    """

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
        """异步调用 LLM，返回模型输出文本。返回空字符串表示失败或跳过。"""
        ...

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
        """
        异步流式调用 LLM，逐 token yield 文本片段。

        默认实现：回退到 run() 一次性返回（保证向后兼容，自定义 Provider 无需强制实现）。

        Yields:
            str: 模型输出的文本片段（delta）
        """
        result = await self.run(
            prompt, instructions,
            agent_name=agent_name,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        if result:
            yield result


@runtime_checkable
class ChunkLike(Protocol):
    """
    store() 方法接收的 chunk 对象协议：必须具有 metadata 和 page_content 属性。

    提升到 rag_protocol.py 以供跨模块复用，避免 ``from store.vector.vector_store import ChunkLike`` 的深层导入。
    """
    metadata: Dict[str, Any]
    page_content: str
