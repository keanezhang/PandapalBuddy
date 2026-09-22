"""pandapal/knowledge_base/manager.py — 知识库生命周期编排。

职责（对齐 MCP 的 McpManager 模式）：
- 多知识库 engine 生命周期（RAGEngineManager，懒加载）
- CRUD + 文档上传 + 建库长任务编排 + 检索
- 独占 ``KB_*`` 出站事件发射（handler 只解析 payload → 调本模块方法 → return None）
- ``get_tools()`` 暴露对话检索工具（供 app.py 自动发现注册）

NexusRAG（``rag``）为重量依赖，全部延迟导入；未安装时知识库功能降级不可用。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, TYPE_CHECKING

from pandapal.events.normalized import NormalizedEvent
from pandapal.knowledge_base.config_store import KnowledgeBaseConfigStore
from pandapal.knowledge_base.models import (
    DocNode,
    KBConfig,
    KBStatus,
    MAX_FILE_SIZE_MB,
    MAX_KB_COUNT,
    SUPPORTED_EMBEDDING_DIMENSIONS,
    SUPPORTED_SUFFIXES,
    collection_name_for,
)

if TYPE_CHECKING:
    from pandapal.broadcast.broadcaster import MessageBroadcast

logger = logging.getLogger(__name__)


class KnowledgeBaseError(Exception):
    """manager 层业务异常，携带错误码（``code``）。

    仅 manager 内部抛出；由应用层 ``_kb_guard`` 捕获并转 ``global_error(code, detail)``。
    manager 之外的层不 catch（§九：中间层无 broad except）。
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


class KnowledgeBaseManager:
    """编排知识库生命周期 + 建库 + 检索 + 事件发射。"""

    def __init__(
        self,
        *,
        config_store: KnowledgeBaseConfigStore,
        broadcast: "MessageBroadcast",
    ) -> None:
        self._config_store = config_store
        self._broadcast = broadcast

        self._lock = asyncio.Lock()
        # name -> RAGEngine（延迟导入，见 _ensure_rag）
        self._engines: dict[str, Any] = {}
        self._engine_loaded: set[str] = set()
        # 建库长任务 + 错误
        self._build_tasks: dict[str, asyncio.Task] = {}
        self._build_errors: dict[str, str] = {}
        # 建库取消信号（接通 builder 的 is_cancelled 检查点）
        self._cancel_flags: dict[str, bool] = {}

    # ══════════════════════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """懒加载：不在此处做重活，engine 首次检索/查询时按需加载。"""

    async def stop(self) -> None:
        """取消建库任务 + 关闭所有 engine（退出路径绝不抛）。"""
        for task in self._build_tasks.values():
            if not task.done():
                task.cancel()
        for name, engine in list(self._engines.items()):
            try:
                engine.close()
            except Exception:  # noqa: BLE001
                logger.warning("关闭知识库 engine %s 失败", name, exc_info=True)
        self._engines.clear()
        self._engine_loaded.clear()

    def get_tools(self) -> list[Any]:
        """暴露知识库对话工具（检索 + 管理），供 app.py 自动发现并注册。"""
        from pandapal.knowledge_base.tool import (
            build_add_documents_tool,
            build_build_tool,
            build_delete_tool,
            build_list_tool,
            build_search_tool,
        )

        return [
            build_search_tool(self),
            build_list_tool(self),
            build_build_tool(self),
            build_add_documents_tool(self),
            build_delete_tool(self),
        ]

    def list_kbs_text(self) -> str:
        """人类可读的知识库清单（供 Agent 工具/LLM 阅读）。"""
        kbs = self.list_kbs()
        if not kbs:
            return "当前没有任何知识库。"
        lines = [f"共 {len(kbs)} 个知识库："]
        for kb in kbs:
            line = (
                f"- {kb['name']}｜状态：{kb['status']}"
                f"｜文档：{kb['document_count']} 个"
                f"｜对话启用：{'是' if kb['enabled_in_chat'] else '否'}"
            )
            if kb.get("description"):
                line += f"｜说明：{kb['description']}"
            lines.append(line)
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════
    # 查询 / 应答发射
    # ══════════════════════════════════════════════════════════════════

    def list_kbs(self) -> list[dict]:
        """所有知识库的摘要列表（含运行时状态推导）。"""
        return [self._summary(cfg) for cfg in self._config_store.load_all()]

    def get_kb(self, name: str) -> dict | None:
        """单库详情（摘要 + 完整 config + 文档列表）；未配置返回 None。"""
        cfg = self._require_cfg(name)
        if cfg is None:
            return None
        detail = self._summary(cfg)
        detail["config"] = cfg.to_dict()
        detail["documents"] = self._list_documents(cfg)
        return detail

    async def emit_list(self) -> None:
        await self._emit(NormalizedEvent.kb_list_result(self.list_kbs()))

    async def emit_get(self, name: str) -> None:
        detail = self.get_kb(name)
        if detail is None:
            await self._emit(
                NormalizedEvent.global_error("kb_not_found", f"知识库 {name!r} 不存在")
            )
            return
        await self._emit(NormalizedEvent.kb_get_result(detail))

    # ══════════════════════════════════════════════════════════════════
    # CRUD
    # ══════════════════════════════════════════════════════════════════

    async def create_kb(self, cfg_dict: dict) -> None:
        """创建知识库（落盘 + 建 documents_dir）；超上限或重名时发 ERROR。"""
        async with self._lock:
            existing = self._config_store.load_all()
            if len(existing) >= MAX_KB_COUNT:
                await self._emit(
                    NormalizedEvent.global_error("kb_limit", f"知识库数量已达上限（{MAX_KB_COUNT} 个）")
                )
                return
            try:
                cfg = KBConfig.from_dict(cfg_dict)
                # documents_dir 由后端自动派生（前端不感知目录结构）
                if not (cfg.documents_dir or "").strip():
                    cfg.documents_dir = str(
                        Path(self._config_store.path).parent / cfg.name / "documents"
                    )
                cfg.status = KBStatus.EMPTY.value
                cfg.validate()
            except Exception as exc:  # noqa: BLE001
                await self._emit(NormalizedEvent.global_error("kb_config_invalid", str(exc)))
                return
            if any(c.name == cfg.name for c in existing):
                await self._emit(
                    NormalizedEvent.global_error("kb_duplicate", f"知识库 {cfg.name!r} 已存在")
                )
                return
            Path(cfg.documents_dir).mkdir(parents=True, exist_ok=True)
            self._config_store.save(cfg)
            await self._emit(NormalizedEvent.kb_saved(cfg.name))

    async def save_kb(self, cfg_dict: dict) -> None:
        """更新配置（不重触发建库；名称即主键）。"""
        try:
            cfg = KBConfig.from_dict(cfg_dict)
            cfg.validate()
        except Exception as exc:  # noqa: BLE001
            await self._emit(NormalizedEvent.global_error("kb_config_invalid", str(exc)))
            return
        old = self._config_store.get(cfg.name)
        if old is None:
            await self._emit(NormalizedEvent.global_error("kb_not_found", f"知识库 {cfg.name!r} 不存在"))
            return
        # 保留持久化状态（除非文档目录变更，则回 pending）
        cfg.status = KBStatus.PENDING.value if cfg.documents_dir != old.documents_dir else old.status
        self._config_store.save(cfg)
        await self._emit(NormalizedEvent.kb_saved(cfg.name))

    async def delete_kb(self, name: str) -> None:
        """删除知识库（配置 + 索引 + 文档目录）。"""
        async with self._lock:
            cfg = self._require_cfg(name)
            if cfg is None:
                await self._emit(NormalizedEvent.global_error("kb_not_found", f"知识库 {name!r} 不存在"))
                return
            # 取消进行中的建库 + 关闭 engine
            task = self._build_tasks.pop(name, None)
            if task is not None and not task.done():
                task.cancel()
            engine = self._engines.pop(name, None)
            if engine is not None:
                try:
                    engine.close()
                except Exception:  # noqa: BLE001
                    pass
            self._engine_loaded.discard(name)
            self._build_errors.pop(name, None)

            self._config_store.delete(name)
            self._remove_dir(Path(cfg.documents_dir))
            self._remove_dir(Path(cfg.db_path))
            await self._emit(NormalizedEvent.kb_deleted(name))

    # ══════════════════════════════════════════════════════════════════
    # 文档管理
    # ══════════════════════════════════════════════════════════════════

    def list_documents(self, name: str) -> list[dict]:
        cfg = self._require_cfg(name)
        return self._list_documents(cfg) if cfg else []

    async def upload_documents(
        self, name: str, source_paths: list[str], target_dir: str = ""
    ) -> None:
        """把本地文件复制到库内 ``target_dir``（相对路径；默认根目录）。

        校验：路径安全（``_resolve_under_documents``）、目标目录存在、格式/大小、
        同名冲突（拒绝，不覆盖）。处理完发 ``kb_documents_changed`` + 全量树；
        确有入库则置 PENDING 并触发自动增量更新。
        """
        self._assert_not_busy(name)
        cfg = self._require_cfg_or_raise(name)
        try:
            dest = self._resolve_under_documents(cfg, target_dir)
        except KnowledgeBaseError as exc:
            await self._emit(NormalizedEvent.global_error("invalid_target", exc.detail))
            return
        if not dest.exists() or not dest.is_dir():
            await self._emit(
                NormalizedEvent.global_error("invalid_target", f"目标目录不存在：{target_dir!r}")
            )
            return

        uploaded: list[dict] = []
        rejected: list[dict] = []
        for sp in source_paths:
            src = Path(sp)
            if not src.is_file():
                rejected.append({"name": src.name, "reason": "文件不存在"})
                continue
            suffix = src.suffix.lower()
            if suffix not in SUPPORTED_SUFFIXES:
                rejected.append({"name": src.name, "reason": f"不支持的格式 {suffix}"})
                continue
            try:
                size_mb = src.stat().st_size / (1024 * 1024)
            except OSError:
                rejected.append({"name": src.name, "reason": "无法读取文件大小"})
                continue
            if size_mb > MAX_FILE_SIZE_MB:
                rejected.append({
                    "name": src.name,
                    "reason": f"文件过大（{size_mb:.1f}MB > {MAX_FILE_SIZE_MB}MB）",
                })
                continue
            if (dest / src.name).exists():
                rejected.append({"name": src.name, "reason": "目标目录已存在同名文件"})
                continue
            try:
                shutil.copy2(src, dest / src.name)
                uploaded.append({"name": src.name, "suffix": suffix})
            except Exception as exc:  # noqa: BLE001
                rejected.append({"name": src.name, "reason": str(exc)})

        if uploaded:
            self._config_store.update_status(name, KBStatus.PENDING.value)
        await self._emit(NormalizedEvent.kb_documents_changed(name, uploaded, rejected))
        await self.emit_tree(name)
        if uploaded:
            # 仅当确有文件入库才触发自动更新；全部被拒时内容未变，不做无谓构建
            await self._maybe_auto_build(cfg)

    async def delete_document(self, name: str, path: str) -> None:
        """删除库内文档（按相对路径；幂等——文件不存在即 no-op）。"""
        self._assert_not_busy(name)
        cfg = self._require_cfg_or_raise(name)
        try:
            target = self._resolve_under_documents(cfg, path)
        except KnowledgeBaseError as exc:
            await self._emit(NormalizedEvent.global_error("invalid_path", exc.detail))
            return
        deleted = False
        if target.is_file():
            try:
                target.unlink()
                deleted = True
            except Exception as exc:  # noqa: BLE001
                await self._emit(NormalizedEvent.global_error("kb_delete_doc_failed", str(exc)))
                return
        if deleted:
            self._config_store.update_status(name, KBStatus.PENDING.value)
            await self._emit(NormalizedEvent.kb_documents_changed(name, [], []))
        await self.emit_tree(name)
        if deleted:
            await self._maybe_auto_build(cfg)

    async def _maybe_auto_build(self, cfg: KBConfig) -> None:
        """文档变更后的自动增量更新（R2.3 / R3.5）。

        ``cfg.auto_rebuild`` 为真且库内仍有文档时触发一次**增量**构建
        （``rebuild=False``）；关闭时维持既有「仅置 PENDING、等用户手动建库」行为。
        空库不触发——``build_kb`` 会对空库发 ``KB_BUILD_FAILED``，这里提前挡掉，
        避免删除最后一个文档时弹出无意义的失败提示。进行中的建库由 ``build_kb``
        内部忽略（重复触发保护），此处不重复判断。
        """
        if not cfg.auto_rebuild:
            return
        if not self._list_documents(cfg):
            return
        await self.build_kb(cfg.name, rebuild=False)

    async def save_text_as_document(
        self,
        name: str,
        filename: str,
        content: str,
        auto_build: bool = False,
    ) -> None:
        """把一段纯文本（如对话内容）保存为库内 .md 文档。

        文件名经 `_safe_filename` 清洗（防路径穿越），冲突则自动加数字后缀。
        写入后把库置回 ``PENDING`` 并发 ``kb_documents_changed``；``auto_build=True``
        时随即触发一次（默认增量）构建。
        """
        cfg = self._require_cfg(name)
        if cfg is None:
            await self._emit(
                NormalizedEvent.global_error("kb_not_found", f"知识库 {name!r} 不存在")
            )
            return
        dest = Path(cfg.documents_dir)
        dest.mkdir(parents=True, exist_ok=True)
        target = self._unique_target(dest, self._safe_filename(filename))
        try:
            target.write_text(content or "", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            await self._emit(NormalizedEvent.global_error("kb_save_text_failed", str(exc)))
            return
        self._config_store.update_status(name, KBStatus.PENDING.value)
        await self._emit(
            NormalizedEvent.kb_documents_changed(
                name, [{"name": target.name, "suffix": target.suffix.lower()}], []
            )
        )
        await self.emit_tree(name)
        if auto_build:
            await self.build_kb(name, rebuild=False)

    # ══════════════════════════════════════════════════════════════════
    # 文档树组织（F8–F13，FS 即真相源）
    # ══════════════════════════════════════════════════════════════════

    async def emit_tree(self, name: str) -> None:
        """扫描 documents_dir 发 KB_TREE_RESULT（全量树）；库不存在 → KnowledgeBaseError。

        扫描在 ``asyncio.to_thread`` 中执行，避免大目录阻塞事件循环。
        """
        cfg = self._require_cfg_or_raise(name)
        nodes = await asyncio.to_thread(self._list_tree, cfg)
        await self._emit(
            NormalizedEvent.kb_tree_result(name, [n.to_dict() for n in nodes])
        )

    async def create_folder(self, name: str, parent_path: str, folder_name: str) -> None:
        """在 ``parent_path``（""=根）下新建空文件夹；校验 → mkdir → emit_tree。

        不改索引状态（空文件夹不入索引）。
        """
        self._assert_not_busy(name)
        cfg = self._require_cfg_or_raise(name)
        safe_name = self._validate_entry_name(folder_name)
        parent = self._resolve_under_documents(cfg, parent_path)
        if not parent.exists():
            raise KnowledgeBaseError("path_not_found", f"父目录不存在：{parent_path!r}")
        if not parent.is_dir():
            raise KnowledgeBaseError("path_not_found", f"{parent_path!r} 不是目录")
        target = parent / safe_name
        if target.exists():
            raise KnowledgeBaseError("name_conflict", f"已存在同名项：{safe_name}")
        async with self._lock:
            try:
                await asyncio.to_thread(target.mkdir, exist_ok=False)
            except OSError as exc:
                raise KnowledgeBaseError("io_error", str(exc)) from exc
        logger.info("知识库 %s 新建文件夹 %s/%s", name, parent_path, safe_name)
        await self.emit_tree(name)

    async def rename_folder(self, name: str, path: str, new_name: str) -> None:
        """重命名文件夹；不改索引状态（文件夹不入索引）→ emit_tree。"""
        self._assert_not_busy(name)
        cfg = self._require_cfg_or_raise(name)
        if not str(path or "").strip():
            raise KnowledgeBaseError("invalid_path", "根目录不可重命名")
        src = self._resolve_under_documents(cfg, path)
        if not src.exists():
            raise KnowledgeBaseError("path_not_found", f"目标不存在：{path!r}")
        if not src.is_dir():
            raise KnowledgeBaseError("path_not_found", f"{path!r} 不是文件夹")
        safe_name = self._validate_entry_name(new_name)
        target = src.parent / safe_name
        if target != src and target.exists():
            raise KnowledgeBaseError("name_conflict", f"已存在同名项：{safe_name}")
        async with self._lock:
            try:
                await asyncio.to_thread(src.rename, target)
            except OSError as exc:
                raise KnowledgeBaseError("io_error", str(exc)) from exc
        logger.info("知识库 %s 重命名文件夹 %s → %s", name, path, safe_name)
        await self.emit_tree(name)

    async def delete_folder(self, name: str, path: str) -> None:
        """递归删除文件夹（含内容）；幂等（不存在即 no-op）→ 置 PENDING + 可选增量 → emit_tree。"""
        self._assert_not_busy(name)
        cfg = self._require_cfg_or_raise(name)
        if not str(path or "").strip():
            raise KnowledgeBaseError("invalid_path", "根目录不可删除")
        target = self._resolve_under_documents(cfg, path)
        deleted = False
        if target.exists():
            if not target.is_dir():
                raise KnowledgeBaseError("path_not_found", f"{path!r} 不是文件夹")
            async with self._lock:
                try:
                    await asyncio.to_thread(shutil.rmtree, target)
                    deleted = True
                except OSError as exc:
                    raise KnowledgeBaseError("io_error", str(exc)) from exc
        if deleted:
            self._config_store.update_status(name, KBStatus.PENDING.value)
            logger.info("知识库 %s 递归删除文件夹 %s", name, path)
        await self.emit_tree(name)
        if deleted:
            await self._maybe_auto_build(cfg)

    async def rename_document(self, name: str, path: str, new_name: str) -> None:
        """重命名文件；置 PENDING + 可选增量 → emit_tree。"""
        self._assert_not_busy(name)
        cfg = self._require_cfg_or_raise(name)
        src = self._resolve_under_documents(cfg, path)
        if not src.exists():
            raise KnowledgeBaseError("path_not_found", f"目标不存在：{path!r}")
        if not src.is_file():
            raise KnowledgeBaseError("path_not_found", f"{path!r} 不是文件")
        safe_name = self._validate_entry_name(new_name)
        target = src.parent / safe_name
        if target != src and target.exists():
            raise KnowledgeBaseError("name_conflict", f"已存在同名项：{safe_name}")
        async with self._lock:
            try:
                await asyncio.to_thread(src.rename, target)
            except OSError as exc:
                raise KnowledgeBaseError("io_error", str(exc)) from exc
        self._config_store.update_status(name, KBStatus.PENDING.value)
        logger.info("知识库 %s 重命名文档 %s → %s", name, path, safe_name)
        await self.emit_tree(name)
        await self._maybe_auto_build(cfg)

    async def move_entry(self, name: str, source_path: str, target_dir: str) -> None:
        """移动文件/文件夹到目标目录（防环）；置 PENDING + 可选增量 → emit_tree。

        source_path 为空（根）、目标不存在/非目录、或把目录移入自身子孙 → 拒绝。
        """
        self._assert_not_busy(name)
        cfg = self._require_cfg_or_raise(name)
        if not str(source_path or "").strip():
            raise KnowledgeBaseError("invalid_path", "根目录不可移动")
        src = self._resolve_under_documents(cfg, source_path)
        dest_dir = self._resolve_under_documents(cfg, target_dir)
        if not src.exists():
            raise KnowledgeBaseError("path_not_found", f"源不存在：{source_path!r}")
        if not dest_dir.exists() or not dest_dir.is_dir():
            raise KnowledgeBaseError("invalid_target", f"目标目录不存在：{target_dir!r}")
        target = dest_dir / src.name
        if target == src:
            await self.emit_tree(name)  # 已在目标目录，无变化
            return
        if src.is_dir() and dest_dir.is_relative_to(src):
            raise KnowledgeBaseError("invalid_target", "不能移动到自身或其子孙目录")
        if target.exists():
            raise KnowledgeBaseError("name_conflict", f"目标已存在同名项：{src.name}")
        async with self._lock:
            try:
                await asyncio.to_thread(shutil.move, str(src), str(target))
            except OSError as exc:
                raise KnowledgeBaseError("io_error", str(exc)) from exc
        self._config_store.update_status(name, KBStatus.PENDING.value)
        logger.info("知识库 %s 移动 %s → %s", name, source_path, target_dir)
        await self.emit_tree(name)
        await self._maybe_auto_build(cfg)

    # ══════════════════════════════════════════════════════════════════
    # 建库
    # ══════════════════════════════════════════════════════════════════

    async def build_kb(self, name: str, rebuild: bool = False) -> None:
        """触发建库（异步后台任务；重复触发时忽略进行中的任务）。"""
        cfg = self._require_cfg(name)
        if cfg is None:
            await self._emit(NormalizedEvent.global_error("kb_not_found", f"知识库 {name!r} 不存在"))
            return
        if not self._list_documents(cfg):
            await self._emit(
                NormalizedEvent.kb_build_failed(name, "文档目录为空，请先上传文档再建库")
            )
            return
        if name in self._build_tasks and not self._build_tasks[name].done():
            logger.info("知识库 %s 正在建库，忽略重复触发", name)
            return
        self._build_errors.pop(name, None)
        task = asyncio.create_task(self._run_build_task(cfg, rebuild))
        self._build_tasks[name] = task

    async def build_and_wait(self, name: str, rebuild: bool = False) -> str:
        """触发建库并等待完成，返回人类可读结果（供 Agent 工具 ``build_knowledge_base``）。

        与 ``build_kb``（fire-and-forget）不同，本方法会 await 后台任务直到结束，
        从而把「是否成功 / 失败原因」以文本反馈给 LLM。``_run_build_task`` 内部
        已捕获异常并写入 ``_build_errors``，故此处 await 正常返回后据其判定结果。
        """
        cfg = self._require_cfg(name)
        if cfg is None:
            return f"知识库 {name!r} 不存在。"
        if not self._list_documents(cfg):
            return f"知识库 {name!r} 文档目录为空，请先上传文档再构建。"
        await self.build_kb(name, rebuild=rebuild)
        task = self._build_tasks.get(name)
        if task is None:
            # build_kb 提前返回（文档为空已在上面拦截）——只可能是并发已在进行中
            return f"知识库 {name!r} 正在构建中，请稍后再试。"
        await task
        err = self._build_errors.get(name)
        if err:
            return f"构建知识库 {name!r} 失败：{err}"
        return f"知识库 {name!r} 构建完成（{'全量' if rebuild else '增量'}），现可检索。"

    async def cancel_build(self, name: str) -> None:
        """取消进行中的建库：先置取消信号让 builder 尽快退出，再兜底 cancel task。"""
        self._cancel_flags[name] = True
        task = self._build_tasks.get(name)
        if task is not None and not task.done():
            task.cancel()
            await self._emit(NormalizedEvent.kb_build_failed(name, "已取消"))
            self._config_store.update_status(name, KBStatus.PENDING.value)

    async def _run_build_task(self, cfg: KBConfig, rebuild: bool) -> None:
        from pandapal.knowledge_base.builder import run_build
        from pandapal.knowledge_base.rag_llm_provider import build_provider

        name = cfg.name
        self._cancel_flags.pop(name, None)  # 新任务：清上次取消信号
        try:
            embedding_key = (cfg.embedding_api_key or "").strip()
            if not embedding_key:
                raise RuntimeError("未填写 Embedding API Key")
            provider = build_provider(
                provider=cfg.llm_provider,
                api_key=cfg.llm_api_key,
                model=cfg.llm_model,
                base_url=cfg.llm_api_url,
            )

            async def _progress(stage: str, percent: int, message: str) -> None:
                await self._emit(
                    NormalizedEvent.kb_build_progress(
                        name, stage, percent, message, incremental=not rebuild
                    )
                )

            await run_build(
                config=cfg,
                llm_provider=provider,
                embedding_api_key=embedding_key,
                rebuild=rebuild,
                progress_cb=_progress,
                is_cancelled=lambda: self._cancel_flags.get(name, False),
            )
            self._config_store.update_status(name, KBStatus.READY.value)
            self._engine_loaded.discard(name)  # 索引已重建，失效缓存
            await self._emit(NormalizedEvent.kb_build_done(name))
        except asyncio.CancelledError:
            self._config_store.update_status(name, KBStatus.PENDING.value)
            await self._emit(NormalizedEvent.kb_build_failed(name, "已取消"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("知识库 %s 建库失败", name)
            self._build_errors[name] = str(exc)
            self._config_store.update_status(name, KBStatus.FAILED.value)
            await self._emit(NormalizedEvent.kb_build_failed(name, str(exc)))
        finally:
            self._cancel_flags.pop(name, None)

    # ══════════════════════════════════════════════════════════════════
    # 检索
    # ══════════════════════════════════════════════════════════════════

    async def search(self, name: str, query: str, k: int = 5) -> dict | None:
        """独立检索（前端检索测试页用），返回结构化结果。"""
        cfg = self._require_cfg(name)
        if cfg is None:
            return None
        results = await self._do_search(cfg, query, k)
        return {"knowledge_base": name, "query": query, "count": len(results), "results": results}

    async def emit_search(self, name: str, query: str, k: int = 5) -> None:
        """发射 KB_SEARCH_RESULT（handler 调用，保持事件 Owner 唯一）。"""
        result = await self.search(name, query, k)
        if result is None:
            await self._emit(NormalizedEvent.global_error("kb_not_found", f"知识库 {name!r} 不存在"))
            return
        await self._emit(NormalizedEvent.kb_search_result(result))

    async def search_for_llm(self, query: str, knowledge_base: str | None = None, k: int = 5) -> str:
        """对话工具入口：检索一个或所有已启用库，返回给 LLM 的文本。"""
        targets: list[KBConfig] = []
        for cfg in self._config_store.load_all():
            if self._derive_status(cfg) != KBStatus.READY.value:
                continue
            if not cfg.enabled_in_chat:
                continue
            if knowledge_base is not None and cfg.name != knowledge_base:
                continue
            targets.append(cfg)

        if not targets:
            return "当前没有可用的知识库（可能尚未建库或未在对话中启用）。"

        parts: list[str] = []
        for cfg in targets:
            try:
                results = await self._do_search(cfg, query, min(k, cfg.top_k or k))
            except Exception as exc:  # noqa: BLE001
                logger.warning("检索知识库 %s 失败: %s", cfg.name, exc)
                continue
            if not results:
                continue
            parts.append(f"【知识库：{cfg.name}】")
            for i, r in enumerate(results, 1):
                title = r.get("title") or r.get("source") or f"片段{i}"
                chapter = r.get("chapter_title") or ""
                loc = f"{title}" + (f" · {chapter}" if chapter else "")
                parts.append(f"[{i}] {loc}\n{r.get('content', '')}")
        if not parts:
            return "在知识库中未检索到相关内容。"
        return "\n\n".join(parts)

    async def _do_search(self, cfg: KBConfig, query: str, k: int) -> list[dict]:
        """执行检索并序列化为前端可消费的结构。"""
        engine = await self._load_engine(cfg)
        children = await engine.search(query, k=k)

        by_parent: dict[str, list] = {}
        order: list[str] = []
        for c in children:
            pid = c.get("parent_id") or c.get("child_id")
            if pid not in by_parent:
                by_parent[pid] = []
                order.append(pid)
            by_parent[pid].append(c)

        parents = engine.get_parent_documents(order)
        parent_map = {p.get("parent_id"): p for p in parents}

        results: list[dict] = []
        for pid in order:
            parent = parent_map.get(pid) or {}
            meta = parent.get("parent_metadata") or {}
            content = parent.get("parent_content") or ""
            matched = [
                {
                    "content": ch.get("content", ""),
                    "distance": ch.get("distance"),
                    "retrieval_sources": ch.get("retrieval_sources"),
                }
                for ch in by_parent[pid]
            ]
            results.append({
                "parent_id": pid,
                "title": meta.get("title"),
                "source": meta.get("source"),
                "chapter_title": meta.get("chapter_title"),
                "scene_title": meta.get("scene_title"),
                "content": content,
                "matched_children": matched,
            })
        return results

    # ══════════════════════════════════════════════════════════════════
    # 私有
    # ══════════════════════════════════════════════════════════════════

    def _require_cfg(self, name: str) -> KBConfig | None:
        return self._config_store.get(name)

    def _require_cfg_or_raise(self, name: str) -> KBConfig:
        """取配置；不存在 → KnowledgeBaseError('kb_not_found')。"""
        cfg = self._require_cfg(name)
        if cfg is None:
            raise KnowledgeBaseError("kb_not_found", f"知识库 {name!r} 不存在")
        return cfg

    def _assert_not_busy(self, name: str) -> None:
        """结构操作前置闸门：建库进行中 → KnowledgeBaseError('kb_busy')。"""
        task = self._build_tasks.get(name)
        if task is not None and not task.done():
            raise KnowledgeBaseError("kb_busy", "索引构建中，请稍后")

    def _resolve_under_documents(self, cfg: KBConfig, rel_path: str) -> Path:
        """把相对路径安全解析为 documents_dir 下的绝对路径。

        含 ``..`` / 绝对路径 / resolve 后越界 / 空父级 → KnowledgeBaseError('invalid_path')。
        返回的路径**不保证存在**（由调用方判定 exists / is_file / is_dir）。
        """
        rel = str(rel_path or "").strip().replace("\\", "/")
        root = Path(cfg.documents_dir).resolve()
        if rel in ("", "."):
            return root
        p = Path(rel)
        if p.is_absolute():
            raise KnowledgeBaseError("invalid_path", f"必须是相对路径：{rel!r}")
        candidate = (root / rel).resolve()
        if candidate != root and not candidate.is_relative_to(root):
            raise KnowledgeBaseError("invalid_path", f"路径越界：{rel!r}")
        return candidate

    @staticmethod
    def _validate_entry_name(name: str) -> str:
        """校验用户命名（文件/文件夹末段名）；非法 → KnowledgeBaseError('invalid_name')。"""
        raw = str(name or "").strip()
        if not raw or raw in (".", ".."):
            raise KnowledgeBaseError("invalid_name", "名称不能为空")
        if re.search(r'[\\/:*?"<>|\x00-\x1f]', raw):
            raise KnowledgeBaseError("invalid_name", f"名称含非法字符：{raw!r}")
        return raw

    def _list_tree(self, cfg: KBConfig) -> list[DocNode]:
        """递归扫描 documents_dir → DocNode 全量树（目录优先 + 名称升序）。"""
        root = Path(cfg.documents_dir)
        if not root.exists():
            return []
        return self._scan_dir(root, root)

    def _scan_dir(self, base: Path, directory: Path) -> list[DocNode]:
        """扫描单层目录并递归；跳过点开头项与符号链接。"""
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return []
        nodes: list[DocNode] = []
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            try:
                if entry.is_symlink():
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            child = directory / name
            rel = child.relative_to(base).as_posix()
            if entry.is_dir(follow_symlinks=False):
                nodes.append(DocNode(
                    path=rel, name=name, is_dir=True, size=0,
                    suffix="", mtime=st.st_mtime,
                    children=self._scan_dir(base, child),
                ))
            elif entry.is_file(follow_symlinks=False):
                nodes.append(DocNode(
                    path=rel, name=name, is_dir=False, size=st.st_size,
                    suffix=Path(name).suffix.lower(), mtime=st.st_mtime,
                    children=None,
                ))
        # 目录优先 + 名称升序（不区分大小写）
        nodes.sort(key=lambda n: (0 if n.is_dir else 1, n.name.lower()))
        return nodes

    async def _load_engine(self, cfg: KBConfig) -> Any:
        """按需加载（或复用）RAGEngine；同步初始化放入线程池。"""
        if cfg.name in self._engine_loaded and cfg.name in self._engines:
            return self._engines[cfg.name]

        from pandapal.knowledge_base.rag import RAGConfig, RAGEngine

        from pandapal.knowledge_base.rag_llm_provider import build_provider

        embedding_key = (cfg.embedding_api_key or "").strip()
        if not embedding_key:
            raise RuntimeError("未填写 Embedding API Key")

        rag_config = RAGConfig(
            project_root=Path(cfg.documents_dir).parent,
            db_path=Path(cfg.db_path),
            collection_name=collection_name_for(cfg.name),
            documents_dir=Path(cfg.documents_dir),
            use_cloud_embedding=True,
            cloud_embedding_api_key=embedding_key,
            cloud_embedding_multimodal_model_id=cfg.embedding_model,
            cloud_embedding_multimodal_api_full_url=cfg.embedding_api_url_full,
            cloud_embedding_api_type=cfg.embedding_api_type,
            cloud_embedding_dimension=cfg.embedding_dimension,
            supported_dimensions=SUPPORTED_EMBEDDING_DIMENSIONS,
            enable_graph=False,
            enable_bm25=cfg.enable_bm25,
            use_triple_retrieval=False,
            enable_reranker=False,
            intent_llm_provider=build_provider(
                provider=cfg.llm_provider,
                api_key=cfg.llm_api_key,
                model=cfg.llm_model,
                base_url=cfg.llm_api_url,
            ),
            verbose=False,
        )
        engine = RAGEngine(config=rag_config)
        await asyncio.to_thread(engine.initialize)
        self._engines[cfg.name] = engine
        self._engine_loaded.add(cfg.name)
        return engine

    def _summary(self, cfg: KBConfig) -> dict:
        return {
            "name": cfg.name,
            "description": cfg.description,
            "status": self._derive_status(cfg),
            "document_count": len(self._list_documents(cfg)),
            "enabled_in_chat": cfg.enabled_in_chat,
            "auto_rebuild": cfg.auto_rebuild,
            "top_k": cfg.top_k,
            "enable_bm25": cfg.enable_bm25,
        }

    def _derive_status(self, cfg: KBConfig) -> str:
        if cfg.name in self._build_tasks and not self._build_tasks[cfg.name].done():
            return KBStatus.BUILDING.value
        if cfg.status == KBStatus.FAILED.value:
            return KBStatus.FAILED.value
        if not self._list_documents(cfg):
            return KBStatus.EMPTY.value
        if cfg.status == KBStatus.READY.value and self._index_exists(cfg):
            return KBStatus.READY.value
        return KBStatus.PENDING.value

    @staticmethod
    def _index_exists(cfg: KBConfig) -> bool:
        db = Path(cfg.db_path)
        try:
            return db.exists() and any(db.iterdir())
        except OSError:
            return False

    def _list_documents(self, cfg: KBConfig) -> list[dict]:
        """列出库内文档（扁平）。

        ``path`` 为**相对 ``documents_dir`` 的 POSIX 路径**——与 ``DocNode.path``
        同一身份口径（唯一身份，跨平台稳定），可直接作为 ``delete_document`` /
        ``rename_document`` / ``move_entry`` 的入参回传；``name`` 仅供展示。
        """
        d = Path(cfg.documents_dir)
        if not d.exists():
            return []
        files: list[dict] = []
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.suffix.lower() in SUPPORTED_SUFFIXES:
                files.append({
                    "name": f.name,
                    "path": f.relative_to(d).as_posix(),
                    "suffix": f.suffix.lower(),
                    "size": f.stat().st_size,
                })
        return files

    @staticmethod
    def _remove_dir(path: Path) -> None:
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            logger.warning("删除目录失败: %s", path)

    @staticmethod
    def _safe_filename(filename: str) -> str:
        """清洗文件名：仅取末段、去非法字符、强制 .md 后缀（防路径穿越）。"""
        raw = Path(str(filename or "").strip().replace("\\", "/")).name
        stem = Path(raw).stem or "对话记录"
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).strip(" .")
        return f"{cleaned or '对话记录'}.md"

    @staticmethod
    def _unique_target(directory: Path, filename: str) -> Path:
        """在目录内生成不冲突的目标路径（同名则追加 _1/_2…）。"""
        target = directory / filename
        if not target.exists():
            return target
        base, suffix = target.stem, target.suffix
        idx = 1
        while True:
            candidate = directory / f"{base}_{idx}{suffix}"
            if not candidate.exists():
                return candidate
            idx += 1

    async def _emit(self, event: NormalizedEvent) -> None:
        try:
            await self._broadcast.send(event)
        except Exception:  # noqa: BLE001
            logger.warning("知识库事件广播失败: %s", event.event_type.value, exc_info=True)
