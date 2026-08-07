# -*- coding: utf-8 -*-
"""生成用户真实场景 fixture：SKILL.md 5 处修改（git 原始 LF 转 CRLF vs 当前磁盘 CRLF）"""
import json
import os

base = os.path.dirname(os.path.abspath(__file__))
repo = os.path.abspath(os.path.join(base, "..", "..", "..", ".."))
orig_lf = open(os.path.join(os.environ["TEMP"], "skill_orig.md"), "r", encoding="utf-8", newline="").read()
disk = open(os.path.join(repo, ".pandapal", "skills", "user", "code-design", "SKILL.md"), "r", encoding="utf-8", newline="").read()

# git 导出为 LF；模拟磁盘原始（Windows autocrlf 检出为 CRLF）
orig_crlf = orig_lf.replace("\r\n", "\n").replace("\n", "\r\n")

print("orig LF lines:", orig_lf.count("\n"), "orig CRLF count after conv:", orig_crlf.count("\r\n"))
print("disk CRLF:", disk.count("\r\n"), "disk bytes:", len(disk.encode("utf-8")))

with open(os.path.join(base, "skill_md_real.json"), "w", encoding="utf-8") as f:
    json.dump({"language": "markdown", "original": orig_crlf, "current": disk}, f, ensure_ascii=False)
print("written skill_md_real.json")
