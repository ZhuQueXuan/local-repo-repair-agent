"""Public version and startup source identity; no account or credential reads."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def release_identity():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    files = [ROOT / "VERSION", ROOT / "启动任务面板.cmd"]
    files += [p for p in (ROOT / "scripts").iterdir() if p.suffix in {".py", ".pyw", ".js", ".html", ".ps1"} and not p.name.startswith("test_")]
    files += list((ROOT / "configs").glob("*.json"))
    files += list((ROOT / "mini-swe-agent/src/minisweagent").rglob("*.py"))
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return {"version": version, "code_sha256": digest.hexdigest(), "scope": "本机固定Python任务；实验协作不保证修复成功"}
