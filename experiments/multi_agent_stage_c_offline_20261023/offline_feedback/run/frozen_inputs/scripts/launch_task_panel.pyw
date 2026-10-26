"""Double-click launcher: reuse an existing panel or start it hidden with logs."""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent.parent
url = "http://127.0.0.1:8765"
ready = False
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url + "/api/health", timeout=1) as response:
        ready = json.load(response).get("product") == "mini-swe-task-panel"
except Exception:
    pass
if ready:
    webbrowser.open(url)
else:
    folder = ROOT / "temp/task_service_launcher"
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    with (folder / (name + ".log")).open("w", encoding="utf-8") as log:
        subprocess.Popen([str(ROOT / ".venv/Scripts/python.exe"), "-B", str(ROOT / "scripts/task_service.py"), "--open"],
                         cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONUTF8="1"),
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
