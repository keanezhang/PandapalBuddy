"""pandaren/memory/estimators.py — 基于真实 BPE tokenizer 的 Token 估算器

动机（见 docs/analysis/压缩管线排查报告.md）：
默认 ``CharBasedTokenEstimator``（chars / 4.0）对中文/代码系统性低估 ~2x，
导致 ``compact_if_needed`` 触发过晚、实际 LLM 输入超出 conversation 预算。
本模块提供与真实 tokenizer 同量纲的估算实现，供应用层通过
``AgentBuilder.memory(token_estimator=...)`` 注入；SDK 默认实现保持不变（零依赖）。

依赖策略（构建期 fail-fast，不做运行期静默降级）：
  - 未安装 tiktoken           → __init__ 抛 ImportError（提示安装方式）
  - vocab_path 文件缺失/损坏  → __init__ 异常原样抛出（hash 校验）
  是否降级回落 CharBased 由应用层决定（pandapal 走 degradation 统一通道）。

离线加载：应用层可将 ``cl100k_base.tiktoken`` vendored 到包内（pandapal 见
``pandapal/resources/tokenizer/``，由 ``scripts/fetch_tiktoken_vocab.py`` 生成），
避免 ``tiktoken.get_encoding`` 首启走 Azure blob 网络下载。

注意：cl100k_base 对 kimi/qwen/deepseek 等中文优化 BPE 是**近似**（±10-20%），
但相比 chars/4.0 的 ~2x 低估已是数量级修正。需要精确词表时，
应用层可自行实现 TokenEstimator Protocol 注入，本类只是 SDK 内置的高质量实现。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .models import MessageDict
from .protocols import TokenEstimator  # noqa: F401  (re-export 语义：类型注解)

logger = logging.getLogger(__name__)

# ── cl100k_base 构造参数 ─────────────────────────────────────────────
# 以下三个常量逐字复制自 tiktoken_ext/openai_public.py 的 cl100k_base()
# （随 tiktoken 0.13.0 校验），用于绕开 get_encoding 的网络下载、
# 直接从本地 vendored 词表文件构造 Encoding。
_CL100K_BASE_EXPECTED_HASH: str = (
    "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
)
_CL100K_PAT_STR: str = (
    r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+|"""
    r""" ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"""
)
_CL100K_SPECIAL_TOKENS: dict[str, int] = {
    "<|endoftext|>": 100257,
    "<|fim_prefix|>": 100258,
    "<|fim_middle|>": 100259,
    "<|fim_suffix|>": 100260,
    "<|endofprompt|>": 100276,
}


class TiktokenEstimator:
    """基于 tiktoken BPE 的 token 估算（实现 TokenEstimator Protocol）。

    实例不可变、encode 线程安全：可由 Memory factory 跨 session 共享。

    Args:
        encoding_name: tiktoken 编码名。vocab_path 为空时传给 get_encoding；
                       vocab_path 非空时仅支持 "cl100k_base"。
        vocab_path:    本地 .tiktoken 词表文件路径（None = 走 tiktoken
                       自身缓存/网络下载链路）。文件经官方 expected_hash 校验。

    Raises:
        ImportError: 未安装 tiktoken（构建期 fail-fast）。
        ValueError:  vocab_path 非空但 encoding_name 不是 cl100k_base。
        Exception:   词表文件缺失/hash 校验失败（原样抛出，由应用层决定降级）。
    """

    def __init__(
        self,
        encoding_name: str = "cl100k_base",
        *,
        vocab_path: str | Path | None = None,
    ) -> None:
        try:
            import tiktoken
            from tiktoken.load import load_tiktoken_bpe
        except ImportError as e:
            raise ImportError(
                "TiktokenEstimator 需要 tiktoken："
                "pip install tiktoken（或 pandapal-buddy[tokenizer]）"
            ) from e

        if vocab_path is not None:
            if encoding_name != "cl100k_base":
                raise ValueError(
                    f"vocab_path 离线加载仅支持 cl100k_base，got {encoding_name!r}；"
                    "其他编码请不传 vocab_path 走 tiktoken 缓存/网络链路"
                )
            mergeable_ranks = load_tiktoken_bpe(
                str(vocab_path), expected_hash=_CL100K_BASE_EXPECTED_HASH
            )
            self._enc = tiktoken.Encoding(
                name="cl100k_base",
                pat_str=_CL100K_PAT_STR,
                mergeable_ranks=mergeable_ranks,
                special_tokens=_CL100K_SPECIAL_TOKENS,
            )
        else:
            self._enc = tiktoken.get_encoding(encoding_name)

        logger.info(
            "TiktokenEstimator ready: encoding=%s source=%s",
            self._enc.name,
            f"vocab_path={vocab_path}" if vocab_path else "tiktoken cache/network",
        )

    def estimate(self, messages: list[MessageDict]) -> int:
        """估算消息列表的 token 总数（真实 BPE encode，与实际 LLM token 同量纲）。

        文本提取逻辑与 CharBasedTokenEstimator 一致（同一把尺子的输入面）：
        content 为 str 或 list[dict]（取 text 字段），tool_calls 按 JSON 字符串计入。

        ``disallowed_special=()``：工具结果/文件内容中若含 <|endoftext|> 类
        特殊 token 文本，按普通文本编码而非抛错——估算器绝不向热路径抛异常。
        """
        total = 0
        encode = self._enc.encode
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                if content:
                    total += len(encode(content, disallowed_special=()))
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = str(part.get("text", ""))
                    else:
                        text = str(part)
                    if text:
                        total += len(encode(text, disallowed_special=()))
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                total += len(encode(str(tool_calls), disallowed_special=()))
        return max(1, total)
