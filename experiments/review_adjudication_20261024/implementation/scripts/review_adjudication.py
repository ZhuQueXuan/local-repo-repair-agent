"""Offline finding adjudication from trusted, frozen checks, never model votes.

Only developer-side code registers probes and exact claim/check mappings. This
module is not a model tool, HTTP route, OS sandbox or natural-language judge.
"""
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid
from typing import Annotated, Literal

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pydantic import Field, StringConstraints, model_validator
from collaboration_protocol import (Message, Identifier, Digest, Text, ProtocolError,
                                    canonical_bytes, sha256, validate_review)
from review_tools import ROOT, _root, _read, _unlinked

TestID = Annotated[str, StringConstraints(pattern=r"^registered_probe\.[A-Za-z_][A-Za-z0-9_.]{0,140}$")]


class CheckSpec(Message):
    check_id: Identifier
    test_id: TestID
    requirement_ref: Identifier
    prior_visibility: Literal["already_reported", "not_previously_reported", "unknown"] = "unknown"


class ProbePlan(Message):
    plan_id: Identifier
    task_digest: Digest
    probe_sha256: Digest
    checks: Annotated[tuple[CheckSpec, ...], Field(min_length=1, max_length=30)]
    scope: Text

    @model_validator(mode="after")
    def distinct(self):
        if len({c.check_id for c in self.checks}) != len(self.checks) or len({c.test_id for c in self.checks}) != len(self.checks):
            raise ProtocolError("duplicate_probe_check")
        return self


class FindingBinding(Message):
    """Trusted exact mapping, not an interpretation supplied by the reviewer."""
    review_id: Identifier
    finding_id: Identifier
    finding_sha256: Digest
    claim_key: Identifier
    check_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=30)]
    coverage_scope: Text


class ProbeReceipt(Message):
    receipt_id: Identifier
    run_id: Identifier
    candidate_id: Identifier
    source_tree_digest: Digest
    task_digest: Digest
    baseline_digest: Digest
    plan_digest: Digest
    results_digest: Digest
    status: Literal["completed", "unavailable"]


class FindingDecision(Message):
    decision_id: Identifier
    run_id: Identifier
    review_id: Identifier
    finding_id: Identifier
    finding_sha256: Digest
    reviewed_candidate_id: Identifier
    reviewed_source_tree_digest: Digest
    target_candidate_id: Identifier
    target_source_tree_digest: Digest
    plan_digest: Digest
    binding_digest: Digest | None
    check_ids: tuple[Identifier, ...]
    reviewed_receipt_id: Identifier | None
    target_receipt_id: Identifier | None
    verdict: Literal["confirmed", "refuted", "unresolved"]
    resolution: Literal["resolved_on_checked_cases", "not_resolved", "not_a_confirmed_defect", "unresolved"]
    impact: Literal["harmful_on_checked_cases", "improved_on_checked_cases", "changed_without_checked_improvement", "unchanged", "unresolved"]
    improved_checks: tuple[Identifier, ...] = ()
    regressed_checks: tuple[Identifier, ...] = ()
    claim_key: Identifier | None
    prior_visibility: Literal["already_reported", "not_previously_reported", "unknown"]
    coverage_scope: Text
    note: Text


class OfflineAdjudicator:
    def __init__(self, home):
        home = Path(os.path.abspath(home))
        base = _root(ROOT / "temp")
        if not home.is_relative_to(base) or home == base:
            raise ValueError("Adjudication outputs require their own project temp directory")
        directory = base
        for part in home.relative_to(base).parts:
            directory = directory / part
            if directory.exists():
                _unlinked(directory)
            else:
                directory.mkdir()
        self.home = _root(home)
        self._plans, self._receipts, self._decisions = {}, {}, {}

    def register_probe(self, plan, source):
        """Trusted developer entry; no caller-selected shell or model code."""
        plan = ProbePlan.parse(plan)
        source = Path(source)
        _root(source.parent)
        raw = _read(source.parent, source.name)
        if sha256(raw) != plan.probe_sha256:
            raise ProtocolError("probe_source_hash_mismatch")
        key = sha256(canonical_bytes(plan))
        directory = self.home / ("plan-" + key)
        if key not in self._plans:
            directory.mkdir()
            (directory / "probe.py").write_bytes(raw)
            (directory / "plan.json").write_bytes(canonical_bytes(plan))
            self._plans[key] = (plan, directory)
        return key

    def _plan(self, key):
        if key not in self._plans:
            raise ProtocolError("probe_plan_not_registered")
        plan, directory = self._plans[key]
        if sha256(_read(directory, "plan.json")) != key or sha256(_read(directory, "probe.py")) != plan.probe_sha256:
            raise ProtocolError("frozen_probe_changed")
        return plan, directory

    def run_probe(self, snapshot, plan_key, *, timeout=10, check=lambda: None):
        check()
        plan, directory = self._plan(plan_key)
        snapshot.verify()
        c = snapshot.candidate
        if plan.task_digest != c.task_digest or any(p.requirement_ref not in {r.requirement_id for r in snapshot.requirements} for p in plan.checks):
            raise ProtocolError("probe_task_or_requirement_mismatch")
        if type(timeout) not in (float, int) or not 0 < timeout <= 60:
            raise ValueError("Use a bounded probe timeout")
        for receipt, _, _, _ in self._receipts.values():
            if (receipt.run_id, receipt.candidate_id, receipt.plan_digest) == (c.run_id, c.candidate_id, plan_key):
                self._receipt(receipt.receipt_id, snapshot, plan_key)
                return receipt  # No accidental repeated evaluation identity.
        output = self.home / ("probe-" + uuid.uuid4().hex)
        output.mkdir()
        repo = output / "repo"
        repo.mkdir()
        files = {}
        for doc in c.documents:
            if doc.side != "candidate":
                continue
            path = repo / doc.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(snapshot.read(doc.document_id))
            files[doc.path] = doc.sha256
        (repo / "temp").mkdir(exist_ok=True)
        config = {"plan": plan.model_dump(mode="json"), "probe": str(directory / "probe.py"), "repo": str(repo), "files": files}
        (output / "input.json").write_bytes(canonical_bytes(config))
        env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", TEMP=str(repo / "temp"), TMP=str(repo / "temp"))
        reason = None
        with (output / "console.log").open("wb") as console:
            process = subprocess.Popen([sys.executable, "-B", "-I", str(Path(__file__).resolve()),
                "--child", str(output / "input.json")], cwd=repo, env=env, stdout=console, stderr=subprocess.STDOUT)
            end = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    check()
                    if time.monotonic() >= end or (output / "console.log").stat().st_size > 65536:
                        reason = "timeout_or_output_bound"
                        break
                    try:
                        process.wait(timeout=.1)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    from task_service_worker import terminate_owned_process
                    terminate_owned_process(process)
                    process.wait(timeout=5)
        snapshot.verify()
        self._plan(plan_key)
        if (output / "console.log").stat().st_size > 65536:
            reason = "probe_output_bound"
        if any(sha256(_read(repo, p)) != expected for p, expected in files.items()):
            reason = "probe_modified_evaluated_source"
        for path in repo.rglob("*"):
            _unlinked(path)
            relative = path.relative_to(repo)
            if path.is_file() and relative.parts[0] != "temp" and relative.as_posix() not in files:
                reason = "probe_created_unregistered_file"
        results = {p.check_id: "unknown" for p in plan.checks}
        if reason is None and process.returncode == 0 and (output / "result.json").is_file():
            value = json.loads(_read(output, "result.json"))
            if set(value) != set(results) or any(v not in {"passed", "failed", "unknown"} for v in value.values()):
                raise ProtocolError("probe_result_shape_mismatch")
            results = value
        else:
            reason = reason or "probe_process_unavailable"
        raw = canonical_bytes({"results": results, "reason": reason})
        (output / "receipt-results.json").write_bytes(raw)
        receipt = ProbeReceipt(receipt_id="receipt-" + uuid.uuid4().hex, run_id=c.run_id, candidate_id=c.candidate_id,
            source_tree_digest=c.source_tree_digest, task_digest=c.task_digest, baseline_digest=c.baseline_digest,
            plan_digest=plan_key, results_digest=sha256(raw), status="unavailable" if reason else "completed")
        (output / "receipt.json").write_bytes(canonical_bytes(receipt))
        self._receipts[receipt.receipt_id] = (receipt, output, snapshot, results)
        check()
        return receipt

    def _receipt(self, receipt_id, snapshot, plan_key):
        if receipt_id not in self._receipts:
            raise ProtocolError("receipt_not_program_issued")
        receipt, output, _, results = self._receipts[receipt_id]
        c = snapshot.candidate
        snapshot.verify()
        self._plan(plan_key)
        if (receipt.run_id, receipt.candidate_id, receipt.source_tree_digest, receipt.task_digest, receipt.baseline_digest, receipt.plan_digest) != (
                c.run_id, c.candidate_id, c.source_tree_digest, c.task_digest, c.baseline_digest, plan_key):
            raise ProtocolError("receipt_version_mismatch")
        if _read(output, "receipt.json") != canonical_bytes(receipt) or sha256(_read(output, "receipt-results.json")) != receipt.results_digest:
            raise ProtocolError("receipt_file_changed")
        return receipt, results

    def adjudicate(self, review, reviewer, reviewed, target, *, evidence, reports, plan_key,
                   reviewed_receipt_id=None, target_receipt_id=None, bindings=()):
        reviewed.verify()
        target.verify()
        a, b = reviewed.candidate, target.candidate
        if (a.run_id, a.task_digest, a.baseline_digest) != (b.run_id, b.task_digest, b.baseline_digest):
            raise ProtocolError("adjudication_lineage_mismatch")
        validate_review(review, a, reviewer, evidence, {d.document_id: d for d in a.documents}, reports, reviewed.requirements)
        plan, _ = self._plan(plan_key)
        if plan.task_digest != a.task_digest:
            raise ProtocolError("adjudication_plan_task_mismatch")
        specs = {c.check_id: c for c in plan.checks}
        trusted, claim_groups = {}, {}
        for row in bindings:
            binding = FindingBinding.parse(row)
            finding = next((f for f in review.findings if f.finding_id == binding.finding_id), None)
            if finding is None or binding.review_id != review.review_id or binding.finding_sha256 != sha256(canonical_bytes(finding)):
                raise ProtocolError("finding_mapping_mismatch")
            if binding.finding_id in trusted or len(set(binding.check_ids)) != len(binding.check_ids):
                raise ProtocolError("duplicate_finding_mapping")
            if any(k not in specs or specs[k].requirement_ref != finding.requirement_ref for k in binding.check_ids):
                raise ProtocolError("finding_check_requirement_mismatch")
            signature = (finding.requirement_ref, tuple(sorted(binding.check_ids)))
            if binding.claim_key in claim_groups and claim_groups[binding.claim_key] != signature:
                raise ProtocolError("claim_group_has_different_checked_scope")
            claim_groups[binding.claim_key] = signature
            binding_raw = canonical_bytes(binding)
            binding_path = self.home / ("binding-" + sha256(binding_raw) + ".json")
            if binding_path.exists() and _read(self.home, binding_path.name) != binding_raw:
                raise ProtocolError("trusted_binding_changed")
            if not binding_path.exists():
                with binding_path.open("xb") as stream:
                    stream.write(binding_raw)
            trusted[binding.finding_id] = binding
        before = self._receipt(reviewed_receipt_id, reviewed, plan_key)[1] if reviewed_receipt_id else {}
        after = self._receipt(target_receipt_id, target, plan_key)[1] if target_receipt_id else {}
        improved = tuple(k for k in specs if before.get(k) == "failed" and after.get(k) == "passed")
        regressed = tuple(k for k in specs if before.get(k) == "passed" and after.get(k) == "failed")
        impact = ("harmful_on_checked_cases" if regressed else "unresolved" if not before or not after or "unknown" in (*before.values(), *after.values())
                  else "improved_on_checked_cases" if improved else "changed_without_checked_improvement" if a.source_tree_digest != b.source_tree_digest else "unchanged")
        decisions = []
        for finding in review.findings:
            binding = trusted.get(finding.finding_id)
            keys = binding.check_ids if binding else ()
            values = [before.get(k, "unknown") for k in keys]
            verdict = "confirmed" if "failed" in values else "refuted" if values and all(v == "passed" for v in values) else "unresolved"
            current = [after.get(k, "unknown") for k in keys]
            resolution = ("resolved_on_checked_cases" if verdict == "confirmed" and current and all(v == "passed" for v in current)
                else "not_resolved" if verdict == "confirmed" and "failed" in current else "not_a_confirmed_defect" if verdict == "refuted" else "unresolved")
            visibility = {specs[k].prior_visibility for k in keys}
            prior = "already_reported" if "already_reported" in visibility else "not_previously_reported" if visibility == {"not_previously_reported"} else "unknown"
            fields = dict(run_id=a.run_id, review_id=review.review_id, finding_id=finding.finding_id,
                finding_sha256=sha256(canonical_bytes(finding)), reviewed_candidate_id=a.candidate_id, reviewed_source_tree_digest=a.source_tree_digest,
                target_candidate_id=b.candidate_id, target_source_tree_digest=b.source_tree_digest, plan_digest=plan_key,
                binding_digest=sha256(canonical_bytes(binding)) if binding else None, check_ids=tuple(keys),
                reviewed_receipt_id=reviewed_receipt_id, target_receipt_id=target_receipt_id, verdict=verdict, resolution=resolution,
                impact=impact, improved_checks=improved, regressed_checks=regressed,
                claim_key=binding.claim_key if binding else None, prior_visibility=prior,
                coverage_scope=binding.coverage_scope if binding else "No trusted exact claim/check mapping; the claim remains unresolved.",
                note="Verdicts cover registered cases and trusted claim mapping only. Accepted/rejected/confidence/model votes are not independent truth. Impact is a version comparison, not proof of causal attribution to a finding.")
            decision = FindingDecision(decision_id="decision-" + sha256(canonical_bytes(fields))[:32], **fields)
            raw = canonical_bytes(decision)
            path = self.home / (decision.decision_id + ".json")
            if path.exists() and _read(self.home, path.name) != raw:
                raise ProtocolError("adjudication_record_changed")
            if not path.exists():
                with path.open("xb") as stream:
                    stream.write(raw)
            self._decisions[decision.decision_id] = decision
            decisions.append(decision)
        return tuple(decisions)


def decision_summary(decisions):
    """Deduplicate trusted claim groups without rewriting original findings."""
    unique = {}
    for d in decisions:
        if d.decision_id in unique and unique[d.decision_id] != d:
            raise ProtocolError("conflicting_decision_id")
        unique[d.decision_id] = d
    values = list(unique.values())
    contexts = {(d.run_id, d.review_id, d.reviewed_candidate_id, d.target_candidate_id, d.plan_digest) for d in values}
    if len(contexts) > 1:
        raise ProtocolError("summary_mixes_evaluation_contexts")
    groups = {}
    for d in values:
        if d.claim_key:
            groups.setdefault(d.claim_key, []).append(d)
    confirmed = {k: rows for k, rows in groups.items() if all(d.verdict == "confirmed" for d in rows)}
    return {"findings": len(values), **{v: sum(d.verdict == v for d in values) for v in ("confirmed", "refuted", "unresolved")},
            "duplicate_findings": sum(max(0, len(rows) - 1) for rows in groups.values()), "distinct_confirmed_groups": len(confirmed),
            "new_confirmed_groups_within_scope": sum(all(d.prior_visibility == "not_previously_reported" for d in rows) for rows in confirmed.values()),
            "already_reported_confirmed_groups": sum(any(d.prior_visibility == "already_reported" for d in rows) for rows in confirmed.values()),
            "scope": "Program-checked declared cases and trusted mappings only; no general success-rate or autonomous-discovery claim."}


def child(config_path):
    config_path = Path(config_path)
    if not config_path.resolve().is_relative_to(ROOT / "temp"):
        raise ProtocolError("probe_child_requires_temp_input")
    config = json.loads(_read(config_path.parent, config_path.name))
    plan = ProbePlan.parse(config["plan"])
    probe, repo = Path(config["probe"]), _root(config["repo"])
    if sha256(_read(probe.parent, probe.name)) != plan.probe_sha256:
        raise ProtocolError("probe_child_hash_mismatch")
    if any(sha256(_read(repo, k)) != h for k, h in config["files"].items()):
        raise ProtocolError("probe_child_source_mismatch")
    sys.path.insert(0, str(repo))
    os.environ["ADJUDICATION_REPO"] = str(repo)
    spec = importlib.util.spec_from_file_location("registered_probe", probe)
    module = importlib.util.module_from_spec(spec)
    sys.modules["registered_probe"] = module
    spec.loader.exec_module(module)
    outcomes = {}
    for check in plan.checks:
        suite = unittest.defaultTestLoader.loadTestsFromName(check.test_id)
        result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        unknown = result.testsRun != 1 or result.errors or result.skipped or result.expectedFailures or result.unexpectedSuccesses
        outcomes[check.check_id] = "unknown" if unknown else "failed" if result.failures else "passed"
    (config_path.parent / "result.json").write_bytes(canonical_bytes(outcomes))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=Path)
    args = parser.parse_args()
    if args.child:
        child(args.child)
    else:
        parser.print_help()
