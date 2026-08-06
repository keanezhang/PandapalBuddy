"""pandapal.subsystem_container — IoC 容器（★ 根本解 2026-06-11 层次 3）。

为什么需要这个模块：
  之前 PandaPalApp.start() 是手写 11 步序列（步骤编号 2.5/2.6/2.7/3/5/6/6.5/6.6/7/8），
  任何"记着调"的反模式（比如忘了在步骤 6.5 调 inject_task_scheduler）都需要
  AST 静态扫描 + 启动期 fail-fast 来兜底。

  本模块提供 SubsystemContainer：
    - 声明式：每个子系统用 SubsystemSpec 自描述（name/factory/needs/inject_into）
    - 自动拓扑排序：容器按 needs 自动决定启动顺序
    - 自动注入：实例化后自动注入到目标对象的字段
    - 启动期校验：缺依赖时立即报"哪个 spec 缺哪个 type"，不再静默 None
    - 零 import 副作用：PandaPalApp 不再 import 子系统类，依赖图集中管理

设计原则：
  - 容器只做"装配 + 启动"，不持有业务逻辑
  - factory 必须是"无副作用"的纯构造函数（或带 async 初始化的工厂对象）
  - 循环依赖立即抛异常（带完整环路径）
  - 失败隔离（HC3）：单个子系统启动失败不影响其他（但容器会记录并 raise aggregate error）

使用：
    from pandapal.subsystem_container import (
        SubsystemContainer, SubsystemSpec, AppContext,
    )

    container = SubsystemContainer(context=AppContext(...))
    container.register(SubsystemSpec(
        name="task_scheduler",
        factory=lambda broadcast, router, task_repo: TaskScheduler(
            task_repo=task_repo, broadcast=broadcast, router=router,
            config_manager=context.config_manager,
        ),
        needs=(MessageBroadcast, MessageRouter, TaskRepository),
        inject_into=((SchedulerTools, "_ts", TaskScheduler),),
    ))
    await container.start_all()
    scheduler = container.get("task_scheduler")
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Type

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 外部上下文（不是容器管理的依赖，但 factory 启动时需要用）
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class AppContext:
    """外部传入的、容器不构造的依赖。

    容器在实例化 subsystem 时，把 context_needs 中声明的 type 对应的实例
    注入到 factory kwargs 中。

    示例：
        AppContext(
            blueprint=blueprint,
            session_manager=session_manager,
            storage_manager=storage_manager,
            config_manager=config_manager,
            wss_transport=wss_transport,
            shutdown_event=shutdown_event,
            user_id="alice",
        )
    """

    # ★ 多 Session 并发改造：只传 AgentBlueprint，不传单 Agent 实例。
    #   Pool 消费 blueprint 按需 materialize Agent。
    blueprint: Any = None
    session_manager: Any = None
    storage_manager: Any = None
    config_manager: Any = None
    wss_transport: Any = None
    shutdown_event: asyncio.Event | None = None
    user_id: str = ""
    # 双层 Prompt：{mode: 完整prompt} + 缺省模式，透传给 SessionAgentPool 做 delta-rebind。
    prompt_by_mode: dict[str, str] | None = None
    default_mode: str = ""

    def get(self, t: Type) -> Any:
        """按类型获取外部依赖（★ 容器内部使用）。

        ★ 特殊：当 t == AppContext 时返回 self（让 context_needs=(AppContext,) 能拿到 context）。
        """
        # 特殊：context 本身就是 AppContext 类型
        if t is AppContext:
            return self
        for attr_name in self.__dataclass_fields__:
            value = getattr(self, attr_name)
            if isinstance(value, t) if isinstance(value, type) else False:
                return value
            # 宽松匹配：t 是 value 的类型
            if isinstance(value, t):
                return value
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 子系统规格（自描述）
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class SubsystemSpec:
    """子系统规格：自描述 + 自依赖 + 自注入。

    Args:
        name: 唯一名称（用于 container.get(name)）
        factory: 构造函数。kwargs 由 needs + context_needs 解析后传入。
                 factory 的 return annotation 决定该子系统"提供"的类型。
                 容器把 spec 的返回类型作为其他 spec 的 needs 解析键。
        needs: 依赖的其他子系统（按类型查找），从 container._instances 取
        context_needs: 依赖的外部上下文（按类型查找），从 context.get() 取
        inject_into: 启动后自动注入到目标对象的字段
                      格式：((target_obj, field_name, expected_type), ...)
        start: 是否在 start_all() 中调 instance.start()（默认 True）
        init: 可选 async 初始化函数（构造后、start 前调用）

    注入语义（inject_into）：
        容器实例化本 spec 后，遍历 inject_into 列表，对每个元组：
          - target_obj：目标对象（一般是 Provider 类的 _module_class_ref）
          - field_name：要设置的属性名
          - expected_type：期望类型（用于校验）
        然后 setattr(target_obj, field_name, instance)
        目标对象可以是：
          - 类的 _instance 引用（Provider 用 class-level singleton）
          - 或一个具体对象（inject 时容器用 weakref 持有）
    """

    name: str
    factory: Callable[..., Any]
    needs: tuple[Type, ...] = ()
    context_needs: tuple[Type, ...] = ()
    inject_into: tuple[tuple[Any, str, Any], ...] = ()
    start: bool = True
    init: Callable[[Any], Any] | None = None  # sync or async


# ══════════════════════════════════════════════════════════════════════════════
# IoC 容器
# ══════════════════════════════════════════════════════════════════════════════


class CircularDependencyError(RuntimeError):
    """循环依赖异常（带环路径）。"""


class SubsystemContainer:
    """IoC 容器：自动按依赖图拓扑排序启动所有子系统。

    启动流程：
      1. 拓扑排序（基于 factory 参数名 + 类型注解）
      2. 按顺序实例化（解析 needs + context_needs）
      3. 调用 init()（如有）
      4. 调用 start()（如 spec.start=True）
      5. 执行 inject_into

    错误处理：
      - 缺依赖：抛 KeyError / RuntimeError 带具体 spec 和 type
      - 循环依赖：抛 CircularDependencyError 带环路径
      - 启动失败：抛 RuntimeError 带失败 spec 名称（不中断其他 spec）
    """

    def __init__(self, context: AppContext | None = None) -> None:
        self._context = context or AppContext()
        self._specs: dict[str, SubsystemSpec] = {}
        self._instances: dict[str, Any] = {}
        self._type_to_name: dict[Type, str] = {}

    # ── 注册 ──────────────────────────────────────────────────────────────

    def register(self, spec: SubsystemSpec) -> None:
        """注册一个子系统。重复注册同名 spec 抛 ValueError。"""
        if spec.name in self._specs:
            raise ValueError(
                f"Subsystem name 重复: '{spec.name}' —— "
                f"检查 register_pandapal_subsystems() 是否多次注册了同一子系统。"
            )
        self._specs[spec.name] = spec
        # 记录 type → name 索引（按 factory 返回类型）
        return_type = _get_factory_return_type(spec.factory)
        if return_type is not None and return_type is not type(None):
            if return_type in self._type_to_name:
                logger.warning(
                    "Container: type %s 已被 '%s' 提供，又被 '%s' 覆盖 —— "
                    "可能造成 needs 解析歧义",
                    return_type.__name__,
                    self._type_to_name[return_type],
                    spec.name,
                )
            self._type_to_name[return_type] = spec.name

    # ── 启动 ──────────────────────────────────────────────────────────────

    async def start_all(self) -> None:
        """按依赖图拓扑排序启动所有子系统。

        失败隔离：单个 spec 启动失败 → 记录到 _failures，但不中断其他 spec。
        启动后调用 get_failures() 可获取失败列表。
        """
        order = self._topo_sort()
        failures: list[tuple[str, BaseException]] = []
        for name in order:
            spec = self._specs[name]
            try:
                instance = self._instantiate(spec)
                self._instances[name] = instance
                logger.info("Container: %s instantiated", name)
            except Exception as e:
                logger.error("Container: %s instantiation failed: %s", name, e)
                failures.append((name, e))
                continue

            # init()（构造后、start 前的钩子）
            if spec.init is not None:
                try:
                    result = spec.init(instance)
                    if inspect.isawaitable(result):
                        await result
                except Exception as e:
                    logger.error("Container: %s init() failed: %s", name, e)
                    failures.append((name, e))
                    continue

            # start()
            if spec.start and hasattr(instance, "start"):
                try:
                    result = instance.start()
                    if inspect.isawaitable(result):
                        await result
                    logger.info("Container: %s started", name)
                except Exception as e:
                    logger.error("Container: %s start() failed: %s", name, e)
                    failures.append((name, e))
                    continue

            # inject_into
            for target, field_name, source in spec.inject_into:
                # source 可以是 Type（校验本 instance）或 str（按 spec name 取）
                if isinstance(source, type):
                    if not isinstance(instance, source):
                        logger.warning(
                            "Container: %s inject_into 期望类型 %s，实际 %s，跳过",
                            name, source.__name__, type(instance).__name__,
                        )
                        continue
                    value = instance
                elif isinstance(source, str):
                    # 按 spec name 取
                    if source not in self._instances:
                        logger.warning(
                            "Container: %s inject_into 引用 spec '%s' 未启动，跳过",
                            name, source,
                        )
                        continue
                    value = self._instances[source]
                else:
                    logger.warning(
                        "Container: %s inject_into source 类型非法: %s，跳过",
                        name, type(source).__name__,
                    )
                    continue
                try:
                    setattr(target, field_name, value)
                    logger.info(
                        "Container: %s injected into %s.%s (source=%s)",
                        name, type(target).__name__, field_name,
                        source.__name__ if isinstance(source, type) else source,
                    )
                except Exception as e:
                    logger.error(
                        "Container: %s inject_into %s.%s failed: %s",
                        name, type(target).__name__, field_name, e,
                    )
                    failures.append((name, e))

        if failures:
            failed_names = [n for n, _ in failures]
            details = "\n".join(
                f"  - {n}: {type(e).__name__}: {e}" for n, e in failures
            )
            raise RuntimeError(
                f"Container: {len(failures)} 个子系统启动失败: {failed_names}\n"
                f"{details}"
            )

    async def stop_all(self) -> None:
        """按启动顺序的逆序停止所有子系统（对称关闭）。"""
        # 收集已启动的 spec，按 _instances 插入顺序倒序
        names = list(self._instances.keys())
        for name in reversed(names):
            instance = self._instances[name]
            if hasattr(instance, "stop"):
                try:
                    result = instance.stop()
                    if inspect.isawaitable(result):
                        await result
                    logger.info("Container: %s stopped", name)
                except Exception as e:
                    logger.warning("Container: %s stop() error: %s", name, e)

    # ── 访问 ──────────────────────────────────────────────────────────────

    def get(self, name: str) -> Any:
        """按名称获取已启动的子系统实例。"""
        if name not in self._instances:
            raise KeyError(
                f"Subsystem '{name}' 不存在或未启动。"
                f"已注册: {list(self._specs.keys())}；"
                f"已启动: {list(self._instances.keys())}"
            )
        return self._instances[name]

    def get_by_type(self, t: Type) -> Any:
        """按类型获取已启动的子系统实例。"""
        if t not in self._type_to_name:
            raise KeyError(
                f"没有子系统提供类型 {t.__name__}。"
                f"已注册类型: {[tt.__name__ for tt in self._type_to_name]}"
            )
        name = self._type_to_name[t]
        return self._instances[name]

    def has(self, name: str) -> bool:
        return name in self._instances

    @property
    def context(self) -> AppContext:
        return self._context

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _instantiate(self, spec: SubsystemSpec) -> Any:
        """解析 needs + context_needs → kwargs → 调 factory。

        ★ 关键设计（2026-06-11 修复）：
          按 factory 的 parameter annotation 匹配 dep type → 用 **参数名**（不是类型名）
          当 kwargs key。强制要求 factory 参数带类型注解（这是契约，不可省）。
        """
        sig = inspect.signature(spec.factory)
        ann_to_pname: dict[Type, str] = {}
        for pname, param in sig.parameters.items():
            ann = param.annotation
            if ann is inspect.Parameter.empty:
                # ★ 严格要求：factory 参数必须有类型注解
                # 不允许 `def make_x(_FooT)` 这种"参数名碰巧 == 类名"的写法
                continue
            # string annotation（from __future__ import annotations）
            if isinstance(ann, str):
                try:
                    resolved = eval(ann, spec.factory.__globals__)  # noqa: S307 — 自有 globals
                    if isinstance(resolved, type):
                        ann_to_pname[resolved] = pname
                except Exception:
                    continue
            elif isinstance(ann, type):
                ann_to_pname[ann] = pname

        kwargs: dict[str, Any] = {}

        # 1. needs：从容器内其他子系统取
        for dep_type in spec.needs:
            pname = ann_to_pname.get(dep_type)
            if pname is None:
                raise RuntimeError(
                    f"Subsystem '{spec.name}' needs {dep_type.__name__}，"
                    f"但 factory {spec.factory.__name__} 的参数中没有该类型注解。"
                    f"factory 参数: {list(sig.parameters)}"
                )
            dep_name = self._type_to_name.get(dep_type)
            if dep_name is None or dep_name not in self._instances:
                raise RuntimeError(
                    f"Subsystem '{spec.name}' needs {dep_type.__name__}，"
                    f"但容器内没有该类型的子系统。"
                    f"已注册类型: {[t.__name__ for t in self._type_to_name]}"
                )
            kwargs[pname] = self._instances[dep_name]

        # 2. context_needs：从 AppContext 取
        for ctx_type in spec.context_needs:
            pname = ann_to_pname.get(ctx_type)
            if pname is None:
                raise RuntimeError(
                    f"Subsystem '{spec.name}' needs context {ctx_type.__name__}，"
                    f"但 factory {spec.factory.__name__} 的参数中没有该类型注解。"
                    f"factory 参数: {list(sig.parameters)}"
                )
            ctx_value = self._context.get(ctx_type)
            if ctx_value is None:
                raise RuntimeError(
                    f"Subsystem '{spec.name}' needs context {ctx_type.__name__}，"
                    f"但 AppContext 未提供。"
                    f"AppContext 字段: {list(self._context.__dataclass_fields__)}"
                )
            kwargs[pname] = ctx_value

        return spec.factory(**kwargs)

    def _topo_sort(self) -> list[str]:
        """基于 spec.needs 解析依赖关系，返回拓扑序（依赖在前）。

        算法：DFS 后序遍历
          - 对每个 spec，递归访问其所有依赖
          - 访问完所有依赖后再添加自己到结果
          - 这样依赖一定先于自己出现在结果中

        循环依赖检测：用 visiting 集合标记当前递归路径上的节点。
        """
        dependency: dict[str, set[str]] = {}
        for name, spec in self._specs.items():
            dependency[name] = set()
            for dep_type in spec.needs:
                dep_name = self._type_to_name.get(dep_type)
                if dep_name is None:
                    raise RuntimeError(
                        f"Subsystem '{name}' needs {dep_type.__name__}，"
                        f"但没有任何 spec 的 factory 返回该类型。"
                        f"已注册 spec 的返回类型: "
                        f"{[t.__name__ for t in self._type_to_name]}"
                    )
                if dep_name not in self._specs:
                    raise RuntimeError(
                        f"Subsystem '{name}' needs {dep_name}，但该 spec 未注册。"
                    )
                dependency[name].add(dep_name)

        order: list[str] = []
        visited: set[str] = set()
        visiting: set[str] = set()  # 当前 DFS 路径上（环检测）

        def visit(name: str, path: list[str]) -> None:
            if name in visited:
                return
            if name in visiting:
                cycle = " → ".join(path + [name])
                raise CircularDependencyError(
                    f"Subsystem 循环依赖: {cycle}"
                )
            visiting.add(name)
            for dep in dependency.get(name, ()):
                visit(dep, path + [name])
            visiting.remove(name)
            visited.add(name)
            order.append(name)

        for name in self._specs:
            visit(name, [])

        return order


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════


def _get_factory_return_type(factory: Callable[..., Any]) -> Type | None:
    """从 factory 的 return annotation 解析返回类型。

    处理：
      - 直接类型注解：lambda x: Foo → Foo
      - string 注解（from __future__ import annotations 或 `Foo | None`）：
        在 factory 所在模块的 globals 里 eval 解析
      - Union 类型（含 None）：提取非 None 的 type（如 AgentScheduler | None → AgentScheduler）
      - Union 类型（全部非 None）：取第一个 type

    ★ 为什么不用 inspect.get_annotations(eval_str=True)：
      它对 PEP 604 的 `Foo | None` string 形式解析不了（仍然是 string），
      我们需要直接 eval 才能拿到真实类型对象。
    """
    import typing
    hints = getattr(factory, "__annotations__", {})
    if "return" not in hints:
        return None
    ret = hints["return"]
    if isinstance(ret, str):
        try:
            # 在 factory 所在模块的 globals 里 eval（支持 PEP 604 的 `|` 语法）
            module_globals = getattr(factory, "__globals__", {})
            ret = eval(ret, module_globals)  # noqa: S307 — eval 自有 globals
        except Exception:
            # 注解字符串 eval 失败=「返回类型未知」，best-effort 内省；留痕以暴露 DI 注解笔误。
            logger.debug("无法解析 factory 返回注解: %r", ret, exc_info=True)
            return None
    # 处理 Union 类型（AgentScheduler | None / Union[A, B]）—— 提取非 None 的 type
    if isinstance(ret, type):
        return ret
    origin = getattr(ret, "__origin__", None)
    # 优先用 typing.get_args（兼容 typing.Union 和 types.UnionType）
    try:
        args = typing.get_args(ret)
    except Exception:
        args = getattr(ret, "__args__", ())
    non_none = [a for a in args if a is not type(None)]
    if len(non_none) == 1 and isinstance(non_none[0], type):
        return non_none[0]
    return None
