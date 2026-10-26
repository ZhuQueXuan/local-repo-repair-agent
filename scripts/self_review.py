"""S+ phase transitions: one logical session, explicitly switched capabilities.

Phase role IDs remain distinct for permissions/accounting. They share one
session and chronological history; this is not independent review evidence.
No model/HTTP entry point or registration of caller-selected execution code.
"""
from copy import deepcopy
import json

from collaboration_protocol import ProtocolError, canonical_bytes, sha256

MODE = "same_session_readonly_self_review_v1"
SCOPE = "S+ one logical repair session with read-only self-check phases; not independent reviewer discovery or an effectiveness result."


def validate_self_policy(policy, profile):
    mode = policy.get("context_mode")
    if mode is None:
        return False
    if mode != MODE or profile != "adaptive_history" or policy.get("diagnostic_profile") is not None:
        raise ValueError("S+ requires its explicit context mode, adaptive_history and no manual diagnostic seed")
    if policy.get("feedback_windows") != 1:
        raise ValueError("S+ supports exactly one feedback window")
    return True


class SelfReviewSession:
    def __init__(self, repair_session):
        self.repair = repair_session
        self.pending = None
        self.completed = None
        self.events = []

    def _identity(self, context):
        original = self.repair.context.identity
        if (context.identity.run_id, context.identity.session_id) != (original.run_id, original.session_id):
            raise ProtocolError("self_review_session_identity_mismatch")
        context.check()

    def begin(self, context):
        self._identity(context)
        if context.role["phase"] not in {"reviewing", "final_review"} or self.pending:
            raise ProtocolError("self_review_phase_already_entered")
        if context.role["phase"] == "final_review" and not any(e["phase"] == "revising" for e in self.events):
            raise ProtocolError("self_review_revision_history_missing")
        history = self.repair.submitted_history()
        self.pending = {"phase": context.role["phase"], "role_id": context.identity.role_run_id,
                        "base_digest": sha256(canonical_bytes(history)), "base_length": len(history),
                        "original_system": deepcopy(history[0]), "original_history": deepcopy(history)}
        return deepcopy(history)

    def complete(self, context, history, review):
        self._identity(context)
        pending = self.pending
        if not pending or pending["role_id"] != context.identity.role_run_id:
            raise ProtocolError("self_review_not_started")
        base = pending["original_history"]
        # run_review changes only the active system capability instruction and
        # appends actual observations. Earlier task/actions may not be replaced.
        if history[1:len(base)] != base[1:] or len(history) <= len(base) + 2:
            raise ProtocolError("self_review_history_prefix_changed")
        issued = context.store.records(context.identity.run_id, "review").get(review.review_id)
        if issued != review:
            raise ProtocolError("self_review_not_registered")
        last, previous = history[-1], history[-2]
        actions = previous.get("extra", {}).get("actions", [])
        if len(actions) != 1 or actions[0].get("tool") != "submit_review" or last.get("role") != "tool" or last.get("tool_call_id") != actions[0].get("tool_call_id"):
            raise ProtocolError("self_review_submit_receipt_missing")
        receipt = json.loads(last["content"])
        if receipt.get("status") != "submitted" or receipt.get("review_id") != review.review_id or not receipt.get("provenance_validated"):
            raise ProtocolError("self_review_submit_receipt_invalid")
        self.completed = {**pending, "history": deepcopy(history), "history_digest": sha256(canonical_bytes(history)),
                          "review_id": review.review_id, "review_sha256": sha256(canonical_bytes(review))}
        self.events.append({"phase": pending["phase"], "session_id": context.identity.session_id,
            "phase_role_id": context.identity.role_run_id, "base_length": len(base), "history_length": len(history),
            "base_digest": pending["base_digest"], "history_digest": self.completed["history_digest"],
            "review_id": review.review_id, "scope": SCOPE})
        self.pending = None

    def resume(self, context, feedback):
        self._identity(context)
        done = self.completed
        if not done or done["phase"] != "reviewing" or context.role["phase"] != "revising":
            raise ProtocolError("self_review_completed_history_missing")
        if any(e["phase"] == "revising" for e in self.events):
            raise ProtocolError("self_review_history_already_consumed")
        if sha256(canonical_bytes(done["history"])) != done["history_digest"] or sha256(canonical_bytes(self.repair.submitted_history())) != done["base_digest"]:
            raise ProtocolError("self_review_history_changed")
        meta = context.store.state(context.identity.run_id)["collaboration_state"]
        review = context.store.records(context.identity.run_id, "review").get(done["review_id"])
        if meta["review_id"] != done["review_id"] or review is None or sha256(canonical_bytes(review)) != done["review_sha256"]:
            raise ProtocolError("self_review_active_report_mismatch")
        agent = self.repair.agent
        agent.save(agent.config.output_path)
        agent.messages = deepcopy(done["history"])
        agent.messages[0] = deepcopy(done["original_system"])
        self.events.append({"phase": "revising", "session_id": context.identity.session_id,
            "adopted_history_digest": done["history_digest"], "review_id": done["review_id"], "scope": SCOPE})
        return self.repair._continue(context, "The read-only SELF-CHECK phase ended. Restore the original bash repair capability.\n" + feedback)
