"""pandaren/agent/exceptions.py — Agent 层异常定义"""


class SubAgentRegistrationError(Exception):
    """Agent 注册失败（agent_id 重复、必填字段缺失等）。"""
