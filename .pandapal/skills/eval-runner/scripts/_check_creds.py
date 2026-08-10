import re
from pathlib import Path

appdata = Path.home() / "AppData" / "Roaming"
found = []
for app_dir in ("com.pandapal.desktop", "com.pandapal.app"):
    base = appdata / app_dir / "users"
    if base.exists():
        files = sorted(
            base.glob("*/credentials/llm_credentials.toml"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        found.extend(files)
if found:
    f = found[0]
    txt = f.read_text(encoding="utf-8")
    print("credential file:", f)
    print("models:", re.findall(r'model_id\s*=\s*"([^"]+)"', txt))
    m = re.search(r'default_model_id\s*=\s*"([^"]+)"', txt)
    print("default_model_id:", m.group(1) if m else None)
else:
    print("no credential file found")
