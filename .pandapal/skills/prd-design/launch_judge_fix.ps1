$env:PYTHONIOENCODING = "utf-8"
Set-Location "C:\Users\keanezhang\PycharmProjects\pandapal_buddy\.pandapal\skills\eval-runner\scripts"
python judge.py "C:\Users\keanezhang\PycharmProjects\pandapal_buddy\.pandapal\skills\prd-design" --run-id run-2-isolated --only file-input-missing,injection-attempt,vague-input *> "C:\Users\keanezhang\PycharmProjects\pandapal_buddy\.pandapal\skills\prd-design\judge_fix.log"
