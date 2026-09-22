"""pandapal/knowledge_base/models.py — 知识库配置模型与状态枚举。

纯数据结构，不依赖 NexusRAG / pandaren，供 config_store / manager / 前端协议共用。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

# 支持的文档格式（与 NexusRAG DocumentLoader 对齐）
SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".pdf", ".docx", ".md", ".txt"})

# 单文件大小上限（MB），与 NexusRAG 默认一致
MAX_FILE_SIZE_MB: int = 50

# 知识库数量软上限（PRD P5）
MAX_KB_COUNT: int = 10

# 支持的向量维度（阿里通用文本向量跨模型全集）。
# NexusRAG 仅当维度命中此白名单时才写入请求的 dimensions 字段，否则回落 API 默认。
SUPPORTED_EMBEDDING_DIMENSIONS: list[int] = [2560, 2048, 1536, 1024, 768, 512, 256, 128, 64]


def collection_name_for(name: str) -> str:
    """知识库名 → chromadb collection 名（ASCII 安全）。

    chromadb collection 名只允许 [a-zA-Z0-9._-]，而知识库名允许中文。
    用 name 的稳定 SHA256 派生 ASCII 标识（kb_ + 16 位 hex），
    保证中文名可用、跨重启稳定、碰撞概率可忽略。
    """
    return "kb_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


class KBStatus(str, Enum):
    """知识库索引状态机（PRD §4.3）。

    building 为运行时临时态，不持久化；其余持久化到 kbs.toml。
    """

    EMPTY = "empty"        # 空库：无文档
    PENDING = "pending"    # 待索引：有文档但索引未建/已过期
    BUILDING = "building"  # 索引中（运行时态）
    READY = "ready"        # 已就绪
    FAILED = "failed"      # 建库失败


@dataclass
class DocNode:
    """文档树节点（FS 即真相源的契约模型）。

    与 PRD §3.4 的前端视图模型不同：本模型为后端 / 线格式，前端由 ``path`` 派生
    ``id/kind/type``（见设计文档 Step10.2）。``path`` 为相对 ``documents_dir`` 的
    POSIX 路径（``/`` 分隔），是库内唯一身份；树枚举跳过点开头项与符号链接。

    纯数据结构，不依赖 NexusRAG / pandaren，供 manager 与前端协议共用。
    """

    path: str            # 相对 documents_dir 的 POSIX 路径（/ 分隔），唯一身份
    name: str            # 末段名
    is_dir: bool = False
    size: int = 0        # 文件字节，目录为 0
    suffix: str = ""     # 小写含点，目录为 ''
    mtime: float = 0.0   # 最后修改时间戳
    children: list["DocNode"] | None = None  # 目录才有（目录优先 + 名称升序）

    def to_dict(self) -> dict[str, Any]:
        """递归序列化为线格式 dict（children 为 None 时保持 None）。"""
        return asdict(self)


@dataclass
class KBConfig:
    """知识库配置（持久化到 kbs.toml 的一条）。"""

    name: str
    description: str = ""
    documents_dir: str = ""  # 文档目录（绝对路径）

    # ── Embedding（独立填写 API Key，不复用 LLM 凭据）──
    embedding_api_key: str = ""         # embedding 服务的 API Key
    embedding_model: str = ""           # embedding 模型名（如 text-embedding-v3 / v4）
    embedding_api_type: str = "text"    # "text"（OpenAI 兼容，纯文本）/ "multimodal"
    embedding_api_url: str = ""         # embedding API 完整 URL（OpenAI 兼容 /v1/embeddings）
    embedding_dimension: int = 1024     # 向量维度（阿里通用文本向量默认 1024）

    # ── 抽取 LLM（建库实体/关键词抽取 + 检索分词；独立配置，不复用对话 LLM）──
    llm_provider: str = ""   # provider id（dashscope / openai / deepseek / volcengine）
    llm_api_key: str = ""    # 抽取 LLM 的 API Key
    llm_model: str = ""      # 抽取 LLM 模型名
    llm_api_url: str = ""    # base_url（可选；空则走 provider catalog 默认）

    # ── 索引选项 ──
    enable_bm25: bool = True
    # 二期占位（需求 R3.2）：仅透传配置，本期建库不生效（builder 中 enable_graph 恒为 False）
    enable_graph: bool = False

    # ── 对话集成 ──
    enabled_in_chat: bool = True   # 在对话中启用（PRD §7.5）
    auto_rebuild: bool = True      # 文档变更后自动增量更新（需求 R3.5，默认开）
    top_k: int = 5                 # 引用来源召回数（PRD P2）

    # ── 持久化状态（building 不进这里，运行时覆盖）──
    status: str = KBStatus.EMPTY.value

    @property
    def db_path(self) -> str:
        """向量索引目录（chroma），派生自 documents_dir 的兄弟目录。"""
        return self.documents_dir + "_index"

    @property
    def embedding_api_url_full(self) -> str:
        """规范化的 embedding API 完整 URL（确保以 /embeddings 结尾）。

        NexusRAG 直接 POST 到该 URL，不会自动补 /embeddings 后缀；
        这里容错：用户填到 /v1 时自动补 /embeddings。
        """
        url = (self.embedding_api_url or "").strip()
        if not url:
            return url
        if url.rstrip("/").endswith("/embeddings"):
            return url
        return url.rstrip("/") + "/embeddings"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KBConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def validate(self) -> None:
        """校验配置合法性；非法抛 ValueError（§九 ID 类字段缺失即失败）。"""
        name = (self.name or "").strip()
        if not name:
            raise ValueError("知识库名称不能为空")
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("知识库名称只能包含字母、数字、中文、下划线或连字符")
        if not self.documents_dir or not self.documents_dir.strip():
            raise ValueError("文档目录不能为空")
        if not self.embedding_api_key or not self.embedding_api_key.strip():
            raise ValueError("必须填写 Embedding API Key")
        if not self.embedding_model or not self.embedding_model.strip():
            raise ValueError("必须填写 Embedding 模型名")
        if not self.embedding_api_url or not self.embedding_api_url.strip():
            raise ValueError("必须填写 Embedding API 地址")
        if not self.embedding_api_url.startswith(("http://", "https://")):
            raise ValueError("Embedding API 地址必须以 http:// 或 https:// 开头")
        if self.embedding_api_type not in ("text", "multimodal"):
            raise ValueError("Embedding API 类型必须是 text 或 multimodal")
        if self.embedding_dimension not in SUPPORTED_EMBEDDING_DIMENSIONS:
            raise ValueError(f"向量维度必须是 {SUPPORTED_EMBEDDING_DIMENSIONS} 之一")
        if not self.llm_provider or not self.llm_provider.strip():
            raise ValueError("必须填写抽取 LLM 的 provider")
        if not self.llm_api_key or not self.llm_api_key.strip():
            raise ValueError("必须填写抽取 LLM 的 API Key")
        if not self.llm_model or not self.llm_model.strip():
            raise ValueError("必须填写抽取 LLM 的模型名")
        if self.top_k < 1 or self.top_k > 20:
            raise ValueError("引用来源数量必须在 1~20 之间")
