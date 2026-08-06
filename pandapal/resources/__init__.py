"""pandapal/resources/ — 资源集中管理

目录结构:
    resources/
    ├── skills/
    │   ├── system/  系统自带 Skill（只读，SkillSource.PROJECT）
    │   └── user/    用户自定义 Skill（可 CRUD，SkillSource.USER）
    ├── agents/
    │   ├── system/  系统自带 Agent（只读）
    │   └── user/    用户自定义 Agent（可 CRUD）
    └── tools/       （永久封闭，不纳入 resources 体系）
"""
