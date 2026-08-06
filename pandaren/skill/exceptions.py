"""pandaren/skill/exceptions.py — Skill 层异常定义"""


class SkillRegistrationError(Exception):
    """Skill 注册失败（必填字段缺失、content 为空等）。"""


class SkillScriptError(Exception):
    """Skill 脚本加载或执行失败。

    可能原因：
    - 脚本文件不存在
    - 路径遍历越界（安全拒绝）
    - 入口函数未找到
    - 模块中存在多个候选函数且未指定 entry_function
    - 函数签名不符合要求
    """
