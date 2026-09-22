"""pandapal/knowledge_base/tool.py — 对话检索工具 ``search_knowledge_base``。

一个通用工具检索所有「已就绪且启用」的知识库（PRD P1），通过
``knowledge_base`` 参数指定或省略（省略 = 检索所有已启用库）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pandaren.tool.decorator import tool
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.types import SensitivityLevel, ToolTier

if TYPE_CHECKING:
    from pandapal.knowledge_base.manager import KnowledgeBaseManager

_SEARCH_TOOL_GUIDE = (
    "仅在回答需要外部私有资料（用户导入的知识库文档）时才调用本工具。"
    "若用户通过 @库名 指定了知识库，必须把该名字传入 knowledge_base 参数；"
    "未指定时省略 knowledge_base 即可检索所有已启用的知识库。"
    "本工具只返回命中的原文片段与来源，不生成答案——最终答案由你基于这些片段总结。"
)


def build_search_tool(manager: "KnowledgeBaseManager"):
    """构造 search_knowledge_base 工具（executor 闭包捕获 manager）。"""

    @tool.function(
        name="search_knowledge_base",
        when_to_use="当用户的问题需要从已导入的知识库文档中检索答案时调用",
        description="在用户的知识库中检索相关内容，返回命中的原文片段与来源信息",
        tier=ToolTier.DEFERRED,
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            read_only=True,
            is_reversible=True,
            is_idempotent=True,
            audit_required=False,
        ),
        llm_guide=_SEARCH_TOOL_GUIDE,
        progress_label='搜索知识库「{query}」',
    )
    async def search_knowledge_base(
        ctx: ToolContext,
        query: str,
        knowledge_base: str = "",
        k: int = 5,
    ) -> str:
        """检索知识库。

        Args:
            ctx: 工具上下文。
            query: 用户的检索问题。
            knowledge_base: 指定知识库名称；空字符串表示检索所有已启用库。
            k: 返回结果条数，默认 5。
        """
        return await manager.search_for_llm(
            query,
            knowledge_base=knowledge_base or None,
            k=k,
        )

    return search_knowledge_base


_LIST_TOOL_GUIDE = (
    "当需要了解用户有哪些知识库、其状态或文档数量时调用本工具。"
    "只读，不会修改任何数据。返回库名、状态、文档数与是否在对话中启用。"
)


def build_list_tool(manager: "KnowledgeBaseManager"):
    """构造 list_knowledge_bases 工具（只读）。"""

    @tool.function(
        name="list_knowledge_bases",
        when_to_use="当需要列出/查看用户的知识库及其状态、文档数量时调用",
        description="列出用户所有知识库的名称、状态、文档数量与启用情况",
        tier=ToolTier.DEFERRED,
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            read_only=True,
            is_reversible=True,
            is_idempotent=True,
            audit_required=False,
        ),
        llm_guide=_LIST_TOOL_GUIDE,
        progress_label="列出知识库",
    )
    async def list_knowledge_bases(ctx: ToolContext) -> str:
        """列出所有知识库。"""
        return manager.list_kbs_text()

    return list_knowledge_bases


_BUILD_TOOL_GUIDE = (
    "当用户要求把知识库的文档构建/更新为可检索索引时调用本工具。"
    "默认 incremental=True 走增量（只重建变更文件，快）；"
    "仅当用户明确要求「全量重建/彻底重建」时才传 rebuild=True。"
    "构建为有副作用操作，会消耗 embedding 与 LLM 配额。"
)


def build_build_tool(manager: "KnowledgeBaseManager"):
    """构造 build_knowledge_base 工具（触发建库，有副作用）。"""

    @tool.function(
        name="build_knowledge_base",
        when_to_use="当用户要求构建或更新某个知识库的索引时调用",
        description="触发知识库构建（默认增量，可指定全量重建），等待完成后返回结果",
        tier=ToolTier.DEFERRED,
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.MEDIUM,
            read_only=False,
            is_reversible=False,
            is_idempotent=False,
            audit_required=True,
        ),
        llm_guide=_BUILD_TOOL_GUIDE,
        progress_label="构建知识库「{name}」",
    )
    async def build_knowledge_base(
        ctx: ToolContext,
        name: str,
        rebuild: bool = False,
    ) -> str:
        """构建知识库索引。

        Args:
            ctx: 工具上下文。
            name: 知识库名称。
            rebuild: 是否全量重建；默认 False（增量，只重建变更文件）。
        """
        return await manager.build_and_wait(name, rebuild=rebuild)

    return build_knowledge_base


_ADD_DOCS_TOOL_GUIDE = (
    "当用户要求把本地文件（已存在于磁盘的路径）加入某个知识库时调用本工具。"
    "仅接受本地文件绝对路径；文件会被复制进知识库目录，需要用户再次触发生成索引才会生效。"
)


def build_add_documents_tool(manager: "KnowledgeBaseManager"):
    """构造 add_knowledge_base_document 工具（纳入本地文件，有副作用）。"""

    @tool.function(
        name="add_knowledge_base_document",
        when_to_use="当用户要求把本地文件加入某个知识库时调用",
        description="把本地磁盘文件纳入指定知识库（仅复制，不自动建索引）",
        tier=ToolTier.DEFERRED,
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.MEDIUM,
            read_only=False,
            is_reversible=True,
            is_idempotent=False,
            audit_required=True,
        ),
        llm_guide=_ADD_DOCS_TOOL_GUIDE,
        progress_label="添加文档到知识库「{name}」",
    )
    async def add_knowledge_base_document(
        ctx: ToolContext,
        name: str,
        source_paths: list[str],
    ) -> str:
        """把本地文件加入知识库。

        Args:
            ctx: 工具上下文。
            name: 知识库名称。
            source_paths: 本地文件绝对路径列表。
        """
        if manager.get_kb(name) is None:
            return f"知识库 {name!r} 不存在。"
        before = {d["name"] for d in manager.list_documents(name)}
        await manager.upload_documents(name, list(source_paths))
        after = {d["name"] for d in manager.list_documents(name)}
        added = sorted(after - before)
        if not added:
            return f"未新增任何文档到 {name!r}（文件不存在、格式不支持或超过大小限制）。"
        return (
            f"已向知识库 {name!r} 添加 {len(added)} 个文档：{'、'.join(added)}。"
            "如需可检索，请再调用 build_knowledge_base 构建索引。"
        )

    return add_knowledge_base_document


_DELETE_TOOL_GUIDE = (
    "当用户明确要求删除某个知识库时调用本工具。"
    "该操作不可逆——会同时删除配置、索引与已上传文档，需用户确认。"
)


def build_delete_tool(manager: "KnowledgeBaseManager"):
    """构造 delete_knowledge_base 工具（不可逆，需审批）。"""

    @tool.function(
        name="delete_knowledge_base",
        when_to_use="当用户明确要求删除某个知识库时调用",
        description="删除指定知识库（配置+索引+文档，不可逆）",
        tier=ToolTier.DEFERRED,
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.HIGH,
            read_only=False,
            is_reversible=False,
            is_idempotent=True,
            audit_required=True,
        ),
        llm_guide=_DELETE_TOOL_GUIDE,
        progress_label="删除知识库「{name}」",
    )
    async def delete_knowledge_base(ctx: ToolContext, name: str) -> str:
        """删除知识库。

        Args:
            ctx: 工具上下文。
            name: 知识库名称。
        """
        if manager.get_kb(name) is None:
            return f"知识库 {name!r} 不存在。"
        await manager.delete_kb(name)
        return f"已删除知识库 {name!r}。"

    return delete_knowledge_base
