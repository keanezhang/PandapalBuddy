"""pandaren/skill/exceptions.py — Skill 层异常定义"""


class SkillRegistrationError(Exception):
    """Skill 注册失败（必填字段缺失、content 为空等）。"""
