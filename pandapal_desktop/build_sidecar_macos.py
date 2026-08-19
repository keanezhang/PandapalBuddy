"""
macOS sidecar builder — onedir 模式，打包完整 Python 业务。

PyInstaller --onedir 产出一个目录（可执行文件 + _internal/），
作为 Tauri bundle.resources 整体打包进 .app/Contents/Resources/。

运行后只有 1 个进程（无 stub + Python 双进程问题）。

用法:
    cd pandapal_desktop
    python build_sidecar_macos.py
"""

import subprocess
import sys
import os
import platform
import shutil

# ─── 路径设置 ───────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)  # pandapal-buddy/
TARGET_DIR = os.path.join(SCRIPT_DIR, "src-tauri", "bin")

# 根据架构确定目标名称
arch = platform.machine()  # arm64 / x86_64
if arch == "arm64":
    TARGET_NAME = "pandapal-sidecar-aarch64-apple-darwin"
elif arch == "x86_64":
    TARGET_NAME = "pandapal-sidecar-x86_64-apple-darwin"
else:
    TARGET_NAME = f"pandapal-sidecar-{arch}-apple-darwin"

ENTRY_POINT = os.path.join(PROJECT_ROOT, "pandapal", "local", "run_local.py")

# onedir 输出目录（最终位置）
OUTPUT_DIR = os.path.join(TARGET_DIR, TARGET_NAME)

# ─── 确保 PyInstaller 已安装 ───────────────────────────────────
try:
    import PyInstaller  # noqa: F401
except ImportError:
    print("⚠️  PyInstaller 未安装，正在安装...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

# ─── Hidden imports（动态导入无法被自动发现的模块）──────────────
HIDDEN_IMPORTS = [
    # ── pandapal 核心模块 ──
    "pandapal",
    "pandapal.app",
    "pandapal.local",
    "pandapal.local.run_local",
    "pandapal.local.boot_logger",
    "pandapal.local.llm_policies",
    # pandapal.config — 按关注点拆分为 system / llm / budget 三个子包
    "pandapal.config",
    # config.system — 系统配置（.env 加载、SystemConfig、异常）
    "pandapal.config.system",
    "pandapal.config.system.manager",
    "pandapal.config.system.models",
    "pandapal.config.system.exceptions",
    # config.llm — 模型配置 + 模型切换（provider 目录、凭据、可用模型清单）
    "pandapal.config.llm",
    "pandapal.config.llm.model_prices",
    "pandapal.config.llm.model_registry",
    "pandapal.config.llm.credentials_handler",
    "pandapal.config.llm.credentials_store",
    "pandapal.config.llm.provider_catalog",
    # config.budget — 计费 / 预算（价格表、停机守卫、账本、持久化）
    "pandapal.config.budget",
    "pandapal.config.budget.pricing",
    "pandapal.config.budget.guard",
    "pandapal.config.budget.ledger",
    "pandapal.config.budget.repo",
    # pandapal.storage — 含 repositories 动态发现
    "pandapal.storage",
    "pandapal.storage.manager",
    "pandapal.storage.exceptions",
    "pandapal.storage.models",
    "pandapal.storage.schema_manager",
    "pandapal.storage.repositories",
    "pandapal.storage.repositories._markdown_base",
    "pandapal.storage.repositories._sqlite_base",
    "pandapal.storage.repositories.markdown_agent_task_repo",
    "pandapal.storage.repositories.markdown_approval_repo",
    "pandapal.storage.repositories.markdown_avatar_config_repo",
    "pandapal.storage.repositories.markdown_device_repo",
    "pandapal.storage.repositories.markdown_raw_log_backend",
    "pandapal.storage.repositories.markdown_run_state_repo",
    "pandapal.storage.repositories.markdown_session_group_repo",
    "pandapal.storage.repositories.markdown_session_repo",
    "pandapal.storage.repositories.markdown_task_repo",
    "pandapal.storage.repositories.markdown_working_memory_backend",
    "pandapal.storage.repositories.sqlite_agent_task_repo",
    "pandapal.storage.repositories.sqlite_approval_repo",
    "pandapal.storage.repositories.sqlite_avatar_config_repo",
    "pandapal.storage.repositories.sqlite_device_repo",
    "pandapal.storage.repositories.sqlite_raw_log_backend",
    "pandapal.storage.repositories.sqlite_run_state_repo",
    "pandapal.storage.repositories.sqlite_session_group_repo",
    "pandapal.storage.repositories.sqlite_session_repo",
    "pandapal.storage.repositories.sqlite_summary_backend",
    "pandapal.storage.repositories.sqlite_task_repo",
    "pandapal.storage.repositories.sqlite_working_memory_backend",
    # pandapal.session
    "pandapal.session",
    "pandapal.session.manager",
    "pandapal.session.exceptions",
    "pandapal.session.session_group_handler",
    "pandapal.session.session_group_manager",
    "pandapal.session.session_list_handler",
    "pandapal.session.session_list_manager",
    # pandapal.router
    "pandapal.router",
    "pandapal.router.router",
    "pandapal.router.models",
    # pandapal.broadcast
    "pandapal.broadcast",
    "pandapal.broadcast.broadcaster",
    "pandapal.broadcast.channel_ids",
    "pandapal.broadcast.channel_registry",
    "pandapal.broadcast.models",
    "pandapal.broadcast.policy",
    "pandapal.broadcast.transport",
    # pandapal.gateway
    "pandapal.gateway",
    "pandapal.gateway.gateway",
    "pandapal.gateway.inbound_adapter",
    "pandapal.gateway.models",
    "pandapal.gateway.types",
    "pandapal.gateway.wss_transport",
    # pandapal.hitl
    "pandapal.hitl",
    "pandapal.hitl.bridge",
    "pandapal.hitl.approval_log",
    # pandapal.hooks
    "pandapal.hooks",
    "pandapal.hooks.skill_hooks",
    # pandapal.scheduler
    "pandapal.scheduler",
    "pandapal.scheduler.scheduler",
    "pandapal.scheduler.executor",
    "pandapal.scheduler.hitl_manager",
    "pandapal.scheduler.interaction_manager",
    "pandapal.scheduler.plan_manager",
    "pandapal.scheduler.stream_to_normalized",
    "pandapal.scheduler.reply_manager",
    "pandapal.scheduler.agent_pool",
    "pandapal.scheduler.background",
    # pandapal.desktop_ipc
    "pandapal.desktop_ipc",
    "pandapal.desktop_ipc.inbound_adapter",
    "pandapal.desktop_ipc.stdio_ipc",
    "pandapal.desktop_ipc.ipc_transport",
    "pandapal.desktop_ipc.message_codec",
    # pandapal.dispatch — 入站分发核心（InboundDispatcher / InboundPipeline / 适配器基类）
    "pandapal.dispatch",
    "pandapal.dispatch.adapter",
    "pandapal.dispatch.dispatcher",
    "pandapal.dispatch.pipeline",
    "pandapal.dispatch.types",
    # pandapal.tools — 动态发现（pkgutil.iter_modules），必须显式声明
    "pandapal.tools",
    "pandapal.tools.agent_task_tools",
    "pandapal.tools.scheduler_tools",
    "pandapal.tools.web_tools",
    "pandapal.tools.app_data_tools",
    "pandapal.tools.progress_tools",
    # pandapal.resources — 含 agents/skills 资源
    "pandapal.resources",
    "pandapal.resources.skill_manager",
    # pandapal.subsystem_* — 层次 3 根本解（SubsystemContainer + Registry）
    # subsystem_registry 用 函数内 import + importlib.import_module 动态加载，
    # PyInstaller 静态分析看不到，必须显式声明
    "pandapal.subsystem_container",
    "pandapal.subsystem_registry",
    # pandapal.task_scheduler
    "pandapal.task_scheduler",
    "pandapal.task_scheduler.task_scheduler",
    "pandapal.task_scheduler.models",
    # pandapal.events / messages
    "pandapal.events",
    "pandapal.events.normalized",
    "pandapal.messages",
    "pandapal.messages.types",
    # pandapal.budget — 预算管理
    "pandapal.budget",
    "pandapal.budget.handler",
    # pandapal.dashboard — 聚合看板（从 pandapal_md 构建快照）
    "pandapal.dashboard",
    "pandapal.dashboard.base",
    "pandapal.dashboard.models",
    "pandapal.dashboard.aggregator",
    "pandapal.dashboard.sqlite_aggregator",
    "pandapal.dashboard.handler",
    # pandapal.session_id — 顶层零依赖模块（session_id 唯一真相源）
    "pandapal.session_id",
    # pandapal.quality — 编码质量门控
    "pandapal.quality",
    "pandapal.quality.checker",
    "pandapal.quality.gate",
    "pandapal.quality.models",
    # pandapal.local.prompts — System Prompt 模块
    "pandapal.local.prompts",
    # pandapal.degradation — 统一降级可观测通道
    "pandapal.degradation",

    # ── pandaren SDK 核心 ──
    "pandaren",
    "pandaren.builder",
    "pandaren.cancellation",
    "pandaren.constants",
    # pandaren.identity
    "pandaren.identity",
    "pandaren.identity.models",
    # pandaren.llm — 含 providers 动态加载
    "pandaren.llm",
    "pandaren.llm.client",
    "pandaren.llm.cache_strategy",
    "pandaren.llm.cache_usage",
    "pandaren.llm.capabilities",
    "pandaren.llm.exceptions",
    "pandaren.llm.protocol",
    "pandaren.llm.responses_client",
    "pandaren.llm.router",
    "pandaren.llm.schema",
    "pandaren.llm.types",
    "pandaren.llm._internal",
    "pandaren.llm._internal.cache_primitives",
    "pandaren.llm.providers",
    "pandaren.llm.providers.dashscope",
    "pandaren.llm.providers.volcengine",
    # pandaren.tool — 含 builtin/definition/execution/exposure/registry 子包
    "pandaren.tool",
    "pandaren.tool.safe_name",
    "pandaren.tool.decorator",
    "pandaren.tool.exceptions",
    "pandaren.tool.facade",
    "pandaren.tool.loader",
    "pandaren.tool.schema_inference",
    "pandaren.tool.types",
    # pandaren.tool.builtin
    "pandaren.tool.builtin",
    "pandaren.tool.builtin.agent",
    "pandaren.tool.builtin.plan",
    "pandaren.tool.builtin.protocol",
    "pandaren.tool.builtin.search",
    "pandaren.tool.builtin.skill",
    # pandaren.tool.definition
    "pandaren.tool.definition",
    "pandaren.tool.definition.context",
    "pandaren.tool.definition.tool",
    "pandaren.tool.definition.tool_lifecycle",
    "pandaren.tool.definition.tool_policy",
    "pandaren.tool.definition.tool_result",
    "pandaren.tool.definition.tool_schema",
    # pandaren.tool.execution
    "pandaren.tool.execution",
    "pandaren.tool.execution.executor",
    "pandaren.tool.execution.guard_chain",
    # pandaren.tool.exposure
    "pandaren.tool.exposure",
    "pandaren.tool.exposure.budget",
    "pandaren.tool.exposure.gate_chain",
    "pandaren.tool.exposure.schema_builder",
    # pandaren.tool.registry
    "pandaren.tool.registry",
    "pandaren.tool.registry.discovery",
    "pandaren.tool.registry.store",
    "pandaren.tool.registry.validator",
    # pandaren.skill
    "pandaren.skill",
    "pandaren.skill.exceptions",
    "pandaren.skill.loader",
    "pandaren.skill.models",
    "pandaren.skill.registry",
    # pandaren.memory — 含 backends/compaction/reinject 子包
    "pandaren.memory",
    "pandaren.memory.constants",
    "pandaren.memory.flush_policy",
    "pandaren.memory.long_term",
    "pandaren.memory.models",
    "pandaren.memory.protocols",
    "pandaren.memory.short_term",
    "pandaren.memory.working_memory",
    "pandaren.memory.memory",
    "pandaren.memory.backends",
    "pandaren.memory.backends.sqlite_raw_log",
    "pandaren.memory.compaction",
    "pandaren.memory.compaction.micro_compact",
    "pandaren.memory.compaction.tool_pair_integrity",
    "pandaren.memory.compaction.windowed",
    "pandaren.memory.reinject",
    "pandaren.memory.reinject.coordinator",
    "pandaren.memory.reinject.sources",
    # pandaren.agent
    "pandaren.agent",
    "pandaren.agent.agent",
    "pandaren.agent.blueprint",
    # pandaren.sub_agent
    "pandaren.sub_agent",
    "pandaren.sub_agent.models",
    "pandaren.sub_agent.registry",
    "pandaren.sub_agent.loader",
    "pandaren.sub_agent.exceptions",
    # pandaren.hook
    "pandaren.hook",
    "pandaren.hook.hooks",
    # pandaren.plan
    "pandaren.plan",
    "pandaren.plan.files",
    "pandaren.plan.manager",
    "pandaren.plan.tools",
    "pandaren.plan.prompt",
    # pandaren.engine
    "pandaren.engine",
    "pandaren.engine.loop",
    "pandaren.engine.run_core",
    "pandaren.engine.models",
    "pandaren.engine.types",
    "pandaren.engine.stream",
    "pandaren.engine.message_builder",
    "pandaren.engine.output_parser",
    "pandaren.engine.step_counter",
    # pandaren.behavior — 行为控制
    "pandaren.behavior",
    "pandaren.behavior.context_window_budget",
    "pandaren.behavior.step_guard",
    "pandaren.behavior.error_policy",
    "pandaren.behavior.exceptions",
    "pandaren.behavior.execution_limits",
    "pandaren.behavior.hitl_controller",
    "pandaren.behavior.permission_guard",
    "pandaren.behavior.harness",
    "pandaren.behavior.harness.circuit_breaker",
    "pandaren.behavior.harness.executor",
    "pandaren.behavior.harness.halt",
    "pandaren.behavior.harness.idempotency",
    "pandaren.behavior.harness.output_guard",
    "pandaren.behavior.harness.rate_limiter",
    # pandaren.tools — 内置工具集（注意：与 pandaren.tool 是不同的包）
    "pandaren.tools",
    "pandaren.tools.ask_user",
    "pandaren.tools.bash",
    "pandaren.tools.glob",
    "pandaren.tools.grep",
    "pandaren.tools.math_calculator",
    "pandaren.tools.time",
    "pandaren.tools.file_tool",
    "pandaren.tools.file_tool._utils",
    "pandaren.tools.file_tool.delete_file",
    "pandaren.tools.file_tool.edit_file",
    "pandaren.tools.file_tool.list_files",
    "pandaren.tools.file_tool.read_file",
    "pandaren.tools.file_tool.write_file",
    # pandaren.observability — 可观测性
    "pandaren.observability",
    "pandaren.observability.audit",
    "pandaren.observability.config",
    "pandaren.observability.exceptions",
    "pandaren.observability.hooks_adapter",
    "pandaren.observability.logger",
    "pandaren.observability.metrics",
    "pandaren.observability.protocols",
    "pandaren.observability.provider",
    "pandaren.observability.sanitizer",
    "pandaren.observability.tracer",
    "pandaren.observability.types",
    "pandaren.observability.backend",
    "pandaren.observability.backend.console",
    "pandaren.observability.backend.in_memory",
    "pandaren.observability.backend.markdown",
    "pandaren.observability.backend.sqlite",
    # pandaren.utils — 工具函数
    "pandaren.utils",
    "pandaren.utils.file_validators",
    "pandaren.utils.path_utils",
    "pandaren.utils.project_root",

    # ── 第三方依赖（部分有动态导入/可选导入）──
    "httpx",
    "httpcore",
    "socksio",  # httpx[socks] 依赖：环境设了 socks 代理(all_proxy)时 httpx 需要它，否则 ImportError 崩溃
    "anyio",
    "anyio._backends",
    "anyio._backends._asyncio",
    "certifi",
    "h11",
    "sniffio",
    "websockets",
    "yaml",
    "dotenv",
    "cryptography",
    "aiosqlite",
    "jwt",
    "bcrypt",
    "uvicorn",
    "fastapi",
    "pydantic",
    "starlette",
    "croniter",
    # tiktoken — Token 估算器（pandaren.memory.estimators）；
    # tiktoken_ext.openai_public 的编码注册表是 importlib 动态加载，必须显式声明
    "tiktoken",
    "tiktoken_ext",
    "tiktoken_ext.openai_public",
]

# ─── 数据文件（SKILL.md 等资源文件）──────────────────────────
DATA_FILES = [
    # (source, dest_in_bundle)
    (os.path.join(PROJECT_ROOT, ".env.development"), "."),
    # system skills 随包发布（只读），user skills 从 runtime data_dir 加载
    (os.path.join(PROJECT_ROOT, "pandapal", "resources", "skills", "system"), "pandapal/resources/skills/system"),
    # agents 配置（位于 pandapal/resources/agents/ 下）
    (os.path.join(PROJECT_ROOT, "pandapal", "resources", "agents"), "pandapal/resources/agents"),
    # SDK 内置子 Agent（pandaren/agents/ 下的 explore_agent.md + plan_agent.md）
    (os.path.join(PROJECT_ROOT, "pandaren", "agents"), "pandaren/agents"),
    # ★ SQLite Schema 迁移脚本（v001_*.sql ...）——sqlite 存储模式建表的唯一来源。
    #   SchemaManager 用 Path(__file__).parent/"migrations" 定位它们；不打进包则
    #   frozen 环境下 _discover_migrations() 返回空 → 业务表全不建 → "no such table"。
    #   markdown 模式不需要迁移，故此前一直未暴露。
    (os.path.join(PROJECT_ROOT, "pandapal", "storage", "migrations"), "pandapal/storage/migrations"),
    # provider_catalog.toml — provider 元信息（provider_catalog.py 用 Path(__file__).parent 定位）
    (os.path.join(PROJECT_ROOT, "pandapal", "config", "llm", "provider_catalog.toml"), "pandapal/config/llm"),
    # ★ model_prices.toml — 系统默认单价 + 汇率。model_prices.py 在 **模块导入期** 就
    #   open() 它（计费类零默认，缺失必须 fail-fast），不打进包 → import pandapal.config
    #   整条链直接 FileNotFoundError，sidecar 起不来。
    (os.path.join(PROJECT_ROOT, "pandapal", "config", "llm", "model_prices.toml"), "pandapal/config/llm"),
    # ★ cl100k_base.tiktoken — TiktokenEstimator 的离线词表（run_local 按
    #   _MEIPASS/pandapal/resources/tokenizer/ 定位）；不打进包则估算器构造失败
    #   → 降级回 chars/4.0，压缩触发过晚的根因复活。
    (os.path.join(PROJECT_ROOT, "pandapal", "resources", "tokenizer"), "pandapal/resources/tokenizer"),
    # ★ TEST_RULE.md — coding 模式 system prompt 的测试闭环规则（prompts.py 在
    #   模块导入期 read，缺失 fail-fast）；不打进包 → import pandapal.local.prompts
    #   直接 FileNotFoundError，sidecar 起不来。
    (os.path.join(PROJECT_ROOT, "pandapal", "local", "TEST_RULE.md"), "pandapal/local"),
]

# ─── 清理旧的输出 ──────────────────────────────────────────
if os.path.exists(OUTPUT_DIR):
    print(f"🗑  Removing old output: {OUTPUT_DIR}")
    if os.path.isdir(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    else:
        # 旧的 --onefile 模式留下的单文件，直接删除
        os.remove(OUTPUT_DIR)

# ─── 构建 PyInstaller 命令 ──────────────────────────────────
cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onedir",              # ← 核心改动：onefile → onedir
    "--console",             # 保留 stdin/stdout（IPC 需要）
    "--clean",
    "--noconfirm",
    "--name", TARGET_NAME,
    "--distpath", TARGET_DIR,
    # 搜索路径：项目根目录（让 pandapal/pandaren 可被找到）
    "--paths", PROJECT_ROOT,
]

# 添加 hidden imports
for mod in HIDDEN_IMPORTS:
    cmd.extend(["--hidden-import", mod])

# 添加数据文件
for src, dest in DATA_FILES:
    if os.path.exists(src):
        cmd.extend(["--add-data", f"{src}{os.pathsep}{dest}"])

# 入口点
cmd.append(ENTRY_POINT)

# ─── 执行构建 ──────────────────────────────────────────────
print("=" * 60)
print("🔨 Building macOS sidecar (onedir mode)")
print(f"   Architecture: {arch}")
print(f"   Target name:  {TARGET_NAME}")
print(f"   Entry point:  {ENTRY_POINT}")
print(f"   Output dir:   {OUTPUT_DIR}")
print("=" * 60)
print()

try:
    subprocess.check_call(cmd)
except subprocess.CalledProcessError as e:
    print(f"\n❌ Build failed with exit code {e.returncode}")
    sys.exit(1)

# ─── 验证输出 ──────────────────────────────────────────────
exe_path = os.path.join(OUTPUT_DIR, TARGET_NAME)
if os.path.exists(exe_path):
    # 确保可执行权限
    os.chmod(exe_path, 0o755)

    # 计算总目录大小
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(OUTPUT_DIR):
        for f in filenames:
            total_size += os.path.getsize(os.path.join(dirpath, f))
    size_mb = total_size / (1024 * 1024)

    print()
    print("=" * 60)
    print("✅ Sidecar built successfully! (onedir mode)")
    print(f"   Output dir: {OUTPUT_DIR}")
    print(f"   Executable: {exe_path}")
    print(f"   Total size: {size_mb:.1f} MB")
    print("   Mode:       SINGLE PROCESS (no stub)")
    print("=" * 60)
else:
    print(f"\n❌ Expected executable not found: {exe_path}")
    sys.exit(1)

print()
print("   Next steps:")
print(f"   1. Test:  ./{os.path.relpath(exe_path, SCRIPT_DIR)} --user-id test --token test")
print("   2. Build: pnpm tauri build")
