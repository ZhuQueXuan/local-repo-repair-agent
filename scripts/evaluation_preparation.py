"""Offline qualification of a public-task comparison input packet.

This tool cannot create runs, construct model clients or write a fee ledger.
Frozen preparation packets are explicitly NOT launch manifests. Held-out here
means omitted from public workspaces/prompts, not protected by an OS sandbox.
"""
import argparse
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from collaboration_protocol import canonical_bytes, sha256, ProtocolError
from review_tools import _safe_file, _read
from collaboration_panel import checked_bytes
from task_workspace import ROOT, package_root

DEFAULT_PACKET = ROOT / "evaluation/multi_agent_20261025"


def read_json(base, relative):
    return json.loads(_read(base, relative))


def validate_plan(plan):
    if plan.get("schema_version") != 1 or plan.get("state") != "offline_prepared_not_launchable" or plan.get("paid_launch_authorized") is not False:
        raise ProtocolError("not_an_offline_preparation_plan")
    tasks = plan.get("tasks", [])
    ids = [row["task"] for row in tasks]
    if len(ids) != 6 or len(set(ids)) != 6 or len({row["repository"] for row in tasks}) < 3:
        raise ProtocolError("expected_six_unique_tasks_three_repositories")
    slots = plan.get("slots", [])
    if len(slots) != 15 or len({s["identity"] for s in slots}) != 15 or any(s["task"] not in ids or s["variant"] not in {"S", "D", "Splus"} or s["repetition"] != 1 for s in slots):
        raise ProtocolError("invalid_predeclared_slots")
    for row in tasks:
        variants = sorted(s["variant"] for s in slots if s["task"] == row["task"])
        if variants != sorted(["S", "D"] + (["Splus"] if row["splus_selected"] else [])):
            raise ProtocolError("slot_task_variant_mismatch")
    if sum(bool(row["splus_selected"]) for row in tasks) != 3 or len(plan.get("launch_blockers", [])) < 3:
        raise ProtocolError("missing_preparation_limitations")
    return plan


def public_manifest(packet, task):
    # Never accept caller-selected filesystem paths as task identifiers.
    if not task or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in task):
        raise ProtocolError("invalid_preparation_task")
    base = Path(packet) / "public" / task
    manifest = read_json(base, "manifest.json")
    if manifest.get("task_id") != task or manifest.get("scope") != "new_public_issue_preparation_only":
        raise ProtocolError("preparation_task_identity_mismatch")
    for relative, expected in manifest["source_files_sha256"].items():
        if sha256(checked_bytes(base / "repo", relative)) != expected:
            raise ProtocolError("preparation_source_changed")
    if set(manifest["task_files_sha256"]) != {"issue.md", "checks/test_behavior.py"}:
        raise ProtocolError("unexpected_public_task_files")
    for relative, expected in manifest["task_files_sha256"].items():
        if sha256(_read(base, relative)) != expected:
            raise ProtocolError("preparation_public_check_changed")
    return base, manifest


def copy_public(packet, task, output):
    base, manifest = public_manifest(packet, task)
    output = Path(output)
    if not output.resolve().is_relative_to((ROOT / "temp").resolve()):
        raise ValueError("Qualification copies require project temp")
    output.mkdir(parents=True, exist_ok=False)
    for relative in manifest["source_files_sha256"]:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(checked_bytes(base / "repo", relative))
    (output / "temp").mkdir(exist_ok=True)
    (output / "task_checks").mkdir()
    (output / "task_checks/test_behavior.py").write_bytes(_read(base, "checks/test_behavior.py"))
    (output / "TASK.md").write_bytes(_read(base, "issue.md"))
    return manifest


def apply_reference(repo, manifest, edits):
    """Trusted, exact literal adaptation; never execute a suggested command."""
    if not edits:
        raise ProtocolError("missing_reference_edits")
    for edit in edits:
        relative = edit["path"]
        if relative not in manifest["source_files_sha256"] or not relative.startswith(manifest["source_prefix"]):
            raise ProtocolError("reference_outside_implementation")
        path = _safe_file(repo, relative)
        raw = path.read_bytes()
        newline = b"\r\n" if b"\r\n" in raw else b"\n"
        text = raw.replace(b"\r\n", b"\n").decode()
        if not edit["old"] or text.count(edit["old"]) != edit["expected_occurrences"]:
            raise ProtocolError("reference_context_mismatch")
        path.write_bytes(text.replace(edit["old"], edit["new"]).encode().replace(b"\n", newline))


def check_qualification(result):
    before, after = result["baseline"], result["reference"]
    for partition in ("public", "withheld"):
        if before[partition]["successful"] or not before[partition]["failed_test_methods"]:
            raise ProtocolError("baseline_did_not_reproduce_" + partition)
        if not after[partition]["successful"] or after[partition]["passed"] != after[partition]["tests_run"]:
            raise ProtocolError("reference_did_not_pass_" + partition)
    if not before["upstream"]["successful"] or not after["upstream"]["successful"]:
        raise ProtocolError("upstream_subset_not_clean")
    if not result["protected_inputs_unchanged"]:
        raise ProtocolError("qualification_inputs_changed")
    return result


def qualify(packet, task, output=None):
    packet = Path(packet)
    frozen = (packet / "freeze.json").exists()
    if frozen:
        verify_packet(packet)
    output = output or ROOT / "temp/evaluation_preparation" / (task + "-" + uuid.uuid4().hex)
    output = Path(output)
    if not output.resolve().is_relative_to((ROOT / "temp").resolve()):
        raise ValueError("Qualification copies require project temp")
    output.mkdir(parents=True, exist_ok=False)
    base, manifest = public_manifest(packet, task)
    authority = packet / "evaluator" / task
    check_raw = _read(authority, "test_behavior.py")
    reference_raw = _read(authority, "reference.json")
    versions = {}
    for label in ("baseline", "reference"):
        directory = output / label
        repo = directory / "repo"
        copy_public(packet, task, repo)
        if label == "reference":
            apply_reference(repo, manifest, json.loads(reference_raw)["edits"])
        env = {k:v for k,v in os.environ.items() if k.upper() in {"PATH","SYSTEMROOT","WINDIR","COMSPEC","PATHEXT"}}
        env.update(TEMP=str(repo / "temp"), TMP=str(repo / "temp"), PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        command = [sys.executable, "-B", "-I", str(Path(__file__).resolve()), "--child", str(packet), task, str(repo), str(directory)]
        with (directory / "console.log").open("wb") as stream:
            process = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
        if process.returncode or not (directory / "results.json").is_file():
            raise ProtocolError("qualification_child_unavailable")
        versions[label] = read_json(directory, "results.json")
        # All baseline source/old tests remain exact; reference edits alone may
        # change implementation, never task or evaluator inputs.
        for relative, expected in manifest["source_files_sha256"].items():
            if label == "baseline" or not relative.startswith(manifest["source_prefix"]):
                if sha256(checked_bytes(repo, relative)) != expected:
                    raise ProtocolError("qualification_protected_source_changed")
        if _read(repo, "TASK.md") != _read(base, "issue.md") or _read(repo, "task_checks/test_behavior.py") != _read(base, "checks/test_behavior.py"):
            raise ProtocolError("qualification_public_inputs_changed")
    result = {"task":task, **versions, "protected_inputs_unchanged": _read(authority,"test_behavior.py")==check_raw and _read(authority,"reference.json")==reference_raw,
        "scope":"Trusted developer baseline/reference qualification only, not Agent results. No withheld results enter a running agent.",
        "model_calls":0, "packet_frozen":frozen, "output":str(output)}
    check_qualification(result)
    if frozen:
        verify_packet(packet)
    (output / "qualification.json").write_bytes(canonical_bytes(result))
    return result


def child(packet, task, repo, directory):
    import socket
    def denied(*args, **kwargs):
        raise RuntimeError("Network forbidden during offline qualification")
    socket.socket.connect = socket.socket.connect_ex = socket.getaddrinfo = denied
    packet, repo, directory = Path(packet), Path(repo), Path(directory)
    _, manifest = public_manifest(packet, task)
    sys.path.insert(0, str(package_root(repo, manifest)))
    package = __import__(manifest["package"])
    if not Path(package.__file__).resolve().is_relative_to(repo.resolve()):
        raise ProtocolError("qualification_import_escaped_copy")
    from check_task import run_suite, pytest_suite
    import unittest
    results = {}
    for label, path in (("public",repo / "task_checks/test_behavior.py"), ("withheld",packet / "evaluator" / task / "test_behavior.py")):
        spec = importlib.util.spec_from_file_location("qualification_" + label, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        results[label] = run_suite(unittest.defaultTestLoader.loadTestsFromModule(module))
    # pytest runs in another process; install its own guard instead of assuming
    # this interpreter's socket monkeypatch would be inherited.
    guard = repo / "temp/qualification_network_guard.py"
    guard.write_text("import socket\ndef denied(*a, **k): raise RuntimeError('Offline qualification forbids network')\ndef pytest_configure(config):\n socket.socket.connect=socket.socket.connect_ex=socket.getaddrinfo=denied\n", encoding="utf-8")
    results["upstream"] = pytest_suite(repo, manifest["upstream_target"], directory, "upstream",
        plugins=(*manifest.get("pytest_plugins",()),"temp.qualification_network_guard"), import_root=manifest.get("import_root","."))
    (directory / "results.json").write_bytes(canonical_bytes(results))


def verify_packet(packet=DEFAULT_PACKET):
    packet = Path(packet)
    plan = validate_plan(read_json(packet, "plan.json"))
    if (packet / "freeze.json").exists():
        from evaluation_freeze import verify_freeze
        declared = read_json(packet, "freeze.json").get("files_sha256", {})
        members = {p.relative_to(ROOT).as_posix() for p in packet.rglob("*")
                   if p.is_file() and p.name != "freeze.json" and "temp" not in p.relative_to(packet).parts}
        if not members <= set(declared):
            raise ProtocolError("freeze_omits_packet_inputs")
        frozen = verify_freeze(ROOT, (packet / "freeze.json").relative_to(ROOT).as_posix())
    else:
        raise ProtocolError("packet_not_frozen")
    for task in plan["tasks"]:
        base, manifest = public_manifest(packet, task["task"])
        expected = {"repo/"+p for p in manifest["source_files_sha256"]} | set(manifest["task_files_sha256"]) | {"manifest.json"}
        actual = {p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()}
        if expected != actual:
            raise ProtocolError("unexpected_public_packet_files")
        # Evaluator authority never appears in the public file projection.
        for path in base.rglob("*"):
            if path.is_file() and (path.is_symlink() or "reference" in path.name or "withheld" in path.parts or "evaluator" in path.parts):
                raise ProtocolError("evaluator_material_in_public_packet")
        if any(p.startswith(("evaluator/","reference/")) for p in manifest["source_files_sha256"]):
            raise ProtocolError("evaluator_material_in_source_manifest")
    return {"state":plan["state"], "tasks":len(plan["tasks"]), "planned_slots":len(plan["slots"]), "frozen":frozen,
            "launchable":False,"model_calls":0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify",action="store_true")
    parser.add_argument("--qualify",metavar="TASK")
    parser.add_argument("--child",nargs=4,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.child:
        return child(*args.child)
    if args.qualify:
        result=qualify(DEFAULT_PACKET,args.qualify)
        print(json.dumps({"task":result["task"],"output":result["output"],"model_calls":0}))
    else:
        print(json.dumps(verify_packet(),ensure_ascii=False))


if __name__ == "__main__":main()
