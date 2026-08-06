"""scripts/fetch_tiktoken_vocab.py — 拉取 cl100k_base 词表并 vendor 到仓内。

背景：TiktokenEstimator（pandaren/memory/estimators.py）支持 vocab_path 离线加载，
避免 sidecar 首启时走 tiktoken 默认的 Azure blob 下载路径（国内不稳）。
本脚本负责可复现地生成该 vendored 文件：

  1. tiktoken.get_encoding("cl100k_base") 触发官方缓存下载（内部已做 hash 校验）
  2. 从 tiktoken 缓存目录复制到 pandapal/resources/tokenizer/cl100k_base.tiktoken
  3. 用 tiktoken_ext.openai_public 源码里的 expected_hash 二次校验 vendored 文件

用法：
    python scripts/fetch_tiktoken_vocab.py
"""

from __future__ import annotations

import hashlib
import inspect
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ENCODING_NAME = "cl100k_base"
# cl100k_base 词表的官方 blob 地址（与 tiktoken_ext/openai_public.py 中一致）。
BLOB_URL = (
    "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGET = (
    PROJECT_ROOT / "pandapal" / "resources" / "tokenizer" / "cl100k_base.tiktoken"
)


def _expected_hash_from_source() -> str:
    """从已安装的 tiktoken_ext.openai_public 源码提取官方 expected_hash。

    不把 hash 硬编码进本脚本：随 tiktoken 版本升级自动保持一致。
    """
    import tiktoken_ext.openai_public

    src = inspect.getsource(tiktoken_ext.openai_public.cl100k_base)
    m = re.search(r'expected_hash="([0-9a-f]{64})"', src)
    if not m:
        raise RuntimeError("无法从 tiktoken_ext.openai_public 源码提取 expected_hash")
    return m.group(1)


def main() -> int:
    import tiktoken
    from tiktoken.load import load_tiktoken_bpe

    # 1) 触发下载进 tiktoken 缓存（get_encoding 内部链路自带 expected_hash 校验）
    tiktoken.get_encoding(ENCODING_NAME)

    # 2) 定位缓存文件（tiktoken.load.read_file_cached 的缓存键 = sha1(blob_url)）
    cache_dir = os.environ.get("TIKTOKEN_CACHE_DIR") or os.path.join(
        tempfile.gettempdir(), "data-gym-cache"
    )
    cache_key = hashlib.sha1(BLOB_URL.encode()).hexdigest()
    cached = Path(cache_dir) / cache_key
    if not cached.exists():
        raise RuntimeError(f"tiktoken 缓存文件不存在: {cached}")

    # 3) 复制到 vendored 目标
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached, TARGET)

    # 4) 二次校验 vendored 文件完整性（与官方 expected_hash 比对）
    expected_hash = _expected_hash_from_source()
    ranks = load_tiktoken_bpe(str(TARGET), expected_hash=expected_hash)

    size_kb = TARGET.stat().st_size / 1024
    print(f"✅ vendored: {TARGET}")
    print(f"   size={size_kb:.1f}KB ranks={len(ranks)} sha256={expected_hash[:16]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
