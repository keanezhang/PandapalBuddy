"""pandapal/local/run_local.py — PandaPal 后端启动入口。

唯一入口：装配子系统 → PandaPalApp.start() → 等待 shutdown。

启动流程：
1. 加载 .env
2. 构造 Config + Storage + Session + Gateway + Agent
3. 构造 PandaPalApp(config, agent, session_manager, storage_manager, wss_transport)
4. await app.start()  ← 内部完成：Channels/Broadcast/Router/HITL/Scheduler/IPC
5. 等待 shutdown_event（stdin EOF / SIGINT / SIGTERM）

Usage:
    python -m pandapal.local --user-id alice --token eyJ...
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform as _platform
import sys
from pathlib import Path

from pandapal.local.boot_logger import BootLogger

logger = logging.getLogger(__name__)

# 本地 Agent 的 identity id。同时作为观测侧 agent_id label 的取值（Identity 与
# 降级通道两处必须同源，否则看板按 agent_id 归并时会分裂成两个 agent）。
AGENT_ID = "pandapal"


# ══════════════════════════════════════════════════════════════════════════════
# §0  启动工具函数（与原版一致）
# ══════════════════════════════════════════════════════════════════════════════


def _is_frozen() -> bool:
    """判断当前是否在 PyInstaller 打包后的二进制中运行。"""
    return getattr(sys, "frozen", False)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    import warnings
    warnings.showwarning = lambda msg, *a, **k: print(msg, file=sys.stdout)


def _start_parent_watcher(shutdown_event: asyncio.Event) -> None:
    """监控父进程存活状态（防御性保护）。"""
    import threading
    import platform

    if platform.system() == "Windows":
        _watcher_fn = _windows_parent_watcher(shutdown_event)
    else:
        _watcher_fn = _unix_parent_watcher(shutdown_event)

    t = threading.Thread(target=_watcher_fn, name="parent-watcher", daemon=True)
    t.start()


def _unix_parent_watcher(shutdown_event: asyncio.Event):
    """Unix: 检测 ppid 变化。"""
    original_ppid = os.getppid()

    def _watcher():
        import time
        while not shutdown_event.is_set():
            current_ppid = os.getppid()
            if current_ppid != original_ppid:
                logging.getLogger("pandapal").warning(
                    f"[parent-watcher] ppid changed {original_ppid} → {current_ppid}, shutting down"
                )
                shutdown_event.set()
                return
            time.sleep(2)

    return _watcher


def _windows_parent_watcher(shutdown_event: asyncio.Event):
    """Windows: 通过 OpenProcess + WaitForSingleObject 检测父进程是否仍然存活。"""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x00000000

    parent_pid = os.getppid()

    def _watcher():
        import time
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
        if not handle:
            logging.getLogger("pandapal").warning(
                f"[parent-watcher] cannot open parent process {parent_pid}, shutting down"
            )
            shutdown_event.set()
            return

        try:
            while not shutdown_event.is_set():
                result = kernel32.WaitForSingleObject(handle, 0)
                if result == WAIT_OBJECT_0:
                    logging.getLogger("pandapal").warning(
                        f"[parent-watcher] parent process {parent_pid} exited, shutting down"
                    )
                    shutdown_event.set()
                    return
                time.sleep(2)
        finally:
            kernel32.CloseHandle(handle)

    return _watcher


def _resolve_config_dir() -> Path:
    """系统配置目录：frozen → sys._MEIPASS；开发 → 项目根。"""
    if _is_frozen() and hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════════════════════
# §1  Agent 静态配置（与原版一致：环境块 + 系统提示词 + 规划方法）
# ══════════════════════════════════════════════════════════════════════════════


def _build_quality_gate(work_dir: Path):
    """构造编码质量门控。

    project_root = 用户选定的工作区 —— ruff 子进程靠它找到该项目的 pyproject.toml，
    从而与**用户自己项目的 CI** 用同一把尺子。门控不另立 select/ignore：规则的唯一
    真相源是那份 pyproject.toml。

    ★ 目录分层改造后：work_dir 由前端 --workdir 传入，不再调用 resolve_project_root 探测。
    """
    from pandapal.quality import CodeQualityGate, GateConfig

    return CodeQualityGate(GateConfig(project_root=str(work_dir)))


def _build_environment_block(work_dir: Path) -> str:
    """构建环境信息块，注入到系统提示中。

    ★ 目录分层改造后：work_dir 由前端 --workdir 传入，不再调用 resolve_project_root 探测。
    """
    project_root = str(work_dir)

    os_name = _platform.system()
    home_dir = str(Path.home())

    if os_name == "Windows":
        shell_hint = "PowerShell / cmd"
        path_hint = (
            "  - Windows 下不要使用 /home/user/ 等 Linux 风格路径\n"
            "  - bash 工具执行的是 PowerShell/cmd 命令，不要用 find/pwd/ls/cat/grep，"
            "用 dir/type/get-content 或专用工具代替"
        )
        separator = "\\\\"
    else:
        shell_hint = "bash / zsh"
        path_hint = "  - 使用标准 POSIX 路径"
        separator = "/"

    return f"""---
## 运行环境（不可变事实，必须遵守）

- **操作系统**：{os_name}
- **工作区根目录**：{project_root}
- **用户主目录**：{home_dir}
- **Shell**：{shell_hint}
- **路径分隔符**：{separator}
- **文件路径规则**：
  - 写入文件时，路径必须在工作区根目录或用户主目录内
  - 使用相对路径（如 `output/方案.md`）时基于工作区根目录解析
{path_hint}
"""







def _load_pandapal_md(work_dir: Path) -> str:
    """加载 PANDAPAL.md 项目指引文件（仅 coding 模式注入），失败时降级返回空字符串。

    ★ 目录分层改造后：PANDAPAL.md 从用户工作区（--workdir）加载。
    """
    pandapal_path = work_dir / "PANDAPAL.md"
    if pandapal_path.exists():
        logger.info("PANDAPAL.md loaded from %s (%d chars)", pandapal_path, pandapal_path.stat().st_size)
        return "\n\n" + pandapal_path.read_text(encoding="utf-8")
    logger.warning(
        "PANDAPAL.md not found at %s — Agent will run without project guide",
        pandapal_path,
    )
    return ""


# 运行环境块（模式无关，所有模式共用）：内嵌工作区根目录，由 main() 首次调用
# _get_environment_block(WORK_DIR) 时计算并缓存，后续调用（含 _build_blueprint 内）
# 直接命中缓存。PANDAPAL.md 项目指引不在此缓存内——它仅 coding 模式注入（见
# _get_prompt_suffix / build_prompt_map 的 coding_extra 参数）。
_ENV_BLOCK_CACHE: str | None = None


def _get_environment_block(work_dir: Path | None = None) -> str:
    """返回运行环境块（模式无关的 prompt 尾部，首次调用时计算并缓存）。

    main() 首次调用时必须传入 WORK_DIR，后续调用（含 _build_blueprint 内）缓存命中无需传参。
    """
    global _ENV_BLOCK_CACHE
    if _ENV_BLOCK_CACHE is None:
        assert work_dir is not None, "_get_environment_block: 首次调用必须传入 work_dir"
        _ENV_BLOCK_CACHE = _build_environment_block(work_dir)
    return _ENV_BLOCK_CACHE


def _get_prompt_suffix(work_dir: Path, mode: str) -> str:
    """按模式组装 prompt 尾部：运行环境块所有模式共用；PANDAPAL.md 项目指引仅 coding 注入。

    供 prompts.compose 拼接；office 模式只拿环境块，不带项目指引。
    """
    suffix = _get_environment_block(work_dir)
    if mode == "coding":
        suffix += _load_pandapal_md(work_dir)
    return suffix

# ══════════════════════════════════════════════════════════════════════════════
# §2  子系统构建工厂（5.2 重写：每个工厂聚焦于"只做一件事"）
# ══════════════════════════════════════════════════════════════════════════════


def _build_observability(obs_dir: Path, mode: str) -> dict:
    """按模式装配四大观测支柱后端，返回 {audit, tracer, metrics, log}。

    - "markdown"：四个 Markdown 后端，落人类可读 .md（obs_dir 是**目录**）。
    - "sqlite"  ：四个 SQLite 后端共享**一个** observability.db 连接（obs_dir 内的
                  单库多表）。共享连接的意义：① audit/spans/metrics/logs 同库可跨表
                  join（看板因果回放）；② 单连接串行化写入，规避多连接并发写同一
                  WAL 文件的 SQLITE_BUSY。连接的关闭挂到 atexit（与 Markdown 指标
                  后端的 atexit 刷盘同一范式；sidecar 长驻，WAL 保证每事务已持久）。

    obs_dir 已是 user-scoped（{data}/pandapal_md/users/{uid}），故观测数据与会话/
    对话日志同目录内聚。
    """
    if mode == "sqlite":
        import atexit
        import sqlite3

        from pandaren.observability.backend.sqlite import (
            SQLiteAuditBackend,
            SQLiteLoggerBackend,
            SQLiteMetricsBackend,
            SQLiteTracerBackend,
        )

        obs_dir.mkdir(parents=True, exist_ok=True)
        db_path = obs_dir / "observability.db"
        conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level="DEFERRED",
        )
        conn.execute("PRAGMA journal_mode=WAL")
        # 多线程 hooks 并发写共享连接：给一点排队窗口，进一步压低偶发 BUSY。
        conn.execute("PRAGMA busy_timeout=5000")
        atexit.register(conn.close)

        logger.info("Observability: SQLite 模式 (db=%s)", db_path)
        return {
            "audit": SQLiteAuditBackend(connection=conn),
            "tracer": SQLiteTracerBackend(connection=conn),
            "metrics": SQLiteMetricsBackend(connection=conn),
            "log": SQLiteLoggerBackend(connection=conn),
        }

    # 默认 markdown
    from pandaren.observability.backend.markdown import (
        MarkdownAuditBackend,
        MarkdownLoggerBackend,
        MarkdownMetricsBackend,
        MarkdownTracerBackend,
    )

    logger.info("Observability: Markdown 模式 (dir=%s)", obs_dir)
    return {
        "audit": MarkdownAuditBackend(obs_dir),
        "tracer": MarkdownTracerBackend(obs_dir),
        "metrics": MarkdownMetricsBackend(obs_dir),
        "log": MarkdownLoggerBackend(obs_dir),
    }


def _build_storage_manager(base_data_dir: Path, user_id: str = "", storage_mode: str = "markdown"):
    """构造并初始化 StorageManager。

    ★ 目录分层改造后：base_data_dir 已是 USER_DATA_DIR/data/，路径已含 user-scope，
      不再通过 StorageManager 二次内嵌 users/{uid}（传 user_id="" 避免双 users/ 层）。

    storage_path 随 storage_mode 二分（StorageManager 不做隐式转换）：
      - "sqlite" ：文件 {base_data_dir}/pandapal.db
      - "markdown"：目录 {base_data_dir}/pandapal_md
    """
    from pandapal.storage.manager import StorageManager
    # storage_mode 已在 ConfigManager 校验为 markdown/sqlite；此处非法值兜底回 markdown。
    mode = "sqlite" if storage_mode == "sqlite" else "markdown"
    if mode == "sqlite":
        storage_path = str(base_data_dir / "pandapal.db")
    else:
        storage_path = str(base_data_dir / "pandapal_md")
    sm = StorageManager(
        storage_path=storage_path,
        storage_mode=mode,
        user_id=user_id,
    )
    return sm


def _build_session_manager(storage_manager, config_manager):
    """构造 SessionManager。"""
    from pandapal.session.manager import SessionManager
    session_repo = storage_manager.get_session_repo()
    return SessionManager(
        session_repo=session_repo,
        config_manager=config_manager,
    )


def _build_token_estimator():
    """构造 Token 估算器（压缩触发判据的"尺子"）。

    根本解决（docs/analysis/压缩管线排查报告.md 方案B）：SDK 默认
    CharBasedTokenEstimator（chars/4.0）对中文/代码系统性低估 ~2x，
    导致 compact_if_needed 触发过晚、实际 LLM 输入超出 conversation 预算。
    此处注入真实 BPE tokenizer（cl100k_base），估算与实际 token 同量纲。

    词表离线 vendored 在 pandapal/resources/tokenizer/（由
    scripts/fetch_tiktoken_vocab.py 生成，随 sidecar 打包），
    不走 tiktoken 默认的 Azure blob 首启下载路径。

    失败不炸断启动：经 degradation 统一通道留痕（tokenizer_fallback）后
    返回 None → Memory 回落 CharBasedTokenEstimator（= 改造前行为）。
    """
    try:
        from pandaren.memory.estimators import TiktokenEstimator

        if _is_frozen():
            vocab_path = (
                Path(sys._MEIPASS) / "pandapal" / "resources" / "tokenizer"  # type: ignore[attr-defined]
                / "cl100k_base.tiktoken"
            )
        else:
            vocab_path = (
                Path(__file__).resolve().parent.parent
                / "resources" / "tokenizer" / "cl100k_base.tiktoken"
            )
        return TiktokenEstimator(vocab_path=vocab_path)
    except Exception:
        from pandapal import degradation as _degradation

        _degradation.report_degradation(
            _degradation.DegradationEvent.TOKENIZER_FALLBACK,
            category="capability",
            source="run_local._build_token_estimator",
            expected="TiktokenEstimator(cl100k_base)",
            fallback="CharBasedTokenEstimator(chars/4.0)",
            exc_info=True,
        )
        return None


def _build_blueprint(
    raw_credentials: list[dict],
    default_model_id: str,
    default_provider: str,
    user_id: str,
    user_data_dir: Path,
    user_resources_dir: Path,
    work_dir: Path,
    session_manager=None,
    storage_manager=None,
    storage_mode: str = "markdown",
):
    """构造 pandaren AgentBlueprint。

    ★ 目录分层改造后：user_data_dir = USER_DATA_DIR（AppData/users/{uid}），
      所有用户个人数据从此派生；work_dir = WORK_DIR（用户工作区）。
    """
    from pandaren.builder import AgentBuilder
    from pandaren.identity.models import PERMISSION_ALL, TrustLevel
    from pandaren.llm.client import OpenAICompatibleClient
    from pandaren.llm.router import LLMRouter
    from pandapal.config.budget.pricing import install_price_book
    from pandapal.config.llm.model_registry import (
        build_price_book,
        resolve_available_models,
    )
    from pandapal.config.llm.provider_catalog import resolve_base_url

    # 可用模型清单 = 用户已配置凭据派生（规则 2：系统不内置模型，用户填什么就能用什么）。
    # raw_credentials 含真实 api_key（load_all_raw），resolve 只用 model_id/provider/is_default。
    available = resolve_available_models(raw_credentials)

    # 一致性断言：available 首项（is_default 凭据派生）必须等于传入的 default_model_id。
    # 三处（default_client / available 首项 / 外部透传）同源，断言失败即装配 bug。
    # ⚠️ 不用 assert：`python -O` 会把 assert 整条剥除，而这里守的正是
    #    「装配了 A 却路由到 B」——模型错了、账也记到错误 provider 上（§九 ID 类
    #    字段零容忍）。这类不变量必须用真实分支 + 显式异常。
    if available and available[0].model_id != default_model_id:
        raise RuntimeError(
            f"default 不一致：available[0]={available[0].model_id!r} "
            f"vs default_model_id={default_model_id!r}"
        )

    # 默认 client：从 raw_credentials 显式取 is_default 凭据（真实 key）。
    # 无凭据时用空值占位（与历史行为一致，sidecar 照常启动，LLM 调用由前端 AuthGuard 拦截）。
    default_cred = next(
        (c for c in raw_credentials if c.get("is_default")),
        {"provider": "", "api_key": "", "model_id": "", "base_url": None},
    )
    # base_url 走 resolve_base_url（规则 7：用户不填时用 catalog 的 default_base_url），
    # 不再直接透传 cred["base_url"] 给 SDK（避免 SDK 默认值与 catalog 不一致）。
    default_base_url = (
        resolve_base_url(default_cred["provider"], default_cred.get("base_url"))
        if default_cred["provider"]
        else None
    )
    default_client = OpenAICompatibleClient.for_provider(
        provider=default_cred["provider"],
        api_key=default_cred["api_key"],
        model_name=default_cred["model_id"],
        base_url=default_base_url,
    )

    # LLMRouter：为每个可用 model 各建一个 client，按 model_id 精确路由
    # （run_core 传 ModelSettings(target_model=model_id) 时生效）。非法/未指定 model
    # 回落 default_client。Router 满足 LLMClient Protocol，engine 层对单 client/路由两态透明。
    llm_client = LLMRouter()
    llm_client.register(default_model_id, default_client)
    built_model_ids: set[str] = {default_model_id}
    # effective_models：实际注册成功的可选清单（含 default），下发前端 MODEL_LIST 用；
    # 与 Router 能路由的严格一致（构建失败的 model 不会出现在清单里）。
    effective_models: list = [available[0]] if available else []
    for m in available[1:]:  # 跳过 default（已在首位注册）
        cred = next(c for c in raw_credentials if c["model_id"] == m.model_id)
        try:
            # base_url 走 resolve_base_url（与 default_client 同源，规则 7）。
            m_base_url = resolve_base_url(m.provider, cred.get("base_url"))
            llm_client.register(
                m.model_id,
                OpenAICompatibleClient.for_provider(
                    provider=m.provider,
                    api_key=cred["api_key"],
                    model_name=m.model_id,
                    base_url=m_base_url,
                ),
            )
            built_model_ids.add(m.model_id)
            effective_models.append(m)
        except Exception:
            logger.exception("构建模型 %s 的 client 失败，跳过注册", m.model_id)
    llm_client.set_default(default_client)
    logger.info(
        "LLMRouter 装配完成：可选模型=%s，默认=%s",
        sorted(built_model_ids), default_model_id,
    )

    # 价格账本：从 effective_models（**实际注册成功**的清单）派生，而非 available。
    # 这保证「能路由的」与「能计费的」严格一致——若从 available 派生，构建 client
    # 失败的模型会留在账本里，形成「能计费却路由不到」的幽灵条目。
    price_book = build_price_book(effective_models)
    install_price_book(price_book)
    unpriced = [m.model_id for m in effective_models if m.needs_price]
    if unpriced:
        # 正常路径下不该出现——保存期已拦下无单价来源的模型。会走到这里只可能是
        # 系统默认表升级后移除了这些模型（PRD·R10），或有绕过保存校验的写入路径。
        # 不阻断使用，但必须留痕：其消费会落入未定价兜底桶并触发 P0 告警。
        logger.warning(
            "[pricing] %d 个模型处于「待补价」：%s；"
            "其消费不计入预算，请在设置中补填单价",
            len(unpriced), unpriced,
        )

    agent_builder = AgentBuilder()
    agent_builder.identity(
        agent_id=AGENT_ID,
        agent_name="PandaPal",
        when_to_use="处理用户消息",
        sensitive_permissions=PERMISSION_ALL,
        trust_level=TrustLevel.ORCHESTRATOR,
    )
    agent_builder.llm(client=llm_client)
    # 流式模式下必须显式开启 include_usage，否则遵守 OpenAI spec 的 provider
    # （如 DashScope/Qwen）不会在流末尾回填 usage，导致 token/缓存命中率全为 0。
    # DeepSeek 默认回填 usage 所以之前没配也正常，换 Qwen 后缺陷才暴露。
    agent_builder.llm_settings(include_usage=True)
    from pandapal.local import prompts
    # 必须传入 work_dir：首次调用负责预热缓存（缓存为 None 时未传会触发 assert）。
    # _build_blueprint 在 main() 的缓存预热调用之前执行，故此处显式传入。
    agent_builder.system_prompt(
        prompts.compose(prompts.DEFAULT_MODE, _get_prompt_suffix(work_dir, prompts.DEFAULT_MODE))
    )
    # 费用停机由应用层全权负责（SDK 只提供通用 StepGuard 机制）：注入 CostBudgetGuard，
    # 它按实际净费用（含缓存折扣，价格数据在 pandapal.config.llm_pricing）累加并判断是否超预算，
    # 同时其 spent(run_id) 供会话末尾（REPLY_END）展示本 run 花费。
    # max_usd=None → 不设预算、永不因花费停机（当前默认，与历史行为一致），但仍累加供展示；
    # 需要花费上限时改为 CostBudgetGuard(max_usd=5.0) 即可，无需改动 SDK。
    from pandapal.config.budget.repo import JsonFileBudgetRepo
    from pandapal.config.budget.ledger import BudgetLedger
    from pandapal.config.budget.guard import CostBudgetGuard
    # 预算账本（按 provider 分账）：JSON 文件持久化（mode-agnostic，跨会话/重启累计），
    # 注入守卫。守卫每步把净费用委托账本累加并取超额裁决；账本 spent_usd 是唯一已花费量，
    # 与「停机判据 / 额度条 / Dashboard 该 provider 聚合」同源（PRD §3.4.8）。
    # 用户为每个 provider 分别设额度（SET_BUDGET IPC → ledger.set_budget）；某家耗尽只停该家。
    budget_ledger = BudgetLedger(JsonFileBudgetRepo(str(user_data_dir / "data" / "budgets.json")))
    cost_guard = CostBudgetGuard(max_usd=None, ledger=budget_ledger)

    # 编码质量门控：Agent 写完 .py 立刻跑 ruff，诊断随同一条 tool 消息回灌到下一轮，
    # 把「改完代码要检查」从 prompt 软倡议变成框架强制。规则唯一真相源是**用户工作区的**
    # pyproject.toml —— 与他们自己的 CI 同一把尺子，门控不另立标准。
    # project_root 就是用户选定的工作区：ruff 靠它找到配置，
    # 设错会让 ruff **静默回落默认档**（不报错），是本机制最隐蔽的失效模式。
    quality_gate = _build_quality_gate(work_dir)

    agent_builder.behavior(
        max_steps=200,
        step_timeout=600.0,
        total_timeout=1200.0,
        step_guard=cost_guard,
        # 控制面：贡献反馈（会影响 Agent 行为——LLM 读得到）
        tool_feedback_providers=[quality_gate],
        # HIGH 敏感度工具（edit_file / write_file / delete_file 等）自动放行，
        # 不再弹 HITL 人工审批。注意：仍是全局开关，被自动升级到 HIGH 的工具一并放行；
        # audit_required=True 的工具仍会写审计日志（可追溯）。CRITICAL 级受 HC6 保护，
        # 无视此开关，永远强制审批。
        auto_confirm_high=True,
        stream=True,
    )
    agent_builder.context_budget(
        context_window=100000,
        system_prompt_ratio=0.15,
        tool_schema_ratio=0.10,
        conversation_ratio=0.65,
    )
    agent_builder.plan_mode(
        plan_dir=str(user_data_dir / "plans"),
    )

    # Token 估算器（真实 BPE tokenizer）——压缩触发判据与实际 LLM token
    # 同量纲的前提（排查报告问题 1 的根本解）。与持久化解耦：即使 memory
    # backends 不可用，估算器也必须注入，否则判据退回 chars/4.0 旧量纲。
    token_estimator = _build_token_estimator()
    memory_kwargs: dict = (
        {"token_estimator": token_estimator} if token_estimator is not None else {}
    )

    # 注入 memory backends
    # ★ 多 Session 并发：raw_log_backend / working_memory_backend 是共享后端，
    #   多个 Memory 实例并发写同一 backend 时后端内部按 (user_id, session_id) 分片。
    if user_id and storage_manager is not None:
        from pandapal.local.llm_policies import LLMDropSummarizer

        drop_summarizer = LLMDropSummarizer(llm_client=default_client)
        try:
            raw_log_backend = storage_manager.get_raw_log_backend(user_id)
            working_memory_backend = storage_manager.get_working_memory_backend(user_id)
            if raw_log_backend and working_memory_backend:
                memory_kwargs.update(
                    raw_log_backend=raw_log_backend,
                    working_memory_backend=working_memory_backend,
                    drop_summarizer=drop_summarizer,
                )
            else:
                logger.warning(
                    "Memory backends unavailable for user_id=%s "
                    "(raw_log=%s, working_memory=%s) — LLM will run without memory",
                    user_id, raw_log_backend, working_memory_backend,
                )
        except Exception:
            logger.exception(
                "Memory backend injection failed for user_id=%s — "
                "LLM will run without memory", user_id,
            )

    # .memory() 无条件调用：仅含 token_estimator 时等价于原"不调用"行为
    # （其余字段全为默认值），只是估算器始终生效。
    agent_builder.memory(**memory_kwargs)

    # 工具
    from pandapal.tools import get_all_tools
    app_tools = get_all_tools()
    agent_builder.tools(app_tools)

    # Skills — system/ 只读 (PROJECT) + user/ 可CRUD (USER)，同名时 USER 优先
    from pandaren.skill.models import SkillSource

    if _is_frozen():
        resources_dir = Path(sys._MEIPASS) / "pandapal" / "resources"  # type: ignore[attr-defined]
    else:
        resources_dir = Path(__file__).resolve().parent.parent / "resources"

    # system/ 随 sidecar 打包，只读
    agent_builder.skills_from_dir(
        resources_dir / "skills" / "system",
        source=SkillSource.PROJECT,
    )
    # user/ 从 ~/.pandapal 加载（用户自建，不受 sidecar 升级影响）
    agent_builder.skills_from_dir(
        user_resources_dir / "skills",
        source=SkillSource.USER,
    )

    # 专家子 Agent —— 主 Agent 通过 call_agent 委派。与 Skills 对称的双层加载：
    #   system/ 随 sidecar 打包（只读，如 test-designer / test-coder）；
    #   user/   从持久化数据目录加载（用户自建，不受 rebuild/upgrade 影响）。
    #   蓝图 tools 字段从 app_tools 池按名过滤；空 tools → 只继承 SDK 内置文件工具。
    #   agent_name 即 call_agent 的调用键。目录不存在时 loader 静默返回空列表。
    agent_builder.sub_agents_from_dir(
        resources_dir / "agents" / "system",
        default_client,
        tools=app_tools,
    )
    agent_builder.sub_agents_from_dir(
        user_resources_dir / "agents",
        default_client,
        tools=app_tools,
    )

    # 可观测性
    # ★ 目录分层改造后：obs 数据与 storage 同目录（user_data_dir/data/），
    #   不再通过 _make_user_scoped_path 推导（路径已含 user-scope）。
    if storage_mode == "sqlite":
        obs_dir = user_data_dir / "data"
    else:
        obs_dir = user_data_dir / "data" / "pandapal_md"
    obs_backends = _build_observability(obs_dir, storage_mode)
    agent_builder.observability(
        audit=obs_backends["audit"],
        tracer=obs_backends["tracer"],
        metrics=obs_backends["metrics"],
        log=obs_backends["log"],
    )
    # 统一降级通道复用同一个 Metrics 后端做趋势 counter（§5，不造第五柱）。
    # 走 Metrics facade 而非裸后端：白拿 agent_id label（与其余 counter 同构，否则按
    # agent_id join 会整体漏掉）+ 复用 facade 的观测 Fail-Safe 边界。
    from pandaren.observability.metrics import Metrics

    from pandapal import degradation as _degradation
    _degradation.set_metrics(
        Metrics(backend=obs_backends["metrics"], agent_id=AGENT_ID),
    )

    # Skill 生命周期 Hooks — 构造时无 broadcast，后续由 app 延迟绑定
    from pandaren.hook import CompositeAgentHooks

    from pandapal.hooks.skill_hooks import SkillAwareHooks

    # .hooks() 是"最后一次调用生效"，故必须组合而非覆盖，否则 SkillAwareHooks 会被顶掉。
    _hooks = CompositeAgentHooks()
    _hooks.add(SkillAwareHooks())
    # 门控在此**第二次**露面：注册的是它的回收适配器（run 结束时清掉该 session 的熔断
    # 计数，纯自扫门前雪、不影响行为）。门控本体走上面的 tool_feedback_providers。
    _hooks.add(quality_gate.reclaim_hooks())
    agent_builder.hooks(_hooks)

    return agent_builder.build_blueprint(), effective_models


def _build_gateway(sys_config, args):
    """构造 Gateway（远程渠道用，可选）。"""
    from pandapal.gateway.gateway import Gateway
    from pandapal.gateway.models import GatewayConfig
    try:
        return Gateway(
            relay_url=sys_config.relay_url,
            jwt_token=args.token,
            config=GatewayConfig(),
        )
    except Exception as e:
        logger.warning(f"cannot construct gateway — running offline ({e})")
        return None


def _build_wss_transport(gateway):
    """把 Gateway 包装成 WSSGateway Transport（承载所有远程渠道：wecom / xiaozhi）。"""
    if gateway is None:
        return None
    from pandapal.gateway.wss_transport import WSSGateway
    return WSSGateway(gateway=gateway, default_channel_id="wecom")


# ══════════════════════════════════════════════════════════════════════════════
# §3  主启动流程（5.2 重写版：PandaPalApp 接管）
# ══════════════════════════════════════════════════════════════════════════════


async def run_local() -> None:
    """5.2 重写版启动入口。"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", default="")
    parser.add_argument("--token", default="")
    # 工作区根目录：由客户端在用户「打开文件夹」后传入，是 Agent 文件工具的唯一根。
    # 一进程一目录：本进程只服务这一个工作区；切换工作区由客户端重启本进程实现。
    parser.add_argument("--workdir", default="")
    # 应用数据根目录：由 Tauri app_data_dir() 传入，指向系统 AppData 下
    # com.pandapal.desktop/，用户个人数据（DB/凭据/skills/plans）持久化在此。
    parser.add_argument("--app-data-dir", required=True,
                        help="系统应用数据目录（Tauri app_data_dir() 传入）")
    args = parser.parse_args()
    user_id: str = args.user_id

    _setup_logging()

    boot = BootLogger(use_color=True)
    boot.banner()

    # ── Step 0 · 工作区根目录（唯一来源：客户端 --workdir，不做任何探测）──
    boot.step(0, "工作区根目录")
    from pandaren.utils import set_search_root
    if not args.workdir:
        boot.error(
            "未指定工作区目录（--workdir）。\n"
            "请先在客户端「打开一个文件夹」后再启动 Agent。"
        )
        sys.exit(2)
    try:
        set_search_root(args.workdir)
    except NotADirectoryError as e:
        boot.error(str(e))
        sys.exit(2)
    boot.kv("workspace", args.workdir)
    boot.ok("workspace root set")
    boot.separator()

    # ── Step 1 · 基础配置加载 ─────────────────────────────
    boot.step("1a", "CONFIG 目录定位")
    config_dir = _resolve_config_dir()
    boot.kv("config_dir", str(config_dir))

    boot.step("1b", "SYSTEM 配置加载（环境文件）")
    from pandapal.config.system.manager import ConfigManager
    from pandapal.config.system.exceptions import ConfigFileError

    config_manager = ConfigManager(str(config_dir))
    try:
        await config_manager.load_config()
    except ConfigFileError as e:
        boot.error(str(e))
        sys.exit(1)

    sys_config = config_manager.get_system_config()
    boot.ok("env config loaded")

    boot.step("1c", "路径解析 & 目录初始化")
    # ★ 目录分层改造：用户个人数据迁至系统 AppData，路径由 CLI 参数一次解析、后续模块禁止各自推导。
    APP_DATA_DIR = Path(args.app_data_dir)
    USER_DATA_DIR = APP_DATA_DIR / "users" / user_id
    WORK_DIR = Path(args.workdir)
    USER_RESOURCES_DIR = WORK_DIR / ".pandapal"

    # 自动创建 AppData 下所有子目录（首次启动 / 新用户）
    _dirs_to_create = [
        APP_DATA_DIR,
        USER_DATA_DIR / "data",
        USER_DATA_DIR / "credentials",
        USER_DATA_DIR / "plans",
        USER_DATA_DIR / "sessions",
        USER_RESOURCES_DIR / "skills",
        USER_RESOURCES_DIR / "agents",
    ]
    for _d in _dirs_to_create:
        _d.mkdir(parents=True, exist_ok=True)

    boot.kv("APP_DATA_DIR", str(APP_DATA_DIR))
    boot.kv("USER_DATA_DIR", str(USER_DATA_DIR))
    boot.kv("WORK_DIR", str(WORK_DIR))

    boot.ok("path resolution done")

    # ── 1d · 加载用户 LLM 凭据（BYOK 唯一真相源：llm_credentials.toml）──
    # 显式加载 raw_credentials（真实 key），供 Step 1e / Step 5 直接消费，
    # 不再经过 os.environ 中转 —— default / Router / 可选清单三处同源且显式一致。
    boot.step("1d", "加载用户 LLM 凭据 (BYOK)")
    _cred_dir = USER_DATA_DIR / "credentials"
    _cred_dir.mkdir(parents=True, exist_ok=True)
    from pandapal.config.llm.credentials_store import (
        CredentialStore,
        LegacyCredentialFormatError,
    )
    _cred_store = CredentialStore(_cred_dir)
    try:
        raw_credentials = _cred_store.load_all_raw()
    except LegacyCredentialFormatError:
        # v1 格式凭据文件：**不能**让它崩掉 sidecar。get_status() 专门设计成
        # 不抛异常、返回 legacy_format=True，就是为了让前端引导用户「备份后重新
        # 配置」；若此处直接传播，进程活不到能响应 GET_CREDENTIALS_STATUS 的那
        # 一刻，整条引导路径永远不可达，用户只能看到每次启动都 traceback。
        # 降级留痕：视为「无凭据」启动，LLM 功能由前端门禁拦截。
        boot.warn(
            "检测到 v1 版凭据文件（含 default_provider），已忽略；"
            "请在「模型服务」中备份后重新配置"
        )
        raw_credentials = []
    _status = _cred_store.get_status()
    if _status["configured"]:
        boot.ok(f"credentials loaded ({_status['credential_count']} providers, "
                f"default={_status['default_model_id']})")
    else:
        boot.warn("no user credentials found (BYOK); LLM provisioning disabled until configured")
    boot.separator()

    boot.step("1e", "LLM 默认配置")
    # BYOK：默认模型从 raw_credentials 显式派生（is_default 凭据），不经过 env。
    # 无凭据时 sidecar 照常启动——LLM 不可用，由前端门禁（AuthGuard）拦截并引导用户配置。
    if raw_credentials:
        _default_cred = next((c for c in raw_credentials if c.get("is_default")), None)
        if _default_cred is None:
            boot.error("credentials 已配置但无 default_model_id，请通过 UI 设置默认模型")
            sys.exit(1)
        default_model_id = _default_cred["model_id"]
        default_provider = _default_cred["provider"]
        boot.ok("LLM secrets loaded")
        boot.kv("model", default_model_id)
        boot.kv("provider", default_provider)
    else:
        default_model_id = ""
        default_provider = ""
        boot.warn("LLM secrets not configured; "
                  "LLM features disabled until user provides credentials via BYOK UI")

    boot.kv("relay_url", sys_config.relay_url or "(not set)")
    boot.separator()

    # ── Step 2 · Storage ──────────────────────────────────
    boot.step(2, "Storage Manager")
    storage_manager = _build_storage_manager(
        USER_DATA_DIR / "data", user_id="", storage_mode=sys_config.storage_mode,
    )
    await storage_manager.initialize_storage()
    boot.ok("storage initialized")
    boot.kv("storage_mode", sys_config.storage_mode)
    boot.kv("storage_path", storage_manager._storage_path)

    # ── Step 3 · Session ──────────────────────────────────
    boot.step(3, "Session Manager")
    session_manager = _build_session_manager(storage_manager, config_manager)
    boot.ok("session manager ready")

    # ── Step 4 · Gateway (可选) ───────────────────────────
    boot.step(4, "Gateway Manager")
    gateway = _build_gateway(sys_config, args)
    if gateway is not None:
        boot.ok("gateway manager created")
        boot.kv("relay_url", sys_config.relay_url)
    boot.separator()

    # ── Step 5 · Build Agent Blueprint（凭据已在 Step 1d 注入）──
    boot.step(5, "Build Agent Blueprint")
    blueprint, available_models = _build_blueprint(
        raw_credentials, default_model_id, default_provider,
        user_id, USER_DATA_DIR, USER_RESOURCES_DIR, WORK_DIR, session_manager, storage_manager,
        storage_mode=sys_config.storage_mode,
    )
    boot.ok("agent blueprint built")
    boot.separator()

    # ── Step 6 · WSSGateway Transport ─────────────────────
    boot.step(6, "WSSGateway Transport")
    wss_transport = _build_wss_transport(gateway)
    if wss_transport is not None:
        # 真实 WSS 握手会在 PandaPalApp.start() 内部由 wss_transport.start() 触发，
        # 这里只构造对象（懒连接：Fail-Safe，连接失败不阻塞启动）。
        boot.ok("WSSGateway constructed (will connect on app.start, handles wecom/xiaozhi)")
    else:
        boot.warn("WSSGateway skipped (no Gateway)")
    boot.separator()

    # ── Step 7 · PandaPalApp 启动（唯一启动入口）───────
    boot.step(7, "PandaPalApp 启动")
    from pandapal.app import PandaPalApp

    app_config = {
        "user_id": user_id,
        "db_path": str(USER_DATA_DIR / "data" / "pandapal.db"),
        "data_dir": str(USER_DATA_DIR),  # ★ 目录分层：用户数据根（AppData/users/{uid}）
        "user_resources_dir": str(USER_RESOURCES_DIR),  # ★ user skills/agents 根 (WORK_DIR/.pandapal)
    }
    # 双层 Prompt：为每个模式预生成完整 prompt，供 SessionAgentPool 按 mode delta-rebind。
    # 环境块所有模式共用；PANDAPAL.md 项目指引仅 coding 注入（office 不注入）。
    from pandapal.local import prompts
    prompt_by_mode = prompts.build_prompt_map(
        _get_environment_block(WORK_DIR),
        coding_extra=_load_pandapal_md(WORK_DIR),
    )

    app = PandaPalApp(
        config=app_config,
        blueprint=blueprint,
        session_manager=session_manager,
        storage_manager=storage_manager,
        wss_transport=wss_transport,
        config_manager=config_manager,  # ★ 根本解 2026-06-10：用于 TaskScheduler
        prompt_by_mode=prompt_by_mode,
        default_mode=prompts.DEFAULT_MODE,
        available_models=available_models,       # 模型选择：可选清单（MODEL_LIST 下发）
        default_model_id=default_model_id,  # 默认模型
    )
    await app.start()
    boot.ok("PandaPalApp started")
    boot.ready()

    # 输出 PANDAPAL_READY 信号（前端 IPC 通道）
    # ★ 携带当前激活的模型 / provider，供前端展示真实模型名（key=value 格式）。
    #   sidecar 握手协议层，非 IPC 消息，故直接写 stdout（不走 IpcStdoutTransport）。
    # ★ token 回传：Rust 以 --token 传入，此处原样回传，供前端 get_auth_token 取用。
    #   此前不回传导致 BackendToken 恒为空串，而 Rust/前端却拿「token 非空」当作
    #   「sidecar 已就绪」的判据 → 热重启时 backend-ready 永不重发、界面死等。
    #   就绪判据现已改为 child.is_some()（见 sidecar.rs），token 只承担鉴权职责。
    #   顺序固定为 token → model → provider，与 parse_ready_signal 的 key=value
    #   解析一致；JWT 不含空白字符，不会破坏按空格切分。
    sys.stdout.write(
        f"PANDAPAL_READY token={args.token} "
        f"model={default_model_id} provider={default_provider}\n"
    )
    sys.stdout.flush()

    # 启动父进程监控
    if _is_frozen():
        # PandaPalApp.shutdown_event 在 stdin EOF 或 SIGINT/SIGTERM 时 set
        _start_parent_watcher(app.shutdown_event)
        boot.ok("parent-watcher started (orphan protection)")

    # ── 等待关闭信号 ─────────────────────────────────────
    try:
        # IPC server 的 stdin EOF → set shutdown_event
        # signal handler（SIGINT/SIGTERM）也 set shutdown_event
        await app.shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await asyncio.wait_for(app.stop(), timeout=30.0)
        except asyncio.TimeoutError:
            boot.warn("graceful shutdown timed out, forcing exit")
        except Exception as e:
            boot.warn(f"shutdown error: {e}")
        boot.shutdown_done()


# ══════════════════════════════════════════════════════════════════════════════
# §4  入口
# ══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    """同步入口。"""
    asyncio.run(run_local())


if __name__ == "__main__":
    main()
