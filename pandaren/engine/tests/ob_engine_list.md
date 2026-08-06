测试点清单：pandaren/engine/ (Mock)
Section 1: StepCounter — 不可变保护 & 边界逻辑
#	测试点	Mock 方式	验证目标
1	max_steps <= 0 时构造抛 ValueError	无（直接构造）	ValueError raised
2	max_steps = 0 抛 ValueError（边界值）	无	ValueError raised
3	正常构造 max_steps=1 不抛异常	无	无异常，count==0
4	__setattr__ 直接赋值抛 PermissionError	无	PermissionError raised
5	__delattr__ 删除字段抛 PermissionError	无	PermissionError raised
6	increment() 未到上限返回 True	无	result == True
7	increment() 到达上限（count==max）时返回 False	无	result == False
8	increment() 超过上限后仍可调用（只增不停），返回 False	无	result == False
9	remaining 在用完后为 0（不为负）	无	remaining >= 0
10	increment() 通过 object.__setattr__ 内部路径绕过保护更新 _count	无	调用后 count 递增
Section 2: OutputParser — 3 条解析分支
#	测试点	Mock 方式	验证目标
11	response 含 tool_calls → is_final=False	MagicMock response	parsed.is_final == False
12	response 含 tool_calls → tool_calls 字段回填	MagicMock response	parsed.tool_calls == response.tool_calls
13	response 无 tool_calls，content 非空 → is_final=True, is_empty=False	MagicMock response	两字段正确
14	response 无 tool_calls，content 为空/None → is_final=True, is_empty=True	MagicMock response	is_empty == True
Section 3: MessageBuilder — classmethod 纯函数
#	测试点	Mock 方式	验证目标
15	三参数全 None/[] → build_static_context_str 返回 None	无（纯函数）	result is None
16	只传 deferred_tool_summaries → 返回包含 <available_tools> 的字符串	无	"<available_tools>" in result
17	只传 skill_summaries → 返回含 <available_skills>	无	"<available_skills>" in result
18	只传 agent_summaries → 返回含 <available_agents>	无	"<available_agents>" in result
19	recall_text=None → build_dynamic_reminder 返回 None	无	result is None
20	recall_text 有内容 → 返回含 <system-reminder> 的字符串	无	"<system-reminder>" in result
21	build() 中 static_context_str 被追加到 system 消息末尾	patch.object MessageBuilder	system 消息内容被修改
22	build() 中 dynamic_reminder 被追加为独立 user 消息	无	messages 末尾多一条 role==user
23	build() 不传 static_context_str 时 system 消息保持原样	无	内容不变
Section 4: AgentLoop.init — 前缀缓存 & 冻结保护
#	测试点	Mock 方式	验证目标
24	hooks=None 时自动使用 DefaultLoopHooks	patch DefaultLoopHooks	_hooks 是 DefaultLoopHooks 实例
25	context_window_budget 为 None 时不触发 logger.warning	patch pandaren.engine.loop.logger	warning.called == False
26	system_prompt 本身已超 system_prompt_tokens → logger.warning + _static_context_str = None	patch logger + mock budget	warning.called == True，_static_context_str is None
27	static_context_tokens > available → logger.warning + 截断	patch logger + mock budget	warning.called == True，len(_static_context_str) <= max_chars
28	skill_registry is None → 不调用 build_skill_summaries	MagicMock skill_registry	build_skill_summaries 不被调用
29	agent_registry 有值 → 调用 build_agent_summaries(exclude_agent_id=...)	MagicMock agent_registry	被调用且 exclude_agent_id 正确
Section 5: AgentLoop.setattr — 冻结保护
#	测试点	Mock 方式	验证目标
30	初始化前对冻结字段赋值不抛异常（init 内部流程）	无	构造成功
31	初始化完成后对 _identity 赋值抛 AttributeError	无	AttributeError raised
32	初始化完成后对 _llm_client 赋值抛 AttributeError	无	AttributeError raised
33	初始化完成后对非冻结字段（_cancelled）赋值正常	无	赋值成功，cancel() 可用
34	cancel() 设置 _cancelled=True	无	loop._cancelled == True
Section 6: RunCoreMixin._safe_hook — 异常抑制 & 日志
#	测试点	Mock 方式	验证目标
35	hook 方法抛异常 → 不传播，logger.warning 被调用	MagicMock hook, patch logger	无异常传播，warning.called == True
36	hook 方法不存在（AttributeError）→ 不抛异常，logger.warning 被调用	MagicMock(spec=...) + patch logger	无异常
37	hook 方法正常执行 → 不触发 logger.warning	MagicMock hook, patch logger	warning.called == False
38	_safe_hook 调用 on_run_start 时传递正确参数	MagicMock hook	on_run_start.call_args 正确
Section 7: RunCoreMixin.run() — O3 外层兜底
#	测试点	Mock 方式	验证目标
39	generator 未发 RUN_END 就结束 → logger.error + 返回 AgentResult(success=False)	patch _run_stream_core 返回空 async-gen, patch logger	error.called == True，返回值 success==False
40	generator 抛 Exception → logger.error + 返回 AgentResult(success=False)	patch _run_stream_core side_effect=Exception, patch logger	error.called == True，返回值 success==False
41	正常路径（RUN_END 被发出）→ 不触发 logger.error，返回 success=True result	patch _run_stream_core 返回 RUN_END event	error.called == False，success==True
Section 8: _run_stream_core — 入口校验 & 取消 & 终止路径
#	测试点	Mock 方式	验证目标
42	session_id 为空串 → 直接抛 ValueError（不进入 try）	无（直接调用）	ValueError raised
43	user_id 为空串 → 直接抛 ValueError	无	ValueError raised
44	resume_state.session_id != session_id → PermissionError（propagates）	MagicMock RunState	PermissionError raised
45	resume_state.user_id != user_id → PermissionError	MagicMock RunState	PermissionError raised
46	hitl_decision="reject_and_halt" → on_halt 钩子被调用	patch _safe_hook + mock HITL	on_halt 被调用
47	_cancelled=True 在步骤开始前被检测 → CANCELLED 终止，on_halt 被调用	patch _safe_hook, set _cancelled	on_halt.called == True
48	LLM LLMRateLimitError 耗尽重试 → LLM_ERROR 终止，logger.warning 被调用	patch LLM call, patch logger	warning.called == True
49	LLMAuthError → 立即 LLM_ERROR 终止（无重试）	patch LLM call	终止，on_halt 被调用
50	asyncio.TimeoutError 在工具执行时 → STEP_TIMEOUT 终止	patch tool exec, patch asyncio.wait_for	STEP_TIMEOUT terminal reason
51	工具执行结果 halt=True → TOOL_HALT 终止，on_halt 被调用	MagicMock tool result	on_halt.called == True
52	连续失败 3 次 → CIRCUIT_BREAKER 终止	patch _run_stream_core inner, simulate failures	CIRCUIT_BREAKER terminal reason
53	consecutive_permission_denied_rounds >= 3 → PERMISSION_EXHAUSTED 终止	patch check_permission always deny	PERMISSION_EXHAUSTED terminal reason
54	loop_correction_count >= 3（循环检测） → LLM_LOOP_DETECTED 终止，logger.warning 被调用	注入重复 tool_calls	LLM_LOOP_DETECTED terminal reason
55	for 循环耗尽 max_steps → MAX_STEPS_EXCEEDED 终止	设 max_steps=1，不停 LLM → tool 循环	MAX_STEPS_EXCEEDED terminal reason
56	AuditWriteError 在步骤内 → 传播到外层 AUDIT_FAILURE 终止	patch audit_log.write_sync raise AuditWriteError	AUDIT_FAILURE terminal reason
57	on_run_start hook 在 run 开始时被调用	MagicMock hooks, patch _safe_hook	on_run_start 被调用
58	on_run_end hook 在 finally 中被调用	MagicMock hooks, patch _safe_hook	on_run_end 被调用
59	recall text 超出预算 → logger.warning 被触发	patch logger + mock budget	warning.called == True
60	工具不在 registry → logger.warning 被触发（permission denied）	patch tool_registry	warning.called == True
统计
总计：60 个测试点
logger 验证：24 个（Section 4: #25–27; Section 6: #35–37; Section 7: #39–40; Section 8: #48, 54, 59, 60 等）
回调验证：10 个（Section 6 全部; Section 8: #46–47, 51, 57–58）
内部方法替换（patch.object）：8 个（Section 3: #21; Section 7: #39–41; Section 8: #44–48）
异常注入：12 个（Section 1: #1–2, 4–5; Section 5: #31–32; Section 7: #40; Section 8: #42–44, 49, 56）
边界逻辑：6 个（Section 1: #6–10; Section 2: #11–14）
