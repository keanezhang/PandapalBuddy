"""pandapal.scheduler.stream_to_normalized — StreamEvent → NormalizedEvent 转换器。

★ 5.2.I 核心设计：
- 这是唯一的转换点，所有 SDK 事件 → 系统事件都在此完成
- 转换时自动注入 reply_id（从 ReplyIdManager 获取）
- 转换失败的消息会被捕获并转为 NormalizedEvent(ERROR)
- ★ 关键：convert_stream_event_to_normalized 是 generator，支持 yield 多个事件
  例：HITL_REQUESTED → 先 yield REPLY_END 再 yield HITL_REQUEST
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Iterator, TYPE_CHECKING

from pandapal.events.normalized import EventType, NormalizedEvent

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# StreamEventType → EventType 映射表（与 pandaren.engine.models.StreamEventType 对齐）
STREAM_TO_NORMALIZED_MAPPING: dict[str, EventType] = {
    "run_start":                EventType.RUN_START,
    "run_end":                  EventType.RUN_END,
    "llm_token":                EventType.LLM_TOKEN,
    "llm_reasoning_token":      EventType.REASONING_TOKEN,
    "tool_call_start":          EventType.TOOL_START,
    "tool_call_end":            EventType.TOOL_END,
    "hitl_requested":           EventType.HITL_REQUEST,
    "interaction_requested":    EventType.INTERACTION_REQUEST,
    "plan_approval_requested":  EventType.PLAN_APPROVAL_REQUEST,
    "permission_denied":        EventType.PERMISSION_DENIED,
    "agent_halted":             EventType.AGENT_HALTED,
    "agent_cancelled":          EventType.AGENT_HALTED,
}


# ── AGENT_HALTED 友好提示映射表 ──────────────────────────────────────────
# terminal_reason → (halt_kind, friendly_prefix)
# halt_kind 供前端按类别选图标，friendly_prefix 是面向用户的中文引导语。
# 引擎 13 种 terminal_reason + executor 的 budget_exhausted（不走 converter）。
_HALT_REASON_MAP: dict[str, tuple[str, str]] = {
    "llm_error":             ("llm_error",             "LLM 调用异常，Agent 已停止"),
    "halted_by_guard":        ("guard_halt",            "触发保护机制，Agent 已停止"),
    "max_steps_exceeded":     ("max_steps",             "达到最大步数限制，Agent 已停止"),
    "step_timeout":          ("timeout",                "步骤执行超时，Agent 已停止"),
    "total_timeout":         ("timeout",                "总执行超时，Agent 已停止"),
    "circuit_breaker":       ("circuit_breaker",        "熔断保护触发，Agent 已停止"),
    "llm_loop_detected":     ("loop_detected",          "检测到 LLM 输出循环，Agent 已停止"),
    "context_overflow":      ("context_overflow",       "上下文超出 token 限制，Agent 已停止"),
    "tool_halt":             ("tool_halt",              "工具要求停止，Agent 已停止"),
    # ★ 当前引擎设计：工具全失败时让 LLM 自我纠正，不立即 emit AGENT_HALTED，
    #   故 tools_exhausted 暂不会被命中。保留此条目作为防御（未来引擎可能改变策略）。
    "tools_exhausted":       ("tools_exhausted",        "工具调用预算耗尽，Agent 已停止"),
    "permission_exhausted":  ("permission_exhausted",   "权限申请被拒绝次数过多，Agent 已停止"),
    "hitl_rejected":         ("hitl_rejected",          "人工审批被拒绝，Agent 已停止"),
    "audit_failure":         ("audit_failure",          "审计检查不通过，Agent 已停止"),
    "cancelled":             ("cancelled",              "用户取消，Agent 已停止"),
}


# 故意不转发给前端聊天的 SDK 事件：循环内核的生命周期/观测埋点。
# 它们的正经消费方是 SDK 自己的 Tracer/Metrics（on_step_*/on_*_llm_call hook
# → observability.db → 前端 Dashboard 按 (run_id, step) 聚合展示），
# 不属于聊天渲染契约（前端出站类型无 STEP / LLM_CALL 档）。
# ★ 与「映射滞后漏登记」的区分点：不在映射表、也不在此集，才是真·疑似丢事件 → 才 warning。
#   显式登记于此 → 静默跳过不刷屏，同时保留审计 #12 对「SDK 新增该转发事件却漏更映射」的告警精度。
_BACKEND_ONLY_EVENTS: frozenset[str] = frozenset({
    "step_start",
    "step_end",
    "llm_call_start",
    "llm_call_end",
    "handoff",  # P2 预留，尚未实现
})


# 需要"先关流再发本体"的事件类型
_STREAM_CLOSING_EVENTS: frozenset[str] = frozenset({
    "hitl_requested",
    "interaction_requested",
    "plan_approval_requested",
    "permission_denied",
    "agent_halted",
    "agent_cancelled",
})

# 关流时 REPLY_END 的 status 字段
_STREAM_CLOSING_STATUS: dict[str, str] = {
    "hitl_requested":           "paused_for_hitl",
    "interaction_requested":    "paused_for_interaction",
    "plan_approval_requested":  "paused_for_plan_approval",
    "permission_denied":        "permission_denied",
    "agent_halted":             "halted",
    "agent_cancelled":          "halted",
}


def convert_stream_event_to_normalized(
    event_type: str,
    data: dict,
    run_id: str,
    reply_id: str,
    tool_name: str | None = None,
) -> Iterator[NormalizedEvent]:
    """将 SDK StreamEvent 转换为 NormalizedEvent 列表（generator）。

    ★ 关键改造：支持 yield 多个事件。
      - 普通事件：yield 1 个
      - HITL_REQUESTED：先 yield REPLY_END（关流），再 yield HITL_REQUEST
      - INTERACTION_REQUESTED：先 yield REPLY_END，再 yield INTERACTION_REQUEST
      - PERMISSION_DENIED / AGENT_HALTED：先 yield REPLY_END，再 yield 本体

    ★ 责任归属：
      Transport 不做"先关流再发"的配对（不该 transport 越权做业务）。
      这个配对是 Scheduler 转换层的责任——Agent 暂停时前端 UI 状态需要收尾。

    Args:
        event_type: StreamEvent 的 type 字段（字符串，与 pandaren 对齐）
        data: StreamEvent 的 data 字段
        run_id: Agent run 的 ID
        reply_id: 当前回复周期的 ID
        tool_name: StreamEvent 的 tool_name 字段（如果 SDK 提供了）
    """
    if event_type in _BACKEND_ONLY_EVENTS:
        # 故意不转发给聊天：数据已由 observability hook 送达 Dashboard（见 _BACKEND_ONLY_EVENTS 注释）。
        # 这是「有据的静默」，不是降级 —— 不报 warning，也不进兜底告警分支。
        return

    target_type = STREAM_TO_NORMALIZED_MAPPING.get(event_type)
    if target_type is None:
        # 未登记映射、也不在 _BACKEND_ONLY_EVENTS → 真·疑似丢事件：SDK 新增了「该转发」的事件类型却
        # 漏更映射表。这是「结果不对但没报错」的典型：DEBUG 生产通常关闭 = 零留痕。升 warning 暴露
        # 「映射表滞后于 SDK」（静默降级审计 #12 / §1.1 原则三）。此时事件类型有限，不会刷屏。
        logger.warning(
            "stream event %r 无 NormalizedEvent 映射，已丢弃（前端收不到）→ 请在 "
            "STREAM_TO_NORMALIZED_MAPPING 补登记（SDK 新增事件类型未同步，见静默降级审计 #12）。",
            event_type,
        )
        return

    try:
        # ★ HITL_REQUESTED / INTERACTION_REQUESTED / PERMISSION_DENIED / AGENT_HALTED
        #   这些事件语义上"结束当前回复周期"，必须先 yield REPLY_END
        if event_type in _STREAM_CLOSING_EVENTS:
            # 对于 agent_halted / agent_cancelled：提前算 halt_kind，让 REPLY_END 也带上，
            #   与 executor 预算预检路径保持一致（两条路产出的 REPLY_END 都有 halt_kind）。
            _halt_kind = ""
            if event_type == "agent_halted":
                _term = data.get("terminal_reason", "")
                _halt_kind, _ = _HALT_REASON_MAP.get(_term, ("unknown", ""))
            elif event_type == "agent_cancelled":
                _halt_kind = "cancelled"
            # 第一步：yield REPLY_END 关闭当前流
            _reply_payload: dict = {
                "output": "",
                "status": _STREAM_CLOSING_STATUS.get(event_type, "ok"),
            }
            if _halt_kind:
                _reply_payload["halt_kind"] = _halt_kind
            yield NormalizedEvent(
                event_type=EventType.REPLY_END,
                reply_id=reply_id,
                run_id=run_id,
                payload=_reply_payload,
            )
            # 第二步：yield 实际事件（HITL_REQUEST / INTERACTION_REQUEST / ...）
            yield from _convert_event_body(
                event_type, data, run_id, reply_id, tool_name, target_type
            )
            return

        # 其他事件：单 yield
        yield from _convert_event_body(
            event_type, data, run_id, reply_id, tool_name, target_type
        )

    except Exception as e:
        logger.exception("failed to convert %s to normalized: %s", event_type, e)
        yield NormalizedEvent.error(
            error_code="convert_failed",
            error_message=str(e),
            error_detail=f"original_event_type={event_type}",
            reply_id=reply_id,
            run_id=run_id,
        )


def _convert_event_body(
    event_type: str,
    data: dict,
    run_id: str,
    reply_id: str,
    tool_name: str | None,
    target_type: EventType,
) -> Iterator[NormalizedEvent]:
    """转换事件主体（不含"先关流"配对）。"""
    if event_type == "llm_token":
        yield NormalizedEvent.llm_token(
            delta=data.get("delta", ""),
            snapshot=data.get("snapshot", ""),
            reply_id=reply_id,
            run_id=run_id,
            msg_id=data.get("msg_id"),
        )
    elif event_type == "llm_reasoning_token":
        yield NormalizedEvent(
            event_type=EventType.REASONING_TOKEN,
            reply_id=reply_id,
            run_id=run_id,
            payload={"delta": data.get("delta", ""), "snapshot": data.get("snapshot", "")},
        )
    elif event_type == "run_start":
        # REPLY_START 已由 _execute_agent_run_stream 在启动流之前发出（带 scope），
        # 此处不再重复 emit，避免无 scope 的重复事件。
        pass
    elif event_type == "run_end":
        yield NormalizedEvent(
            event_type=EventType.REPLY_END,
            reply_id=reply_id,
            run_id=run_id,
            payload={
                "output": data.get("output", ""),
                "status": data.get("status", "ok"),
            },
        )
    elif event_type == "tool_call_start":
        actual_tool_name = tool_name or data.get("tool_name", "unknown")
        yield NormalizedEvent.tool_start(
            tool_name=actual_tool_name,
            tool_call_id=data.get("tool_call_id", run_id or "unknown"),
            tool_args=data.get("tool_args", {}),
            reply_id=reply_id,
            run_id=run_id,
        )
    elif event_type == "tool_call_end":
        # ★ 5.2：engine emit 的 data 含 result(完整)/success(bool)/tool_args(完整)/error(str)，
        #   之前的 converter 错读 data.get("is_error") / data.get("duration_ms") 全是空。
        #   success → is_error 反转；result/tool_args/error 引擎已发，透传即可。
        #   5.2 协议：engine 必发 success，缺字段视为协议破坏（Converter 不兜底）。
        actual_tool_name = tool_name or data.get("tool_name", "unknown")
        # feedback：engine 发的是 {text, severity, source} 或 None（见 run_core
        # .feedback_to_event_data）。此处**原样透传不解释** —— converter 是管道不是
        # 解释器；对 source/severity 取值做判断会让「加一个新 provider」变成要改这里。
        yield NormalizedEvent.tool_end(
            tool_name=actual_tool_name,
            tool_call_id=data.get("tool_call_id", "unknown"),
            result_full=data.get("result"),
            result_error=data.get("error"),
            is_error=not data.get("success"),
            duration_ms=data.get("duration_ms"),
            tool_args=data.get("tool_args", {}),
            feedback=data.get("feedback"),
            reply_id=reply_id,
            run_id=run_id,
        )
    elif event_type == "hitl_requested":
        # 注意：HITL_REQUESTED 走 _STREAM_CLOSING_EVENTS 分支，REPLY_END 已在外面 yield
        actual_tool_name = tool_name or data.get("pending_tool_name", "unknown")
        # ★ 5.2：engine 必发 pending_tool_args（透传给前端 tool_args_summary），
        #   不读 data.get("pending_tool_args_summary")（老协议字段，engine 已不发）。
        tool_args_summary = data.get("pending_tool_args", {})
        approval_id = data.get("approval_id") or _generate_approval_id()
        # ★ 5.2：engine 把 session_id 放在 run_state（dataclass）里，不在 data 顶层。
        session_id = _session_id_from_run_state(data.get("run_state"))
        # ★ reply_id == run_id（Option C 硬约定）
        yield NormalizedEvent.hitl_request(
            approval_id=approval_id,
            tool_name=actual_tool_name,
            tool_args_summary=tool_args_summary,
            session_id=session_id,
            run_id=run_id,
        )
    elif event_type == "interaction_requested":
        # ★ 引擎产出的事件 data 是
        #   {"run_state": ..., "tool_name": "ask_user",
        #    "tool_args": {"questions_json": "[{...question, header, options, multiSelect...}]"}}
        # 解析全部问题，一次 INTERACTION_REQUEST 携带所有问题传给前端。
        resolved_tool_name = tool_name or data.get("tool_name", "unknown")

        # ★ 防御性解析：tool_args 可能是 dict 或 str（引擎不同版本）
        raw_tool_args = data.get("tool_args")
        if isinstance(raw_tool_args, dict):
            tool_args = raw_tool_args
        elif isinstance(raw_tool_args, str):
            try:
                tool_args = json.loads(raw_tool_args)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.warning(
                    "INTERACTION_REQUEST: tool_args 是 str 但无法 JSON 解析: %s, raw=%.200s",
                    e, raw_tool_args,
                )
                tool_args = {}
        else:
            tool_args = {}

        questions_json = tool_args.get("questions_json") or data.get("questions_json")

        logger.info(
            "[converter] interaction_requested: tool_name=%s questions_json_type=%s",
            resolved_tool_name, type(questions_json).__name__,
        )

        all_questions = _extract_all_questions(questions_json)

        logger.info(
            "[converter] interaction_requested: extracted %d questions, reply_id=%s",
            len(all_questions), reply_id,
        )
        if not all_questions:
            logger.warning(
                "[converter] interaction_requested: NO questions extracted! "
                "questions_json=%.200s data_keys=%s",
                str(questions_json)[:200], list(data.keys()) if data else [],
            )

        yield NormalizedEvent.interaction_request(
            request_id=data.get("request_id", _generate_approval_id()),
            questions=all_questions,
            tool_name=resolved_tool_name,
            reply_id=reply_id,
            run_id=run_id,
        )
    elif event_type == "permission_denied":
        # ★ 5.2：engine emit 的 data 是 {"sensitive_permission": "network" | ...}
        #   之前 converter 读 data.get("reason") 永远空串（老协议字段已废弃）。
        #   sensitive_permission 是权限名（network/filesystem/...），包成人类可读串塞到 reason。
        actual_tool_name = tool_name or data.get("tool_name", "unknown")
        sensitive = data.get("sensitive_permission")
        # 5.2 协议：sensitive_permission 是必发字段，缺字段视为协议破坏。
        reason = f"requires '{sensitive}' permission" if sensitive else "permission denied"
        yield NormalizedEvent.permission_denied(
            tool_name=actual_tool_name,
            reason=reason,
            reply_id=reply_id,
            run_id=run_id,
        )
    elif event_type == "plan_approval_requested":
        # Plan Mode 提交审批：读取 plan_content，走 _STREAM_CLOSING_EVENTS 分支
        plan_path = data.get("plan_path", "")
        plan_content = data.get("plan_content", "")
        session_id = data.get("session_id", "")
        user_id = data.get("user_id", "")
        yield NormalizedEvent.plan_approval_request(
            plan_path=plan_path,
            plan_content=plan_content,
            run_id=run_id,
            session_id=session_id,
            user_id=user_id,
        )
    elif event_type == "agent_halted":
        # ★ 5.2：engine emit 的 data 是 {"terminal_reason": "...", "error": "..."}
        #   拼友好 reason + halt_kind，让前端按类别选图标并展示完整原因。
        term = data.get("terminal_reason", "")
        err = data.get("error", "")
        halt_kind, friendly_prefix = _HALT_REASON_MAP.get(
            term, ("unknown", "Agent 异常停止")
        )
        reason = friendly_prefix + "。"
        if err:
            reason += f"\n详细信息：{err}"
        yield NormalizedEvent.agent_halted(
            reason=reason,
            halt_kind=halt_kind,
            reply_id=reply_id,
            run_id=run_id,
        )
    elif event_type == "agent_cancelled":
        # 用户主动取消：data 携带 {"error": "Cancelled by user"}
        err = str(data.get("error") or "Cancelled by user")
        halt_kind, friendly_prefix = _HALT_REASON_MAP.get(
            "cancelled", ("cancelled", "用户取消，Agent 已停止")
        )
        reason = friendly_prefix + "。\n详细信息：" + err
        yield NormalizedEvent.agent_halted(
            reason=reason,
            halt_kind=halt_kind,
            reply_id=reply_id,
            run_id=run_id,
        )
    else:
        # 未识别的 StreamEvent：原样透传
        yield NormalizedEvent(
            event_type=target_type,
            reply_id=reply_id,
            run_id=run_id,
            payload=data,
        )


def _generate_approval_id() -> str:
    """HITL 没有显式 approval_id 时兜底生成。"""
    return f"appr-{uuid.uuid4().hex[:12]}"


def _session_id_from_run_state(run_state: object) -> str:
    """5.2：从 run_state 取 session_id。

    run_state 是 pandaren.engine.models.RunState dataclass，engine 必发 session_id 字段。
    Converter 不支持 dict 形态（5.2 协议下 engine 永远传 dataclass）。
    """
    if run_state is None:
        return ""
    return str(getattr(run_state, "session_id", "") or "")


def _extract_all_questions(questions_json: object) -> list[dict]:
    """从 ask_user 的 questions_json 中提取全部问题。

    questions_json 是 JSON 字符串（ask_user 工具的 input_schema 声明为 string），
    形如：
        [
          {
            "question": "今天天气怎么样？",
            "header": "天气",
            "options": [{"label": "好", "description": "..."}, ...],
            "multiSelect": false
          },
          ...
        ]

    返回 list[dict]，每个 dict 含 question/header/options/multiSelect。
    解析失败时返回 []，不抛异常。
    """
    if not questions_json:
        return []

    if isinstance(questions_json, list):
        raw_list = questions_json
    elif isinstance(questions_json, dict):
        raw_list = [questions_json]
    elif isinstance(questions_json, str):
        try:
            parsed = json.loads(questions_json)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(
                "INTERACTION_REQUEST: questions_json 不是合法 JSON 字符串: %s",
                e,
            )
            return []
        if not isinstance(parsed, list):
            logger.warning(
                "INTERACTION_REQUEST: questions_json 解析后不是 list，实际=%s",
                type(parsed).__name__,
            )
            return []
        raw_list = parsed
    else:
        logger.warning(
            "INTERACTION_REQUEST: questions_json 类型异常: %s",
            type(questions_json).__name__,
        )
        return []

    if not raw_list:
        return []

    result: list[dict] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue

        question_text = str(item.get("question", "") or "")
        header = str(item.get("header", "") or "")
        multi_select = bool(item.get("multiSelect", False))

        raw_options = item.get("options") or []
        if not isinstance(raw_options, list):
            raw_options = []

        options: list[dict] = []
        for opt in raw_options:
            if isinstance(opt, dict):
                options.append({
                    "label":       str(opt.get("label", "") or ""),
                    "description": str(opt.get("description", "") or ""),
                })
            elif isinstance(opt, str):
                options.append({"label": opt, "description": ""})

        result.append({
            "question":    question_text,
            "header":      header,
            "options":     options,
            "multiSelect": multi_select,
        })

    return result
