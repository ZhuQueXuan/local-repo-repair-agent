"""Explicit stage-C worker runner. Stores remain temp-only until panel rollout."""
import argparse
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

from collaboration import CollaborationCoordinator, attributed_usage
from collaboration_protocol import (EvidenceRef, FindingResponse, Handoff, ProgramReport, ProtocolError,
                                     canonical_bytes, sha256)
from collaboration_runtime import (ProtectedReviewModel, RepairEnvironment, RepairModel, RepairSession, run_review)
from collaboration_store import CollaborationStore
from deepseek_budget import SharedBudget, load_budget_config
from review_tools import ReviewEvidenceBroker, seal_candidate
from state_agent import StatefulAgent
from task_service_store import ROOT, now
from task_workspace import digest, load_task, package_root, prepare_task


def verification_summary(report, baseline):
    """Keep acceptance relative to baseline distinct from an all-green suite."""
    def failures(suite):
        return set(suite["failures"] + suite["errors"])

    before, after = failures(baseline["full_upstream_suite"]), failures(report["full_upstream_suite"])
    groups = {}
    for name, names in (("baseline_failures", before), ("remaining_failures", after), ("new_failures", after - before)):
        ordered = sorted(names)
        groups[name] = {"count": len(ordered), "names": [n[:96] for n in ordered[:3]],
                        "truncated": len(ordered) > 3 or any(len(n) > 96 for n in ordered[:3])}
    value = {"schema_version": 2, "acceptance_rule": "task_checks_pass_and_no_new_full_suite_failures",
             "accepted_against_baseline": bool(report["task_tests_passed"] and not (after - before)),
             "issue": {k: report["issue_checks"][k] for k in ("tests_run", "passed", "successful")},
             "targeted": {k: report["upstream_targeted"][k] for k in ("tests_run", "passed", "successful")},
             "full_suite": {k: report["full_upstream_suite"][k] for k in ("tests_run", "passed")},
             "full_suite_all_passed": report["full_upstream_suite"]["successful"],
             "protected_tests_unchanged": report["protected_tests_unchanged"], **groups,
             "note": "Acceptance against baseline does not mean all tests passed. Listed failures remain; truncated names are previews."}
    # ProgramReport.summary is at most 2,000 characters; never slice JSON text.
    while len(encoded := json.dumps(value, ensure_ascii=False)) > 2000:
        group = max(groups.values(), key=lambda g: sum(map(len, g["names"])))
        group["names"].pop()
        group["truncated"] = True
    return encoded


class LiveRunner:
    def __init__(self, store, run_id, *, settings, accounting=None):
        self.store, self.run_id, self.settings = store, run_id, settings
        self.accounting = accounting or SharedBudget(load_budget_config())
        self.output = store.home / "runs" / run_id
        self.output.mkdir(parents=True, exist_ok=False)
        self.route_state, self.session, self.work = {}, None, None
        self.models, self.assessments = [], {}
        self.baseline = None
        self.begin = time.monotonic()
        self.parent_deadline = time.monotonic() + store.get(run_id)["collaboration_policy"]["parent_seconds"]

    def check_parent(self):
        from collaboration_store import CollaborationStopped
        parent = self.store.get(self.run_id)
        if parent["cancel_requested"]:
            raise CollaborationStopped("Parent cancelled")
        if time.monotonic() >= self.parent_deadline:
            raise TimeoutError("Parent deadline exceeded")

    def command(self, command, *, timeout, check):
        from task_service_worker import terminate_owned_process
        check()
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        end = time.monotonic() + timeout
        try:
            while True:
                check()
                try:
                    output, _ = process.communicate(timeout=min(0.25, max(0.001, end - time.monotonic())))
                    check()
                    return process.returncode, output
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= end:
                        raise TimeoutError("Owned verification process deadline exceeded")
        finally:
            if process.poll() is None:
                terminate_owned_process(process)
                process.communicate(timeout=5)

    def grade(self, checkout, name, context=None):
        check = context.check if context else self.check_parent
        deadline = context.deadline if context else self.parent_deadline
        check()
        directory = self.output / name
        directory.mkdir()
        target = directory / "report.json"
        code, console = self.command([sys.executable, "-B", str(ROOT / "scripts/check_task.py"), "--task",
            self.task, "--repo", str(checkout), "--output", str(target), "--full-suite"],
            timeout=min(240, deadline - time.monotonic()), check=check)
        (directory / "console.log").write_bytes(console)
        if not target.exists():
            raise RuntimeError("Independent evaluation produced no report")
        report = json.loads(target.read_text(encoding="utf-8"))
        expected = 0 if report["task_tests_passed"] and report["full_upstream_suite"]["successful"] else 1
        if code != expected:
            raise RuntimeError("Evaluator exit/report mismatch")
        return report

    def setup(self):
        from task_service_worker import freeze_inputs
        record = self.store.get(self.run_id)
        self.task = record["task"]
        self.frozen = freeze_inputs(self.task, self.output)
        policy_path = ROOT / "configs/multi_agent_v1.json"
        self.frozen[policy_path.relative_to(ROOT).as_posix()] = digest(policy_path)
        import shutil
        shutil.copyfile(policy_path, self.output / "frozen_inputs/configs/multi_agent_v1.json")
        (self.output / "inputs.json").write_bytes(canonical_bytes({"schema_version": 1, "files_sha256": self.frozen}))
        (self.output / "run_config.json").write_bytes(canonical_bytes(record))
        self.source, self.manifest = load_task(self.task)
        self.issue = (self.source / "issue.md").read_bytes()
        self.work, _ = prepare_task(self.task)
        self.store.update(self.run_id, event="workspace_ready", workspace=str(self.work))
        self.baseline = self.grade(self.work, "baseline")
        if self.baseline["issue_checks"]["successful"] or not self.baseline["upstream_targeted"]["successful"]:
            raise RuntimeError("Expected reproduced problem and passing upstream targeted tests")

    def assessment_passes(self, report):
        before = self.baseline["full_upstream_suite"]
        after = report["full_upstream_suite"]
        regression = set(after["failures"] + after["errors"]) - set(before["failures"] + before["errors"])
        return bool(report["task_tests_passed"] and not regression)

    def save_model(self, model):
        model.ledger_path.write_bytes(canonical_bytes(model.ledger))

    def seal(self, context):
        from task_service_worker import collect_patch
        phase = context.role["phase"]
        directory = self.output / phase
        directory.mkdir()
        changed = collect_patch(self.task, self.work, directory, self.manifest)
        if not changed:
            raise ProtocolError("no_nonempty_candidate")
        snapshot = seal_candidate(self.manifest, self.source / "repo", self.work, self.issue,
            self.store.home / "sealing" / (phase + "-" + uuid.uuid4().hex),
            run_id=self.run_id, producer_role_run_id=context.identity.role_run_id)
        previous_id = self.store.state(self.run_id)["collaboration_state"]["candidate_id"]
        if previous_id == snapshot.candidate.candidate_id:
            snapshot = self.store.snapshot(self.run_id, previous_id)
        else:
            self.store.register_snapshot(context.identity.role_run_id, snapshot)
        report = self.grade(self.work, phase + "-verification", context)
        c = snapshot.candidate
        program = ProgramReport(report_id=phase + "-program", run_id=self.run_id, candidate_id=c.candidate_id,
            source_tree_digest=c.source_tree_digest, producer_role_run_id="independent-evaluator",
            origin="program_test", status="passed" if self.assessment_passes(report) and changed else "failed",
            scope="Frozen issue/targeted/extended checks compared with baseline; actual subprocess execution, not reviewer truth.",
            summary=verification_summary(report, self.baseline))
        self.store.register_record(context.identity.role_run_id, "report", program)
        self.assessments[c.candidate_id] = {"passed": program.status == "passed", "report": program.report_id,
            "patch": str(directory / "repair.diff"), "changed": changed}
        return snapshot

    def repair(self, context):
        record = self.store.get(self.run_id)
        configuration = record["configuration"]
        if context.role["phase"] == "repairing":
            env = RepairEnvironment(self.work, Path(os.environ.get("MINI_TASK_BASH", r"D:\Git\bin\bash.exe")),
                source_files=list(self.manifest["source_files_sha256"]), state_policy=configuration["task_state"],
                python_import_root=package_root(self.work, self.manifest), context=context, **configuration["tool_output_budget"])
            model = RepairModel(self.settings, task_state=env.task_state, context_policy=configuration["context_budget"],
                ledger_path=self.output / "repairer-ledger.json", error_path=self.output / "repairer-error.json",
                context=context, accounting=self.accounting, route_state=self.route_state)
            env.model = model
            if configuration.get("archive_assistant_content"):
                model.context_budget.enable_assistant_archives(self.work)
            self.models.append(model)
            preflight = env.execute({"command": "python -B -c " + shlex.quote("import " + self.manifest["package"] + "; print(" + self.manifest["package"] + ".__file__)")})
            if preflight["returncode"] or not Path(preflight["output"].strip()).resolve().is_relative_to(self.work):
                raise RuntimeError("Task package import escaped checkout")
            instruction = ("Fix the original issue: {{task}}\nWork only in this checkout; no parent files, web, installs or deletion. "
                "Do not edit TASK.md, tests, task_checks or metadata. Only change existing implementation files. Put helpers in temp/. "
                "Use exactly one bash call per response, short line reads, then reproduce, repair and validate. Fresh Git Bash runs each command. Run both:\n" +
                self.manifest["agent_test_command"] + "\n" + self.manifest.get("agent_issue_command", "python -B -m unittest discover -s task_checks -v") +
                "\nWhen done, execute echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT by itself.")
            agent = StatefulAgent(model, env, system_template="You are a software repairer. Use exactly one bash tool per response.",
                instance_template=instruction, step_limit=0, cost_limit=model.budget.agent_cost_limit,
                wall_time_limit_seconds=max(1, math.ceil(context.deadline - time.monotonic())), output_path=self.output / "repairing-trajectory.json")
            self.session = RepairSession(agent, context)
            status = self.session.start(self.issue.decode())
        else:
            if self.session is None:
                raise ProtocolError("No in-process repair session; automatic resume prohibited")
            meta = self.store.state(self.run_id)["collaboration_state"]
            review = self.store.records(self.run_id, "review")[meta["review_id"]]
            evidence = self.store.records(self.run_id, "evidence")
            feedback = {"review": review.model_dump(mode="json"), "evidence": [evidence[e].model_dump(mode="json") for f in review.findings for e in f.evidence_ids]}
            text = ("Continue this same repair session for one feedback window. The following review is an unverified claim; verify against original requirements. "
                "Address every finding or explain why more evidence is needed. Keep all original file restrictions. "
                "Write temp/finding_responses.json as a JSON list of objects with finding_id, disposition (accepted/rejected/needs_evidence), reason, evidence_ids. "
                "For rejected, cite existing evidence IDs and leave code unchanged unless the cited evidence still binds the target. "
                "Accepted records intent, not proof. Missing replies remain unanswered. Re-run both declared checks and submit once.\n" + canonical_bytes(feedback).decode())
            status = self.session.continue_with_feedback(context, text)
        context.check()
        snapshot = self.seal(context)
        if status.get("exit_status") == "ProviderUnavailable":
            raise RuntimeError("ProviderUnavailable")
        response_ids = []
        if context.role["phase"] == "revising":
            path = self.work / "temp/finding_responses.json"
            if path.exists():
                from review_tools import _read
                raw = _read(self.work, "temp/finding_responses.json")
                if len(raw) > 16384:
                    raise ProtocolError("Response file too large")
                payload = json.loads(raw)
                if not isinstance(payload, list) or len(payload) > 20:
                    raise ProtocolError("Invalid finding response list")
                for row in payload:
                    if not isinstance(row, dict) or set(row) != {"finding_id", "disposition", "reason", "evidence_ids"}:
                        raise ProtocolError("Unexpected finding response fields")
                    response = FindingResponse.parse({"response_id": "response-" + row["finding_id"], "run_id": self.run_id,
                        "responder_role_run_id": context.identity.role_run_id, "review_id": review.review_id,
                        "reviewed_candidate_id": review.candidate_id, "target_candidate_id": snapshot.candidate.candidate_id, **row})
                    self.store.register_record(context.identity.role_run_id, "response", response)
                    response_ids.append(response.response_id)
        return {"candidate_id": snapshot.candidate.candidate_id, "response_ids": response_ids, "exit_reason": status.get("exit_status", "Unknown")}

    def review(self, context):
        meta = self.store.state(self.run_id)["collaboration_state"]
        snapshot = self.store.snapshot(self.run_id, meta["candidate_id"])
        candidate = snapshot.candidate
        reports = [r for r in self.store.records(self.run_id, "report").values() if r.candidate_id == candidate.candidate_id]
        handoff = Handoff(handoff_id=context.role["phase"] + "-handoff", run_id=self.run_id,
            from_role_run_id=candidate.producer_role_run_id, to_role_run_id=context.identity.role_run_id,
            task_digest=candidate.task_digest, baseline_digest=candidate.baseline_digest, candidate_id=candidate.candidate_id,
            source_tree_digest=candidate.source_tree_digest, patch_sha256=candidate.patch_sha256, snapshot_id=candidate.snapshot_id,
            read_document_ids=tuple(d.document_id for d in candidate.documents), report_ids=tuple(r.report_id for r in reports),
            questions=("Independently check this immutable patch against the original issue; cite issued evidence, not guesses.",))
        self.store.register_record(context.identity.role_run_id, "handoff", handoff)
        broker = ReviewEvidenceBroker(snapshot, context.identity, handoff, reports=reports,
            cancelled=lambda: self.store.get(self.run_id)["cancel_requested"])
        model = ProtectedReviewModel(self.settings, context=context, accounting=self.accounting, route_state=self.route_state,
            ledger_path=self.output / (context.role["phase"] + "-ledger.json"), error_path=self.output / (context.role["phase"] + "-error.json"))
        self.models.append(model)
        selected_docs = [d for d in candidate.documents if d.side in {"issue", "diff"} or d.path in self.assessments[candidate.candidate_id]["changed"]]
        data = {"run_id": self.run_id, "reviewer_role_run_id": context.identity.role_run_id,
            "candidate_id": candidate.candidate_id, "source_tree_digest": candidate.source_tree_digest,
            "snapshot_id": candidate.snapshot_id, "requirements": [r.model_dump(mode="json") for r in snapshot.requirements],
            "documents": [{"document_id": d.document_id, "side": d.side, "path": d.path, "lines": d.line_count} for d in selected_docs],
            "report_ids": [r.report_id for r in reports], "review_id": context.role["phase"] + "-review"}
        if context.role["phase"] == "final_review":
            data["previous_review"] = self.store.records(self.run_id, "review")[meta["review_id"]].model_dump(mode="json")
            data["responses"] = [r.model_dump(mode="json") for r in self.store.records(self.run_id, "response").values()]
        prompt = "Use registered IDs below. Read diff and issue, then selected source lines/report as needed. Do not print long code or read whole files. Submit a ReviewReport with coverage and findings; approve only when justified, insufficient_evidence with explicit unknowns when needed.\n" + canonical_bytes(data).decode()
        try:
            review = run_review(model, broker, context, self.output / (context.role["phase"] + "-trajectory.json"), prompt)
        finally:
            for ref in broker.evidence.values():
                if not self.store.get(self.run_id)["cancel_requested"]:
                    self.store.register_record(context.identity.role_run_id, "evidence", ref)
            (self.output / (context.role["phase"] + "-events.json")).write_bytes(canonical_bytes(broker.events))
        self.store.register_record(context.identity.role_run_id, "review", review)
        return {"review_id": review.review_id, "exit_reason": "ReviewSubmitted"}

    def __call__(self, context):
        context.deadline = min(context.deadline, self.parent_deadline)
        return self.repair(context) if context.role["role"] == "repairer" else self.review(context)

    def finalize(self, result):
        self.check_parent()
        candidate_id = next(reversed(self.assessments), None) or self.store.state(self.run_id)["collaboration_state"]["candidate_id"]
        assessment = self.assessments.get(candidate_id)
        repair_result = "unknown"
        if assessment:
            repair_result = "passed" if assessment["passed"] else "failed"
            if assessment["passed"]:
                replay, _ = prepare_task(self.task)
                code, _ = self.command(["git", "-C", str(replay), "apply", "--", assessment["patch"]],
                    timeout=min(30, self.parent_deadline - time.monotonic()), check=self.check_parent)
                replay_report = self.grade(replay, "patch-replay") if code == 0 else None
                if replay_report is None or not self.assessment_passes(replay_report):
                    repair_result = "failed"
            import shutil
            shutil.copyfile(assessment["patch"], self.output / "repair.diff")
        if any(digest(ROOT / path) != expected for path, expected in self.frozen.items()):
            raise ProtocolError("Frozen live run input changed")
        if assessment is None:
            return
        fields = {"repair_result": repair_result, "final_candidate_id": candidate_id, "assessment_report_id": assessment["report"]}
        if result["status"] == "failed" and assessment:
            fields.update(collaboration_result="degraded", review_verdict="unavailable",
                reason=result["reason"] + "; previous legal candidate and its actual independent assessment are retained.")
        elif result["status"] == "finished":
            fields["reason"] = "Model collaboration ended; repair acceptance is from independent checks and clean patch replay."
        self.store.attach_live_result(self.run_id, "assessment", fields)

    def close(self):
        for model in self.models:
            model.ledger.update(completed=True, note="Entire role/session ended; do not resume or resend.")
            self.save_model(model)
        usage = attributed_usage(self.accounting, self.run_id)
        usage["total_elapsed_seconds"] = round(time.monotonic() - self.begin, 3)
        summary = {"run": self.store.get(self.run_id), "roles": self.store.roles(self.run_id), "usage": usage,
            "route_state": self.route_state, "assessments": self.assessments,
            "scope": "Live integration acceptance on an existing public development task, not effectiveness evidence."}
        (self.output / "summary.json").write_bytes(canonical_bytes(summary))
        for kind in ("report", "handoff", "review", "response", "evidence"):
            (self.output / (kind + "s.json")).write_bytes(canonical_bytes([r.model_dump(mode="json") for r in self.store.records(self.run_id, kind).values()]))
        (self.output / "events.json").write_bytes(canonical_bytes(self.store.events(self.run_id)))
        text_files = [p for p in self.output.rglob("*") if p.is_file()]
        secrets = [v for k, v in self.settings.items() if "KEY" in k and isinstance(v, str) and v]
        if any(secret in p.read_text(encoding="utf-8", errors="replace") for secret in secrets for p in text_files):
            self.store.update(self.run_id, status="failed", reason="Sensitive artifact check failed", artifacts={})
            raise ProtocolError("Sensitive output blocked")
        hashes = {p.relative_to(self.output).as_posix(): digest(p) for p in text_files}
        (self.output / "artifact_sha256.json").write_bytes(canonical_bytes(hashes))
        names = ("summary.json", "repair.diff", "reports.json", "handoffs.json", "reviews.json", "responses.json", "evidences.json",
                 "baseline/report.json", "repairing-verification/report.json", "revising-verification/report.json", "patch-replay/report.json",
                 "reviewing-trajectory.json", "final_review-trajectory.json")
        artifacts = {name: str(self.output / name) for name in names if (self.output / name).is_file() and (self.output / name).stat().st_size <= 262144}
        self.store.attach_live_result(self.run_id, "receipts", {"metrics": usage, "artifacts": artifacts})


def run_live_worker(store, run_id):
    from dotenv import dotenv_values
    parent = store.get(run_id)
    if parent["status"] != "queued":
        return parent  # Interrupted/ended identities never call models again.
    runner = LiveRunner(store, run_id, settings=dotenv_values(ROOT / ".env", interpolate=False))
    try:
        runner.setup()
        result = CollaborationCoordinator(store, runner).run(run_id)
        if result["status"] != "cancelled":
            runner.finalize(result)
    except Exception as error:
        store.stop(run_id, "failed", "Live stage failure: " + type(error).__name__)
        if store.get(run_id)["status"] == "finished":
            store.attach_live_result(run_id, "assessment_error", {"repair_result": "unknown", "collaboration_result": "degraded",
                "reason": "Final independent assessment unavailable: " + type(error).__name__})
        (runner.output / "failure.json").write_bytes(canonical_bytes({"type": type(error).__name__}))
    finally:
        runner.close()
    return store.get(run_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default="split-after-boundary")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        print("Pass --run to create exactly one new live integration identity using the shared protected accounting.")
        return
    home = ROOT / "temp/multi_agent_stage_c_20261023" / ("live_" + uuid.uuid4().hex)
    store = CollaborationStore(home)
    policy = json.loads((ROOT / "configs/multi_agent_v1.json").read_text(encoding="utf-8"))
    parent = store.create_collaboration(args.task, policy["repair_profile"], uuid.uuid4().hex, policy, execution="live")
    command = [sys.executable, "-B", str(ROOT / "scripts/task_service_worker.py"), "--collaboration", "--run-id", parent["id"], "--home", str(home)]
    print("Run: " + parent["id"] + "\nStore: " + str(home), flush=True)
    result = subprocess.run(command, cwd=ROOT)
    print("Result: " + str(home / "runs" / parent["id"] / "summary.json"), flush=True)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
