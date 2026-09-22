"""pandapal.knowledge_base — 知识库子系统。

基于 NexusRAG SDK（仓库内包 ``pandapal.knowledge_base.rag``）实现：
- 知识库 CRUD + 文档上传（``config_store`` / ``manager``）
- 建库长任务编排 + 进度（``builder``）
- 对话检索工具 ``search_knowledge_base``（``tool``）
- RAGLLMProvider 适配（复用 pandaren 的 LLM client）
"""
