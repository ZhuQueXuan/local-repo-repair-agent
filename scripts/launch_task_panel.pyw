"""Double-click entry with bounded readiness and visible startup failures."""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser

from release_info import release_identity

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8765"


def health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(URL + "/api/health", timeout=1) as response:
            value = json.loads(response.read(8192))
            return value if isinstance(value, dict) else None
    except urllib.error.HTTPError:
        return {"product": "other-service"}
    except (OSError, ValueError):
        return None


def compatible(value, expected):
    return value is not None and value.get("product") == "mini-swe-task-panel" and all(
        value.get(key) == expected[key] for key in ("version", "code_sha256"))


def show_error(message):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "仓库修复 Agent · 启动未完成", 0x10)
    else:
        print(message)


def launch():
    expected = release_identity()
    existing = health()
    if existing is not None:
        if compatible(existing, expected):
            webbrowser.open(URL)
        else:
            show_error("8765端口已有其他版本或服务。请先在原面板等待任务结束，再点击停止服务，然后重新双击启动。\n未终止已有进程或任务。")
        return
    folder = ROOT / "temp/task_service_launcher"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".log")
    with path.open("x", encoding="utf-8") as log:
        try:
            process = subprocess.Popen([str(ROOT / ".venv/Scripts/python.exe"), "-B", str(ROOT / "scripts/task_service.py")],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONUTF8="1"),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as error:
            log.write(type(error).__name__ + "\n")
            show_error("无法启动项目环境。请查看 README.md。\n日志：" + str(path))
            return
        expires = time.monotonic() + 45
        while time.monotonic() < expires:
            current = health()
            if compatible(current, expected):
                webbrowser.open(URL)
                return
            if current is not None or process.poll() is not None:
                break
            time.sleep(.3)
    show_error("面板未在45秒内就绪或启动时退出。没有创建任务或发送模型请求。\n请检查8765端口、Python环境和日志，再重新双击。\n日志：" + str(path))


if __name__ == "__main__":
    try:
        launch()
    except Exception as error:
        show_error("启动检查失败：" + type(error).__name__ + "。请查看项目 README.md 与 temp/task_service_launcher 日志。")
