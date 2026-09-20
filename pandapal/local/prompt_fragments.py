"""工作区 Prompt 片段文件 —— 统一加载 + 按内容 hash 热重载。

设计见计划 prompt文件片段注入与热重载。

- 工作区根目录下的 .md 片段按模式注入 system prompt：

    | 文件              | 注入模式 |
    |-------------------|----------|
    | PANDAPAL.md       | coding   |
    | CODING_RULES.md   | coding   |
    | soul.md           | office   |
    | OFFICE_RULES.md   | office   |

- 加载时机：每次取用时按内容 sha256 检测变化，**变了才**重建缓存并返回新字符串；
  内容未变 → 返回同一字符串（字节稳定）→ 调用方（SessionAgentPool）无需 rebind，
  prompt cache 全程命中。
- 文件名**大小写不敏感**匹配（Linux 敏感、macOS/Windows 不敏感，此处显式统一）。
- 读文件失败：**保留旧值** + warning 留痕，绝不把 prompt 清空、绝不炸 run
  （健壮性契约 §九：展示/辅助类可回落，但至少留痕）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FragmentSpec:
    """一个工作区 prompt 片段的声明。

    - filename: 规范文件名（大小写不敏感匹配时以其 lower() 为准）
    - modes:    归属模式集合，如 frozenset({"coding"})
    - heading:  注入时的 Markdown 标题（让 LLM 知道这段是什么）
    - order:    同模式内注入顺序（升序）
    """

    filename: str
    modes: frozenset[str]
    heading: str
    order: int = 0


#: 内置片段表（本需求）。
DEFAULT_FRAGMENTS: tuple[FragmentSpec, ...] = (
    FragmentSpec("PANDAPAL.md", frozenset({"coding"}), "## 项目指引（PANDAPAL.md）", 10),
    FragmentSpec("CODING_RULES.md", frozenset({"coding"}), "## 编码规范（CODING_RULES.md）", 20),
    FragmentSpec("soul.md", frozenset({"office"}), "## 用户性格与偏好（soul.md）", 10),
    FragmentSpec("OFFICE_RULES.md", frozenset({"office"}), "## 办公规范（OFFICE_RULES.md）", 20),
)


class PromptAssembler:
    """按模式组装 system prompt，支持工作区片段文件的热重载。

    组合规则：``{base_prompts[mode]} + {env_block} + 该 mode 的片段块``，
    片段块形如 ``"{heading}\\n\\n{content}"``，块间以 ``"\\n\\n"`` 连接。

    线程/协程安全：所有方法为同步纯函数式（读文件 + 更新自身 dict），
    在 asyncio 单线程事件循环内调用无数据竞争。
    """

    def __init__(
        self,
        *,
        base_prompts: Mapping[str, str],
        env_block: str,
        fragments: Sequence[FragmentSpec] = DEFAULT_FRAGMENTS,
        work_dir: Path,
    ) -> None:
        self._base_prompts: dict[str, str] = dict(base_prompts)
        self._env_block = env_block
        self._fragments: tuple[FragmentSpec, ...] = tuple(
            sorted(fragments, key=lambda f: f.order)
        )
        self._work_dir = Path(work_dir)
        # spec.filename → 最近成功读取的内容 / 内容 sha256
        self._fragment_text: dict[str, str] = {}
        self._fragment_hash: dict[str, str] = {}
        # mode → 完整 prompt（仅在片段变化时重建）
        self._prompt_cache: dict[str, str] = {}
        self._refresh(force=True)

    # ─── 对外接口 ────────────────────────────────────────────────

    def get(self, mode: str | None) -> str | None:
        """返回该 mode 的完整 prompt（内部按需刷新）；mode 非法/NULL 返回 None。"""
        if mode not in self._base_prompts:
            return None
        self.refresh()
        return self._prompt_cache.get(mode)

    def refresh(self) -> bool:
        """检测片段文件是否变化，变了重建 prompt 缓存；返回是否有变化。"""
        return self._refresh(force=False)

    # ─── 内部实现 ────────────────────────────────────────────────

    def _refresh(self, *, force: bool) -> bool:
        index = self._scan_dir()
        changed = False
        for spec in self._fragments:
            path = index.get(spec.filename.lower())
            try:
                text = path.read_text(encoding="utf-8") if path is not None else ""
            except Exception:
                # 读失败：保留上次成功内容，留痕。首次读失败则记为缺失（空）。
                logger.warning(
                    "[PromptAssembler] 读取片段失败，保留上次内容: %s", path, exc_info=True,
                )
                if spec.filename not in self._fragment_hash:
                    self._fragment_text[spec.filename] = ""
                    self._fragment_hash[spec.filename] = sha256(b"").hexdigest()
                    changed = True
                continue

            h = sha256(text.encode("utf-8")).hexdigest()
            if force or self._fragment_hash.get(spec.filename) != h:
                self._fragment_text[spec.filename] = text
                self._fragment_hash[spec.filename] = h
                changed = True

        if changed:
            self._prompt_cache = {
                mode: self._build_prompt(mode) for mode in self._base_prompts
            }
        return changed

    def _scan_dir(self) -> dict[str, Path]:
        """一次遍历工作区根目录，返回 {小写文件名: Path}（大小写不敏感索引）。

        同名仅大小写不同时，取排序后的首个（确定性）。
        """
        d = self._work_dir
        try:
            if not d.is_dir():
                return {}
        except OSError:
            return {}

        index: dict[str, Path] = {}
        try:
            children = sorted(d.iterdir())
        except OSError:
            logger.warning("[PromptAssembler] 目录遍历失败: %s", d, exc_info=True)
            return index

        for child in children:
            try:
                if child.is_file():
                    index.setdefault(child.name.lower(), child)
            except OSError:
                continue
        return index

    def _build_prompt(self, mode: str) -> str:
        parts: list[str] = [self._base_prompts.get(mode, ""), self._env_block.rstrip()]
        for spec in self._fragments:
            if mode in spec.modes:
                text = self._fragment_text.get(spec.filename, "").strip()
                if text:
                    parts.append(f"{spec.heading}\n\n{text}")
        return "\n\n".join(p for p in parts if p)
