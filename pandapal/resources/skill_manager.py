"""pandapal/resources/skill_manager.py — Skill 资源管理器

提供 Skill 的查询（system + user）和变更（仅 user/）操作。
构建 NormalizedEvent 返回给调用方（Dispatcher 统一转发），本类不持有广播出口。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import yaml
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from pandaren.skill.loader import load_skill_from_file, load_skills_from_dir
from pandaren.skill.models import SkillSource
from pandapal.events.normalized import EventType, NormalizedEvent

logger = logging.getLogger(__name__)

# 用户自定义 Skill 数量上限（system Skill 不计入）
MAX_USER_SKILLS = 200

# ── DTO ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SkillSummaryDTO:
    """Skill 摘要（列表项）。"""
    name: str
    description: str
    when_to_use: str
    source: str          # "system" | "user"
    type: str            # "knowledge" | "action"
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillDetailDTO(SkillSummaryDTO):
    """Skill 详情（含正文）。"""
    content: str = ""
    allowed_tools: list[str] | None = None
    argument_hint: str | None = None
    script: str | None = None
    entry_function: str | None = None


# ── 辅助 ─────────────────────────────────────────────────────────────────────

def _skill_to_summary(skill, source_label: str) -> SkillSummaryDTO:
    return SkillSummaryDTO(
        name=skill.name,
        description=skill.description,
        when_to_use=skill.when_to_use,
        source=source_label,
        type=skill.skill_type.name.lower(),
        tags=list(skill.tags) if skill.tags else [],
    )


def _skill_to_detail(skill, source_label: str) -> SkillDetailDTO:
    return SkillDetailDTO(
        name=skill.name,
        description=skill.description,
        when_to_use=skill.when_to_use,
        source=source_label,
        type=skill.skill_type.name.lower(),
        tags=list(skill.tags) if skill.tags else [],
        content=skill.content,
        allowed_tools=list(skill.allowed_tools) if skill.allowed_tools else None,
        argument_hint=skill.argument_hint,
        script=skill.script,
        entry_function=skill.entry_function,
    )


def _validate_skill_name(name: str) -> str:
    """校验 Skill name 安全性。只允许 [a-z0-9_-]+，拒绝路径遍历。"""
    if not name or not isinstance(name, str):
        raise ValueError("Skill 名称不能为空")
    if "/" in name or ".." in name or "\\" in name:
        raise ValueError(f"Skill 名称包含非法字符: {name!r}")
    # 只允许小写字母、数字、连字符、下划线
    if not re.fullmatch(r"[a-z0-9_-]+", name):
        raise ValueError(
            f"Skill 名称只能包含小写字母、数字、连字符、下划线: {name!r}"
        )
    return name


# ── SkillManager ─────────────────────────────────────────────────────────────

class SkillManager:
    """Skill 资源管理器。

    system/ — 只读，SkillSource.PROJECT
    user/   — 可 CRUD，SkillSource.USER，同名时覆盖 system
    """

    def __init__(self, system_dir: Path, user_dir: Path):
        self._system_dir = system_dir
        self._user_dir = user_dir

    # ── 查询 ──────────────────────────────────────────────────────────────

    def _load_system_skills(self) -> dict[str, SkillSummaryDTO]:
        """加载 system/ 下所有 Skill 摘要。"""
        if not self._system_dir.is_dir():
            return {}
        skills = load_skills_from_dir(self._system_dir, source=SkillSource.PROJECT)
        return {
            s.name: _skill_to_summary(s, "system")
            for s in skills
        }

    def _load_user_skills(self) -> dict[str, SkillSummaryDTO]:
        """加载 user/ 下所有 Skill 摘要。"""
        if not self._user_dir.is_dir():
            return {}
        skills = load_skills_from_dir(self._user_dir, source=SkillSource.USER)
        return {
            s.name: _skill_to_summary(s, "user")
            for s in skills
        }

    def list_skills(self) -> list[SkillSummaryDTO]:
        """列出所有 Skill（user 同名覆盖 system）。"""
        result = self._load_system_skills()
        result.update(self._load_user_skills())  # user 覆盖
        return sorted(result.values(), key=lambda x: x.name)

    def get_skill(self, name: str) -> SkillDetailDTO | None:
        """获取单个 Skill 详情，先查 user/ 再查 system/。"""
        _validate_skill_name(name)

        # 先查 user/
        user_path = self._user_dir / name / "SKILL.md"
        if user_path.is_file():
            try:
                skill = load_skill_from_file(user_path, source=SkillSource.USER)
                return _skill_to_detail(skill, "user")
            except Exception:
                logger.exception("加载 user Skill 失败: %s", user_path)

        # 再查 system/
        sys_path = self._system_dir / name / "SKILL.md"
        if sys_path.is_file():
            try:
                skill = load_skill_from_file(sys_path, source=SkillSource.PROJECT)
                return _skill_to_detail(skill, "system")
            except Exception:
                logger.exception("加载 system Skill 失败: %s", sys_path)

        return None

    # ── 变更（仅 user/）──────────────────────────────────────────────────

    def create_skill(
        self,
        name: str,
        description: str,
        when_to_use: str,
        content: str,
        tags: list[str] | None = None,
    ) -> SkillDetailDTO:
        """在 user/ 下创建新 Skill。

        Raises:
            ValueError: 名称非法或与系统 Skill 同名。
            FileExistsError: user/ 下已存在同名 Skill。
        """
        _validate_skill_name(name)

        # 数量上限（新建必然新增一个用户 Skill）
        count = len(self._load_user_skills())
        if count >= MAX_USER_SKILLS:
            raise ValueError(
                f"已达到用户 Skill 数量上限（{MAX_USER_SKILLS} 个），"
                f"不支持创建更多，请先删除部分 Skill"
            )

        # 不允许覆盖系统 Skill
        sys_path = self._system_dir / name
        if sys_path.is_dir():
            raise ValueError(f"无法创建与系统 Skill 同名的 Skill: {name!r}")

        user_skill_dir = self._user_dir / name
        if user_skill_dir.exists():
            raise FileExistsError(f"用户 Skill 已存在: {name!r}")

        return self._write_skill_file(name, description, when_to_use, content, tags)

    def update_skill(
        self,
        name: str,
        description: str,
        when_to_use: str,
        content: str,
        tags: list[str] | None = None,
    ) -> SkillDetailDTO:
        """更新 user/ 下的 Skill。

        Raises:
            ValueError: Skill 不存在于 user/ 中。
        """
        _validate_skill_name(name)

        user_skill_dir = self._user_dir / name
        if not user_skill_dir.is_dir():
            raise ValueError(f"用户 Skill 不存在，只能编辑用户 Skill: {name!r}")

        return self._write_skill_file(name, description, when_to_use, content, tags)

    def delete_skill(self, name: str) -> bool:
        """删除 user/ 下的 Skill。

        Raises:
            ValueError: Skill 是系统 Skill 或不存在。
        """
        _validate_skill_name(name)

        user_skill_dir = self._user_dir / name
        if not user_skill_dir.is_dir():
            logger.warning("删除失败，用户 Skill 不存在: %s", name)
            return False

        # 二次确认不在 system/
        sys_path = self._system_dir / name
        if sys_path.is_dir():
            raise ValueError(f"无法删除系统 Skill: {name!r}")

        shutil.rmtree(user_skill_dir)
        logger.info("已删除用户 Skill: %s", name)
        return True

    def _write_skill_file(
        self,
        name: str,
        description: str,
        when_to_use: str,
        content: str,
        tags: list[str] | None = None,
    ) -> SkillDetailDTO:
        """写入 SKILL.md 文件。"""
        user_skill_dir = self._user_dir / name
        user_skill_dir.mkdir(parents=True, exist_ok=True)

        frontmatter = {
            "name": name,
            "description": description,
            "when_to_use": when_to_use,
        }
        if tags:
            frontmatter["tags"] = ", ".join(tags)

        fm_yaml = yaml.dump(frontmatter, allow_unicode=True, default_flow_style=False).strip()
        md_content = f"---\n{fm_yaml}\n---\n\n{content.strip()}\n"

        skill_path = user_skill_dir / "SKILL.md"
        skill_path.write_text(md_content, encoding="utf-8")
        logger.info("写入 Skill 文件: %s", skill_path)

        skill = load_skill_from_file(skill_path, source=SkillSource.USER)
        return _skill_to_detail(skill, "user")

    # ── IPC 推送闭包 ─────────────────────────────────────────────────────

    async def build_skill_list_event(self) -> NormalizedEvent:
        """构建 Skill 列表事件（由 Dispatcher 统一转发）。"""
        skills = self.list_skills()
        skill_data = []
        for s in skills:
            skill_path = (self._user_dir if s.source == "user" else self._system_dir) / s.name / "SKILL.md"
            size = 0
            modified_at = ""
            if skill_path.is_file():
                try:
                    stat = skill_path.stat()
                    size = stat.st_size
                    modified_at = str(stat.st_mtime)
                except OSError:
                    pass
            skill_data.append({
                "name": s.name,
                "description": s.description,
                "when_to_use": s.when_to_use,
                "source": s.source,
                "type": s.type,
                "tags": s.tags,
                "size": size,
                "modified_at": modified_at,
            })
        event = NormalizedEvent(
            event_type=EventType.SKILL_LIST_RESULT,
            payload={"skills": skill_data},
        )
        return event

    async def build_skill_detail_event(self, name: str) -> NormalizedEvent:
        """构建单个 Skill 详情事件（不存在时返回 ERROR 事件）。"""
        skill = self.get_skill(name)
        if skill is None:
            return NormalizedEvent.global_error(
                error_code="skill_not_found",
                error_message=f"Skill 不存在: {name!r}",
            )
        # 计算文件大小和修改时间
        skill_dir = self._user_dir if skill.source == "user" else self._system_dir
        skill_path = skill_dir / name / "SKILL.md"
        size = 0
        modified_at = ""
        if skill_path.is_file():
            try:
                stat = skill_path.stat()
                size = stat.st_size
                modified_at = str(stat.st_mtime)
            except OSError:
                pass
        event = NormalizedEvent(
            event_type=EventType.SKILL_GET_RESULT,
            payload={
                "skill_name": skill.name,
                "description": skill.description,
                "when_to_use": skill.when_to_use,
                "source": skill.source,
                "type": skill.type,
                "tags": skill.tags,
                "content": skill.content,
                "size": size,
                "modified_at": modified_at,
            },
        )
        return event

    async def save_and_build_event(
        self, name: str, payload: dict
    ) -> NormalizedEvent:
        """保存 Skill（创建或更新）并构建结果事件。"""
        try:
            is_new = not (self._user_dir / name).is_dir()
            if is_new:
                skill = self.create_skill(
                    name=name,
                    description=payload.get("description", ""),
                    when_to_use=payload.get("when_to_use", ""),
                    content=payload.get("content", ""),
                    tags=payload.get("tags"),
                )
            else:
                skill = self.update_skill(
                    name=name,
                    description=payload.get("description", ""),
                    when_to_use=payload.get("when_to_use", ""),
                    content=payload.get("content", ""),
                    tags=payload.get("tags"),
                )
            event = NormalizedEvent(
                event_type=EventType.SKILL_SAVED,
                payload={
                    "skill": {
                        "name": skill.name,
                        "description": skill.description,
                        "when_to_use": skill.when_to_use,
                        "source": skill.source,
                        "type": skill.type,
                        "tags": skill.tags,
                    },
                },
            )
            return event
        except Exception as e:
            logger.exception("保存 Skill 失败: %s", name)
            return NormalizedEvent.global_error(
                error_code="skill_save_failed",
                error_message=str(e),
            )

    async def delete_and_build_event(self, name: str) -> NormalizedEvent:
        """删除 Skill 并构建结果事件。"""
        try:
            deleted = self.delete_skill(name)
            if deleted:
                return NormalizedEvent(
                    event_type=EventType.SKILL_DELETED,
                    payload={"skill_name": name},
                )
            else:
                return NormalizedEvent.global_error(
                    error_code="skill_not_found",
                    error_message=f"Skill 不存在: {name!r}",
                )
        except ValueError as e:
            return NormalizedEvent.global_error(
                error_code="skill_delete_failed",
                error_message=str(e),
            )

    # 导入时仅允许的文件后缀（.md 技能定义 + .py 脚本）
    _ALLOWED_IMPORT_EXTENSIONS: frozenset[str] = frozenset({".md", ".py"})

    async def import_and_build_event(
        self, content: str = "", fmt: str = "folder", overwrite: bool = False,
        source_path: str | None = None,
    ) -> NormalizedEvent:
        """导入 Skill 并构建结果事件。

        仅支持两种格式：
        - zip: 通过 source_path 传入 ZIP 路径，解压后导入
        - folder: 通过 source_path 传入文件夹路径，整体复制导入
        （不支持单个 .md 文件导入，因为 Skill 可能不全）
        """
        try:
            # 数量上限：非覆盖导入会新增用户 Skill，达上限则拒绝
            if not overwrite and len(self._load_user_skills()) >= MAX_USER_SKILLS:
                raise ValueError(
                    f"已达到用户 Skill 数量上限（{MAX_USER_SKILLS} 个），"
                    f"不支持导入更多，请先删除部分 Skill"
                )
            if fmt == "zip":
                if not source_path:
                    raise ValueError("ZIP 导入需要提供 source_path")
                self._import_from_zip(source_path, overwrite)
            elif fmt == "folder":
                if not source_path:
                    raise ValueError("文件夹导入需要提供 source_path")
                self._import_from_folder(source_path, overwrite)
            else:
                raise ValueError(f"不支持的导入格式: {fmt!r}，仅支持 zip / folder")

            return NormalizedEvent(
                event_type=EventType.SKILL_IMPORTED,
                payload={
                    "success": True,
                    "skill_name": getattr(self, "_last_imported_name", ""),
                },
            )

        except (ValueError, FileExistsError) as e:
            return NormalizedEvent(
                event_type=EventType.SKILL_IMPORTED,
                payload={
                    "success": False,
                    "skill_name": "",
                    "error": str(e),
                },
            )
        except Exception as e:
            logger.exception("导入 Skill 失败")
            return NormalizedEvent(
                event_type=EventType.SKILL_IMPORTED,
                payload={
                    "success": False,
                    "skill_name": "",
                    "error": str(e),
                },
            )

    def _import_from_md(self, content: str, overwrite: bool = False, source_path: str | None = None) -> None:
        """从 MD 文本内容导入（解析 YAML Frontmatter）。source_path 非空时从文件读取。"""
        if source_path:
            md_path = Path(source_path)
            if not md_path.is_file():
                raise ValueError(f"MD 文件不存在: {source_path}")
            content = md_path.read_text(encoding="utf-8")

        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not fm_match:
            raise ValueError("文件格式无效：缺少 YAML Frontmatter")

        frontmatter = yaml.safe_load(fm_match.group(1))
        if not isinstance(frontmatter, dict):
            raise ValueError("文件格式无效：Frontmatter 不是有效的 YAML")

        name = frontmatter.get("name", "")
        if not name:
            raise ValueError("文件格式无效：Frontmatter 中缺少 name 字段")

        _validate_skill_name(name)
        self._check_system_skill_conflict(name)

        body = content[fm_match.end():].strip()
        self._install_skill(name, frontmatter, body, overwrite)
        self._last_imported_name = name
        logger.info("导入 Skill 成功 (md): %s", name)

    def _import_from_zip(self, zip_path: str, overwrite: bool = False) -> None:
        """从 ZIP 文件导入（解压后解析 SKILL.md + 关联文件）。"""
        zip_path_obj = Path(zip_path)
        if not zip_path_obj.is_file():
            raise ValueError(f"ZIP 文件不存在: {zip_path}")

        with tempfile.TemporaryDirectory(prefix="skill_import_") as tmp_dir:
            with zipfile.ZipFile(zip_path_obj, "r") as zf:
                # 解压前校验：仅允许 .md 和 .py
                invalid_entries: list[str] = []
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    ext = Path(info.filename).suffix.lower()
                    if ext not in self._ALLOWED_IMPORT_EXTENSIONS:
                        invalid_entries.append(info.filename)
                if invalid_entries:
                    raise ValueError(
                        "ZIP 中包含不支持的文件格式，仅支持 .md / .py：\n" +
                        "\n".join(f"  - {f}" for f in invalid_entries)
                    )
                zf.extractall(tmp_dir)

            tmp_path = Path(tmp_dir)
            # 解压后可能是 {tmp}/skill_name/... 或 {tmp}/... 直接
            entries = list(tmp_path.iterdir())
            if not entries:
                raise ValueError("ZIP 文件为空")

            # 如果只有一个子目录，进入该子目录
            if len(entries) == 1 and entries[0].is_dir():
                skill_root = entries[0]
            else:
                skill_root = tmp_path

            md_path = skill_root / "SKILL.md"
            if not md_path.is_file():
                raise ValueError("ZIP 中未找到 SKILL.md 文件")

            content = md_path.read_text(encoding="utf-8")
            fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
            if not fm_match:
                raise ValueError("ZIP 中的 SKILL.md 缺少 YAML Frontmatter")

            frontmatter = yaml.safe_load(fm_match.group(1))
            if not isinstance(frontmatter, dict):
                raise ValueError("ZIP 中的 SKILL.md Frontmatter 不是有效的 YAML")

            name = frontmatter.get("name", "")
            if not name:
                raise ValueError("ZIP 中的 SKILL.md Frontmatter 缺少 name 字段")

            _validate_skill_name(name)
            self._check_system_skill_conflict(name)

            body = content[fm_match.end():].strip()
            # 先写入 SKILL.md，再复制关联文件
            self._write_skill_file(
                name,
                str(frontmatter.get("description", "")),
                str(frontmatter.get("when_to_use", "")),
                body,
                self._parse_tags(frontmatter),
            )

            # 复制关联文件（__init__.py、skill_script.py 等非 SKILL.md 文件）
            user_skill_dir = self._user_dir / name
            for item in skill_root.iterdir():
                if item.name == "SKILL.md":
                    continue
                dest = user_skill_dir / item.name
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)

            self._last_imported_name = name
            logger.info("导入 Skill 成功 (zip): %s", name)

    def _import_from_folder(self, folder_path: str, overwrite: bool = False) -> None:
        """从文件夹导入（复制全部文件 + 解析 SKILL.md）。"""
        folder_path_obj = Path(folder_path)
        if not folder_path_obj.is_dir():
            raise ValueError(f"文件夹不存在: {folder_path}")

        # 校验：仅允许 .md 和 .py 文件
        invalid_files: list[str] = []
        for item in folder_path_obj.rglob("*"):
            if item.is_file() and item.suffix.lower() not in self._ALLOWED_IMPORT_EXTENSIONS:
                invalid_files.append(str(item.relative_to(folder_path_obj)))
        if invalid_files:
            raise ValueError(
                "文件夹中包含不支持的文件格式，仅支持 .md / .py：\n" +
                "\n".join(f"  - {f}" for f in invalid_files)
            )

        md_path = folder_path_obj / "SKILL.md"
        if not md_path.is_file():
            raise ValueError("文件夹中未找到 SKILL.md 文件")

        content = md_path.read_text(encoding="utf-8")
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not fm_match:
            raise ValueError("文件夹中的 SKILL.md 缺少 YAML Frontmatter")

        frontmatter = yaml.safe_load(fm_match.group(1))
        if not isinstance(frontmatter, dict):
            raise ValueError("文件夹中的 SKILL.md Frontmatter 不是有效的 YAML")

        name = frontmatter.get("name", "")
        if not name:
            raise ValueError("文件夹中的 SKILL.md Frontmatter 缺少 name 字段")

        _validate_skill_name(name)
        self._check_system_skill_conflict(name)

        user_skill_dir = self._user_dir / name
        if user_skill_dir.exists():
            if not overwrite:
                raise FileExistsError(f"用户技能 \"{name}\" 已存在")
            shutil.rmtree(user_skill_dir)

        # 复制整个文件夹
        shutil.copytree(folder_path_obj, user_skill_dir)

        self._last_imported_name = name
        logger.info("导入 Skill 成功 (folder): %s → %s", folder_path, user_skill_dir)

    def _check_system_skill_conflict(self, name: str) -> None:
        """检查是否与系统 Skill 冲突。"""
        sys_path = self._system_dir / name
        if sys_path.is_dir():
            raise ValueError(f"无法覆盖系统技能 \"{name}\"")

    def _install_skill(
        self, name: str, frontmatter: dict, body: str, overwrite: bool = False
    ) -> None:
        """安装 Skill（仅 MD 文本，写 SKILL.md 文件）。"""
        user_skill_dir = self._user_dir / name
        if user_skill_dir.exists():
            if not overwrite:
                raise FileExistsError(f"用户技能 \"{name}\" 已存在")

        description = str(frontmatter.get("description", ""))
        when_to_use = str(frontmatter.get("when_to_use", ""))
        self._write_skill_file(name, description, when_to_use, body, self._parse_tags(frontmatter))

    @staticmethod
    def _parse_tags(frontmatter: dict) -> list[str]:
        """从 frontmatter 解析 tags。"""
        tags_raw = frontmatter.get("tags", "")
        if isinstance(tags_raw, str):
            return [t.strip() for t in tags_raw.split(",") if t.strip()]
        if isinstance(tags_raw, list):
            return [str(t).strip() for t in tags_raw if str(t).strip()]
        return []

    async def export_and_build_event(
        self, skill_name: str, fmt: str,
        target_path: str | None = None,
    ) -> NormalizedEvent:
        """导出 Skill 并构建结果事件。target_path 非空时直接写入该路径。

        仅支持两种格式：
        - zip: 打包 SKILL.md + 关联文件为 ZIP
        - folder: 复制整个 Skill 文件夹到目标目录
        """
        try:
            _validate_skill_name(skill_name)

            skill = self.get_skill(skill_name)
            if skill is None:
                raise ValueError(f"Skill 不存在: {skill_name!r}")

            skill_dir = self._user_dir if skill.source == "user" else self._system_dir
            skill_path = skill_dir / skill_name

            if fmt == "zip":
                if target_path:
                    base_name = str(Path(target_path).with_suffix(""))
                    shutil.make_archive(
                        base_name, "zip",
                        root_dir=skill_path.parent, base_dir=skill_name,
                    )
                    file_path = target_path
                else:
                    fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix=f"{skill_name}_")
                    os.close(fd)
                    tmp_path_obj = Path(tmp_path)
                    shutil.make_archive(
                        str(tmp_path_obj.with_suffix("")), "zip",
                        root_dir=skill_path.parent, base_dir=skill_name,
                    )
                    file_path = str(tmp_path_obj)
            elif fmt == "folder":
                if target_path:
                    dest = Path(target_path)
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(skill_path, dest)
                    file_path = target_path
                else:
                    fd, tmp_path = tempfile.mkstemp(prefix=f"{skill_name}_")
                    os.close(fd)
                    tmp_dir = Path(tmp_path)
                    tmp_dir.unlink(missing_ok=True)
                    shutil.copytree(skill_path, tmp_dir)
                    file_path = str(tmp_dir)
            else:
                raise ValueError(f"不支持的导出格式: {fmt!r}，仅支持 zip / folder")

            logger.info("导出 Skill 成功: %s → %s", skill_name, file_path)
            return NormalizedEvent(
                event_type=EventType.SKILL_EXPORTED,
                payload={
                    "file_path": file_path,
                    "format": fmt,
                },
            )

        except Exception as e:
            logger.exception("导出 Skill 失败: %s", skill_name)
            return NormalizedEvent.global_error(
                error_code="skill_export_failed",
                error_message=str(e),
            )
