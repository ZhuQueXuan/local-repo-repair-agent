"""Verify predeclared experiment inputs before transport and after a run."""
import hashlib
import json
from pathlib import Path


def verify_freeze(root: Path, manifest_name: str, *, expected_digest=None):
    root = root.resolve()

    def inside(relative):
        path = (root / relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(root):
            raise ValueError("Frozen input must stay inside the project.")
        return path

    manifest_path = inside(manifest_name)
    raw = manifest_path.read_bytes()
    identity = hashlib.sha256(raw).hexdigest()
    if expected_digest is not None and identity != expected_digest:
        raise RuntimeError("Freeze manifest changed during the run.")
    manifest = json.loads(raw)
    if manifest.get("version") != 1 or not manifest.get("files_sha256"):
        raise ValueError("Expected a nonempty version 1 freeze manifest.")
    for name, expected in manifest["files_sha256"].items():
        path = inside(name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Frozen experiment input changed: " + name)
    return {"manifest": manifest_name, "sha256": identity,
            "verified_files": len(manifest["files_sha256"])}
