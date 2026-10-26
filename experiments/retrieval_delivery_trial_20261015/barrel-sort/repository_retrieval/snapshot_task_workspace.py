"""Fixed task inputs and disposable, independent Git workspaces."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_root(repo: Path, manifest: dict) -> Path:
    """Resolve root-layout and src-layout imports without changing installed packages."""
    relative = Path(manifest.get("import_root", "."))
    resolved = (repo / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not resolved.is_relative_to(repo.resolve()) or not resolved.is_dir():
        raise ValueError("Package import root must be an existing directory in this checkout.")
    return resolved


def load_task(name: str) -> tuple[Path, dict]:
    task = ROOT / "tasks" / name
    if task.parent.resolve() != (ROOT / "tasks").resolve():
        raise ValueError("Expected one task directory name.")
    manifest = json.loads((task / "manifest.json").read_text(encoding="utf-8"))
    for relative, expected in manifest["source_files_sha256"].items():
        if digest(task / "repo" / relative) != expected:
            raise RuntimeError("Frozen task source changed: " + relative)
    for relative, expected in manifest["task_files_sha256"].items():
        if digest(task / relative) != expected:
            raise RuntimeError("Frozen task description/check changed: " + relative)
    return task, manifest


def prepare_task(name: str) -> tuple[Path, dict]:
    task, manifest = load_task(name)
    work = task / "temp" / ("run_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")) / "repo"
    work.mkdir(parents=True)
    for relative in manifest["source_files_sha256"]:
        destination = work / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(task / "repo" / relative, destination)
    (work / "temp").mkdir()
    shutil.copytree(task / "checks", work / "task_checks")
    shutil.copy2(task / "issue.md", work / "TASK.md")
    subprocess.run(["git", "init", "--quiet", str(work)], check=True, capture_output=True)
    # Explicit paths prevent runtime output from becoming baseline source.
    subprocess.run(["git", "-C", str(work), "add", "--", *manifest["source_files_sha256"], "TASK.md", "task_checks"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.name=Task Baseline", "-c", "user.email=baseline@local.invalid",
                    "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Frozen task baseline"],
                   check=True, capture_output=True)
    git_root = subprocess.run(["git", "-C", str(work), "rev-parse", "--show-toplevel"],
                             check=True, capture_output=True, text=True).stdout.strip()
    if Path(git_root).resolve() != work.resolve():
        raise RuntimeError("Task Git root mismatch.")
    return work, manifest


def changed_files(work: Path, manifest: dict) -> list[str]:
    changed = [relative for relative, expected in manifest["source_files_sha256"].items()
               if not (work / relative).is_file() or digest(work / relative) != expected]
    for path in work.rglob("*"):
        relative = path.relative_to(work)
        if path.is_file() and relative.parts[0] not in (".git", "temp", "task_checks") and relative.as_posix() != "TASK.md":
            if relative.as_posix() not in manifest["source_files_sha256"]:
                changed.append(relative.as_posix())
    return sorted(changed)
