"""pandapal.broadcast.policy — 事件类别与渲染提示。

★ 分发策略已移到渠道侧（2026-06）：
  事件不再携带"目标范围"属性。事件只有"类别"（EventCategory）与
  "渲染提示"（EVENT_RENDERING_HINTS）；发给哪些渠道由渠道自我声明
  （ChannelDispatchPolicy，见 channel_registry.py）+ 恒定规则
  （R0指名 / R1 echo 不回源 / R2 回复恒达会话主）决定。

保留内容：
  - EventCategory:           事件"形态"（流式 vs 离散），Transport 渲染依据
  - EVENT_CATEGORY:          45 种事件的类别总表
  - EVENT_RENDERING_HINTS:   各事件类型在不同渠道的渲染说明（心智模型对齐）
"""

from __future__ import annotations

from enum import Enum

from pandapal.events.normalized import EventType


# ── 维度 1：EventCategory（事件性质）───────────────────────────────
class EventCategory(str, Enum):
    """事件类别：描述事件本身的"形态"。

    ★ 这个属性决定 Transport 怎么"渲染"这个事件。
    ★ 与渠道分发策略（ChannelDispatchPolicy）是正交维度。
    """

    STREAMING = "streaming"   # 增量事件（多 token 累积），如 LLM_TOKEN
    DISCRETE  = "discrete"    # 一次性事件，如 HITL_REQUEST / TOOL_END / REPLY_END


# ── 45 种事件的"性质"分类（每事件唯一）───────────────────────────
#
# ★ 这是方案的"事件性质总表"——决定 Transport 怎么渲染
# ★ 归类原则：
#     - STREAMING: 必须是"可切分为多次增量发送"的事件（仅 LLM_TOKEN/REASONING_TOKEN，2 种）
#     - DISCRETE:  所有"一次性通知"事件（其余全部）

EVENT_CATEGORY: dict[EventType, EventCategory] = {
    # ── 流式事件：只有 2 种 ──
    EventType.LLM_TOKEN:        EventCategory.STREAMING,
    EventType.REASONING_TOKEN:  EventCategory.STREAMING,

    # ── 离散事件 ──
    EventType.REPLY_START:        EventCategory.DISCRETE,
    EventType.REPLY_END:          EventCategory.DISCRETE,
    EventType.RUN_START:          EventCategory.DISCRETE,
    EventType.RUN_END:            EventCategory.DISCRETE,
    EventType.TOOL_START:         EventCategory.DISCRETE,
    EventType.TOOL_END:           EventCategory.DISCRETE,
    EventType.HITL_REQUEST:       EventCategory.DISCRETE,
    EventType.INTERACTION_REQUEST: EventCategory.DISCRETE,
    EventType.PERMISSION_DENIED:  EventCategory.DISCRETE,
    EventType.AGENT_HALTED:       EventCategory.DISCRETE,
    EventType.ERROR:              EventCategory.DISCRETE,
    EventType.APPROVAL_RESULT:    EventCategory.DISCRETE,
    EventType.AGENT_REPLY:        EventCategory.DISCRETE,
    EventType.USER_INPUT_ECHO:    EventCategory.DISCRETE,
    EventType.TASK_NOTIFICATION:  EventCategory.DISCRETE,
    EventType.AGENT_TASK_EVENT:   EventCategory.DISCRETE,
    EventType.PLAN_APPROVAL_REQUEST: EventCategory.DISCRETE,
    EventType.QUICK_APP_DATA:       EventCategory.DISCRETE,
    EventType.SKILL_PROGRESS:       EventCategory.DISCRETE,
    EventType.SCHEDULED_TASK_LIST:  EventCategory.DISCRETE,
    EventType.SCHEDULED_TASK_CHANGED: EventCategory.DISCRETE,
    EventType.SKILL_LIST_RESULT:    EventCategory.DISCRETE,
    EventType.SKILL_GET_RESULT:     EventCategory.DISCRETE,
    EventType.SKILL_SAVED:          EventCategory.DISCRETE,
    EventType.SKILL_DELETED:        EventCategory.DISCRETE,
    EventType.SKILL_IMPORTED:       EventCategory.DISCRETE,
    EventType.SKILL_EXPORTED:       EventCategory.DISCRETE,
    EventType.SKILL_ACTIVATED:      EventCategory.DISCRETE,
    EventType.SKILL_CLEARED:        EventCategory.DISCRETE,
    EventType.SESSION_CONCURRENCY:  EventCategory.DISCRETE,
    EventType.SESSION_LIST:         EventCategory.DISCRETE,
    EventType.SESSION_SWITCHED:     EventCategory.DISCRETE,
    EventType.SESSION_UPDATED:      EventCategory.DISCRETE,
    EventType.SESSION_DELETED:      EventCategory.DISCRETE,
    EventType.SESSION_GROUP_LIST:   EventCategory.DISCRETE,
    EventType.SESSION_HISTORY_LIST: EventCategory.DISCRETE,
    EventType.SEARCH_RESULT:        EventCategory.DISCRETE,
    EventType.DASHBOARD_DATA:       EventCategory.DISCRETE,
    EventType.BUDGET_STATUS:        EventCategory.DISCRETE,
    EventType.MODEL_LIST:           EventCategory.DISCRETE,
    EventType.CREDENTIALS_LIST:     EventCategory.DISCRETE,
    EventType.CREDENTIALS_SAVED:    EventCategory.DISCRETE,
    EventType.CREDENTIALS_VERIFIED: EventCategory.DISCRETE,
    EventType.CREDENTIALS_STATUS:   EventCategory.DISCRETE,
}


# ── 各事件类型在不同渠道的渲染（Transport 自决）──────
# 此表用于"前端 / 渠道"对齐心智模型；具体实现在各 Transport
EVENT_RENDERING_HINTS: dict[EventType, dict[str, str]] = {
    EventType.LLM_TOKEN:           {"ipc": "实时累加 token",          "wecom": "累积到 REPLY_END 一次性推（Transport 内部 buffer）"},
    EventType.REASONING_TOKEN:     {"ipc": "实时显示推理",            "wecom": "累积或丢弃"},
    EventType.REPLY_START:         {"ipc": "进入流式 UI 状态",         "wecom": "（无操作）"},
    EventType.REPLY_END:           {"ipc": "关闭 streaming bubble",   "wecom": "flush buffer → 推送完整文本"},
    EventType.TOOL_START:          {"ipc": "显示工具调用卡片",         "wecom": "🔧 调用 {tool_name}…"},
    EventType.TOOL_END:            {"ipc": "可折叠结果卡片（含完整内容）", "wecom": "✓ {tool_name} 完成（按大小智能截断）"},
    EventType.HITL_REQUEST:        {"ipc": "弹窗",                    "wecom": "模板卡片"},
    EventType.PLAN_APPROVAL_REQUEST: {"ipc": "Plan Mode 审批弹窗",      "wecom": "Plan 方案文本"},
    EventType.INTERACTION_REQUEST: {"ipc": "内联问卷",                "wecom": "adaptive_card"},
    EventType.ERROR:               {"ipc": "错误提示 UI",             "wecom": "错误文本"},
    EventType.USER_INPUT_ECHO:     {"ipc": "（跳过，自己发的）",      "wecom": "（跳过）"},
    EventType.AGENT_REPLY:         {"ipc": "完整文本",                "wecom": "完整文本"},
    EventType.APPROVAL_RESULT:     {"ipc": "UI 状态更新",             "wecom": "✅ 已批准 / ❌ 已拒绝"},
    EventType.TASK_NOTIFICATION:   {"ipc": "通知 UI",                 "wecom": "文本通知"},
    EventType.PERMISSION_DENIED:   {"ipc": "错误提示",                "wecom": "错误文本"},
    EventType.AGENT_HALTED:        {"ipc": "停止状态",                "wecom": "Agent 已停止"},
    EventType.RUN_START:           {"ipc": "标记 run 开始",            "wecom": "（无操作）"},
    EventType.RUN_END:             {"ipc": "标记 run 结束",            "wecom": "（无操作）"},
    EventType.AGENT_TASK_EVENT:    {"ipc": "任务面板更新",            "wecom": "（简版文本）"},
    EventType.QUICK_APP_DATA:       {"ipc": "快应用数据推送",          "wecom": "（简版文本）"},
    EventType.SKILL_PROGRESS:       {"ipc": "对话内技能进度块",        "wecom": "（简版文本）"},
    EventType.SCHEDULED_TASK_LIST:  {"ipc": "定时任务列表推送",          "wecom": "（简版文本）"},
    EventType.SCHEDULED_TASK_CHANGED: {"ipc": "定时任务增量变更推送",      "wecom": "（简版文本）"},

    #以下事件wecom不支持，无响应即可
    EventType.SKILL_LIST_RESULT:    {"ipc": "Skill 摘要列表响应",        "wecom": "（跳过）"},
    EventType.SKILL_GET_RESULT:     {"ipc": "Skill 详情响应",            "wecom": "（跳过）"},
    EventType.SKILL_SAVED:          {"ipc": "Skill 保存确认",            "wecom": "（跳过）"},
    EventType.SKILL_DELETED:        {"ipc": "Skill 删除确认",            "wecom": "（跳过）"},
    EventType.SKILL_IMPORTED:       {"ipc": "Skill 导入确认",            "wecom": "（跳过）"},
    EventType.SKILL_EXPORTED:       {"ipc": "Skill 导出确认",            "wecom": "（跳过）"},
    EventType.SKILL_ACTIVATED:      {"ipc": "Skill 激活提示",            "wecom": "（跳过）"},
    EventType.SKILL_CLEARED:        {"ipc": "Skill 清除提示",            "wecom": "（跳过）"},
    EventType.SESSION_CONCURRENCY:  {"ipc": "并发池排队状态",            "wecom": "（跳过）"},
    EventType.SESSION_LIST:         {"ipc": "会话列表",                  "wecom": "（跳过）"},
    EventType.SESSION_SWITCHED:     {"ipc": "切换应答 + context_status", "wecom": "（跳过）"},
    EventType.SESSION_UPDATED:      {"ipc": "会话元数据增量变更",        "wecom": "（跳过）"},
    EventType.SESSION_DELETED:      {"ipc": "会话删除确认",              "wecom": "（跳过）"},
    EventType.SESSION_GROUP_LIST:   {"ipc": "会话分组列表",              "wecom": "（跳过）"},
    EventType.SESSION_HISTORY_LIST: {"ipc": "历史消息回补",              "wecom": "（跳过）"},
    EventType.SEARCH_RESULT:        {"ipc": "搜索结果响应",              "wecom": "（跳过）"},
    EventType.DASHBOARD_DATA:       {"ipc": "看板快照响应",              "wecom": "（跳过）"},
    EventType.BUDGET_STATUS:        {"ipc": "每 provider 额度视图",      "wecom": "（跳过）"},
    EventType.MODEL_LIST:           {"ipc": "可选模型清单 + default",    "wecom": "（跳过）"},
    EventType.CREDENTIALS_LIST:     {"ipc": "已有凭据列表（脱敏）",      "wecom": "（跳过）"},
    EventType.CREDENTIALS_SAVED:    {"ipc": "凭据保存结果确认",          "wecom": "（跳过）"},
    EventType.CREDENTIALS_VERIFIED: {"ipc": "凭据连通性校验结果",        "wecom": "（跳过）"},
    EventType.CREDENTIALS_STATUS:   {"ipc": "凭据门禁配置状态",          "wecom": "（跳过）"},
}
