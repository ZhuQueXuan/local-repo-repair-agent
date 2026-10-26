"""Model-role hooks and in-process repair continuation; no implicit resume."""
import json
import math
import os
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent.parent
os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(ROOT / "temp/mini-global")
os.environ["MSWEA_SILENT_STARTUP"] = "1"
os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from collaboration import RoleBudget
from collaboration_protocol import ProtocolError, canonical_bytes, sha256
from collaboration_store import StateConflict
from deepseek_budget import SharedBudget
from minisweagent.exceptions import FormatError, InterruptAgentFlow
from review_model import ReviewModel
from run_task_batch import StatefulModel, StatefulTaskEnvironment
from context_budget import ContextBudget, message_bytes
from collaboration_feedback import pytest_environment


class LiveRoleBudget(RoleBudget):
    attributed = True

    def __init__(self, config, store, identity, request_id, *, ledger_path, temp_dir, check):
        # Explicit C capability: production accounting uses the existing shared
        # ledger and its existing lock, never a new role-specific ledger.
        SharedBudget.__init__(self, config, ledger_path=ledger_path, temp_dir=temp_dir)
        self.store, self.identity, self.logical_request_id, self.check = store, identity, request_id, check
        if store.role(identity.role_run_id)["session_id"] != identity.session_id:
            raise StateConflict("budget_session_identity_mismatch")

    def _check_role(self):
        self.check()


class RoleModelHooks:
    def __init__(self, *args, context, accounting, route_state, **kwargs):
        self.context, self.accounting = context, accounting
        self.request_ordinal = 0
        super().__init__(*args, budget=accounting, route_state=route_state,
                         request_check=lambda: self.context.check(), request_deadline=lambda: self.context.deadline, **kwargs)

    def query(self, messages):
        self.context.check()
        # Provider responses may contain an echoed credential. Never persist it
        # or execute its actions; settlement has already happened at this point.
        result = super().query(messages)
        self.context.check()
        if self.contains_credentials(canonical_bytes(result).decode()):
            raise ProtocolError("credential_in_model_output")
        return result

    def _query(self, messages, **kwargs):
        self.context.check()
        if self.contains_credentials(canonical_bytes(messages).decode()):
            raise ProtocolError("credential_in_role_input")
        self.request_ordinal += 1
        request = self.context.store.prepare_request(self.context.identity.role_run_id,
            "query-" + str(self.request_ordinal), sha256(canonical_bytes(messages)))
        self.budget = LiveRoleBudget(self.budget_config, self.context.store, self.context.identity, request["id"],
            ledger_path=self.accounting.path, temp_dir=self.accounting.temp, check=self.context.check)
        return super()._query(messages, **kwargs)

    def _prepare_messages_for_api(self, messages):
        # Internal exit records never belong on the API wire.
        return super()._prepare_messages_for_api([m for m in messages if m.get("role") != "exit"])

    def _parse_actions(self, response):
        if self.contains_credentials(canonical_bytes(response.model_dump()).decode()):
            raise ProtocolError("credential_in_model_output")
        return super()._parse_actions(response)


class RepairModel(RoleModelHooks, StatefulModel):
    def project_context(self, messages, prepared, pinned=None):
        projected = super().project_context(messages, prepared, pinned)
        if message_bytes(projected) <= self.budget_config["max_messages_utf8_bytes"]:
            return projected
        # Collaboration feedback can coexist with four large recent actions.
        # Re-project only completed exchanges, retain the latest complete one,
        # and leave raw history, task, feedback and hard stop untouched.
        policy = self.context_budget
        emergency = ContextBudget(target_bytes=policy.target_bytes, keep_recent_tool_outputs=1,
            compact_completed_exchanges=True, retained_audit_entries=policy.audit_entries, compact_to_target=True)
        emergency.work, emergency.history_directory = policy.work, policy.history_directory
        pinned = list(pinned or [])
        if self.repository_retrieval is not None:
            pinned.append(self.repository_retrieval.pinned_message())
        reserved = len(json.dumps(pinned, ensure_ascii=False).encode("utf-8")) + 2 if pinned else 0
        compact = emergency.project(messages, prepared, reserved_bytes=reserved)
        result = compact[:2] + pinned + compact[2:]
        policy.events.append({"mode": "collaboration_guard_projection", "before_bytes": message_bytes(projected),
            "after_bytes": message_bytes(result), "hard_limit": self.budget_config["max_messages_utf8_bytes"],
            "keep_recent_tool_outputs": 1, "raw_history_preserved": True, "projection": emergency.events})
        return result

    def _parse_actions(self, response):
        actions = super()._parse_actions(response)
        if len(actions) != 1:
            raise FormatError({"role": "user", "content": "Use exactly one bash call per response.", "extra": {"interrupt_type": "FormatError"}})
        return actions


class ProtectedReviewModel(RoleModelHooks, ReviewModel):
    def _prepare_messages_for_api(self, messages):
        prepared = super()._prepare_messages_for_api(messages)
        # Keep raw trajectory intact. Replace old tool bodies only in the API
        # view, retaining their real IDs and exact registered re-read actions.
        tools = [i for i, m in enumerate(prepared) if m.get("role") == "tool"]
        for index in tools[:-3]:
            if len(canonical_bytes(prepared)) <= 12288:
                break
            prior = prepared[index - 1]
            if prior.get("role") != "assistant" or len(prior.get("tool_calls", [])) != 1:
                continue
            call = prior["tool_calls"][0]
            if call["id"] != prepared[index].get("tool_call_id"):
                raise ProtocolError("review_tool_sequence_mismatch")
            raw = json.loads(prepared[index]["content"])
            refs = []
            if isinstance(raw, dict):
                if raw.get("evidence"):
                    refs.append(raw["evidence"]["evidence_id"])
                refs.extend(hit["evidence"]["evidence_id"] for hit in raw.get("hits", []))
            prepared[index] = prepared[index] | {"content": canonical_bytes({"earlier_observation_in_trajectory": True,
                "evidence_ids": refs, "read_again": call["function"]}).decode()}
        return prepared


class RepairEnvironment(StatefulTaskEnvironment):
    def __init__(self, *args, context, **kwargs):
        self.context, self.completed_submission = context, None
        super().__init__(*args, **kwargs)

    def _configure_child_environment(self, env):
        return pytest_environment(Path(self.config.cwd), env)

    def execute(self, action, cwd="", *, timeout=None):
        self.context.check()
        if self.model.contains_credentials(action["command"]):
            raise ProtocolError("credential_in_command")
        remaining = self.context.deadline - time.monotonic()
        self.context.store.event(self.context.identity.run_id, "action_started", {"role_run_id": self.context.identity.role_run_id,
            "command": action["command"]}, "repairer")
        try:
            result = super().execute(action, cwd, timeout=min(timeout or self.config.timeout, remaining))
        except InterruptAgentFlow as error:
            if error.messages[-1].get("extra", {}).get("exit_status") == "Submitted":
                self.completed_submission = {"action": dict(action), "message": error.messages[-1]}
            raise
        self.context.check()
        self.context.store.event(self.context.identity.run_id, "action_finished", {"role_run_id": self.context.identity.role_run_id,
            "returncode": result["returncode"]}, "repairer")
        return result


class RepairSession:
    def __init__(self, agent, context):
        self.agent, self.context = agent, context
        self.initial_calls = 0

    def start(self, task):
        return self.agent.run(task)

    def continue_with_feedback(self, context, feedback):
        agent = self.agent
        if not agent.messages or agent.messages[-1].get("extra", {}).get("exit_status") != "Submitted":
            raise ProtocolError("repair_session_not_submitted")
        proof = agent.env.completed_submission
        previous = agent.messages[-2]
        actions = previous.get("extra", {}).get("actions", [])
        if not proof or len(actions) != 1 or proof["action"] != actions[0] or not actions[0].get("tool_call_id"):
            raise ProtocolError("cannot_prove_completed_submit_action")
        # Save old trajectory before closing the actual completed call. Preserve
        # its exit internally; API view strips exit but includes this receipt.
        agent.save(agent.config.output_path)
        agent.add_messages({"role": "tool", "tool_call_id": actions[0]["tool_call_id"],
            "content": "The preceding submit command actually completed successfully. Prior stage submission: " + proof["message"].get("extra", {}).get("submission", ""),
            "extra": {"completed_submission_receipt": True}})
        agent.add_messages({"role": "user", "content": feedback})
        self.context = context
        agent.model.context = context
        agent.env.context = context
        agent.env.completed_submission = None
        agent.config.output_path = Path(context.store.home) / "runs" / context.identity.run_id / "revising-trajectory.json"
        agent._start_time = time.time()
        agent.config.wall_time_limit_seconds = max(1, math.ceil(context.deadline - time.monotonic()))
        agent.n_consecutive_format_errors = 0
        while True:
            try:
                context.check()
                agent.step()
                agent.n_consecutive_format_errors = 0
            except FormatError as error:
                agent.cost += error.messages[0].get("extra", {}).get("cost", 0)
                agent.n_consecutive_format_errors += 1
                agent.add_messages(*error.messages)
                if agent.n_consecutive_format_errors >= 3:
                    agent.add_messages({"role": "exit", "content": "RepeatedFormatError", "extra": {"exit_status": "RepeatedFormatError"}})
            except InterruptAgentFlow as error:
                agent.add_messages(*error.messages)
            finally:
                agent.save(agent.config.output_path)
            if agent.messages[-1].get("role") == "exit":
                return agent.messages[-1].get("extra", {})


def run_review(model, broker, context, output, prompt):
    messages = [{"role": "system", "content": "You independently review one immutable candidate. Use exactly one registered read-only tool per response. No shell, file writes, imports, network or arbitrary paths. Claims are unverified. Read the original requirements and diff, cite evidence_ids returned by tools. Do not invent IDs. Test acceptance against baseline is distinct from full_suite_all_passed: explicitly mention remaining failures and new failures; never claim all tests passed when full_suite_all_passed is false. Submit at most one review with schema_version=1; use insufficient_evidence if unable to decide."},
                {"role": "user", "content": prompt}]
    errors, repeated, last = 0, 0, None
    try:
        while True:
            context.check()
            message = None
            try:
                message = model.query(messages)
                messages.append(message)
                action = message["extra"]["actions"][0]
                result = broker.dispatch(context.identity, action["tool"], action["arguments"])
                messages.extend(model.format_observation_messages(message, [result]))
                refs = ([result["evidence"]] if result.get("evidence") else []) + [h["evidence"] for h in result.get("hits", [])]
                key = canonical_bytes([{k: r.get(k) for k in ("kind", "document_id", "start_line", "end_line", "span_sha256", "report_id", "report_sha256")} for r in refs]) if refs else canonical_bytes([action["tool"], action["arguments"]])
                repeated = repeated + 1 if key == last else 0
                last = key
                if repeated >= 3:
                    raise ProtocolError("review_repeated_evidence_limit")
                errors = 0
                if action["tool"] == "submit_review":
                    return next(iter(broker.reviews.values()))
            except (FormatError, ProtocolError) as error:
                from review_tools import IntegrityError, ReviewCancelled
                if isinstance(error, (IntegrityError, ReviewCancelled)) or repeated >= 3 or str(error).startswith("credential_"):
                    raise
                if isinstance(error, FormatError):
                    if model.contains_credentials(canonical_bytes(error.messages).decode()):
                        raise ProtocolError("credential_in_model_output") from None
                    messages.extend(error.messages)
                else:
                    # Close the actual rejected tool call, not a fake success.
                    if message is None:
                        raise
                    messages.extend(model.format_observation_messages(message, [{"status": "rejected", "reason": str(error)}]))
                errors += 1
                if errors >= 3:
                    raise ProtocolError("review_repeated_format_errors")
    finally:
        Path(output).write_bytes(canonical_bytes({"messages": messages, "usage": model.usage,
            "note": "Independent reviewer context; no bash tools or code execution."}))
