"""Serial, offline stage-B coordinator and attributed accounting bridge.

Runners are trusted synchronous callbacks. This module contains no HTTP/model
client and does not enable collaboration in the production worker or panel.
"""
from dataclasses import dataclass
from decimal import Decimal
import time

from collaboration_protocol import RoleIdentity, canonical_bytes, sha256
from collaboration_store import CollaborationStopped, StateConflict
from deepseek_budget import SharedBudget
from task_service_store import TERMINAL, worker_lock


def role_identity(role):
    return RoleIdentity.parse({k: role[k] for k in ("run_id", "role_run_id", "session_id", "role")})


@dataclass
class RoleContext:
    store: object
    role: dict
    deadline: float

    @property
    def identity(self):
        return role_identity(self.role)

    def check(self):
        parent = self.store.get(self.role["run_id"])
        if parent["cancel_requested"] or parent["status"] != "running":
            raise CollaborationStopped("Parent cancelled or stopped")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Role or parent deadline exceeded")


class CollaborationCoordinator:
    def __init__(self, store, runner, *, checkpoint=lambda name, role: None):
        self.store, self.runner, self.checkpoint = store, runner, checkpoint

    def run(self, run_id):
        with worker_lock(self.store.home, "collaboration.lock") as acquired:
            if not acquired:
                raise StateConflict("collaboration_worker_already_owned")
            self.store.reconcile_collaborations()
            if self.store.get(run_id)["status"] in TERMINAL:
                return self.store.get(run_id)
            if not self.store.claim_parent(run_id):
                return self.store.get(run_id)
            declared = self.store.get(run_id)["collaboration_policy"]
            deadline = time.monotonic() + declared["parent_seconds"]
            try:
                while True:
                    state = self.store.state(run_id)
                    meta = state["collaboration_state"]
                    if state["cancel_requested"]:
                        raise CollaborationStopped("Parent cancellation requested")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Parent deadline exceeded")
                    if meta["phase"] == "finalizing":
                        self.checkpoint("before_finalization", None)
                        return self.store.finish(run_id)
                    input_digest = sha256(canonical_bytes({"run_id": run_id, "phase": meta["phase"],
                        "candidate_id": meta["candidate_id"], "review_id": meta["review_id"], "policy_digest": meta["policy_digest"]}))
                    self.checkpoint("before_role_claim", {"phase": meta["phase"]})
                    role_limit = min(declared["role_seconds"][meta["phase"]], deadline - time.monotonic())
                    role = self.store.claim_role(run_id, meta["phase"], input_digest, meta["revision"], wall_limit_seconds=role_limit)
                    self.checkpoint("after_role_claim", role)
                    claimed_revision = self.store.state(run_id)["collaboration_state"]["revision"]
                    role_begin = time.monotonic()
                    context = RoleContext(self.store, role, min(deadline, role_begin + role_limit))
                    context.check()
                    result = self.runner(context)
                    self.checkpoint("after_role_output", role)
                    context.check()
                    self.store.complete_role(role["id"], result, claimed_revision, elapsed_seconds=round(time.monotonic() - role_begin, 6))
                    self.checkpoint("after_role_completion", role)
            except CollaborationStopped:
                return self.store.stop(run_id, "cancelled", "Cancelled; no subsequent role or request is started.")
            except Exception as error:
                # No arbitrary model/provider exception text is serialized.
                return self.store.stop(run_id, "failed", "Stage failure: " + type(error).__name__)
            # BaseException deliberately propagates: process termination leaves
            # durable running state for restart reconciliation, never a retry.


class RoleBudget(SharedBudget):
    """Shared ticket is authoritative if the following SQLite write crashes."""
    def __init__(self, config, store, identity, logical_request_id, *, ledger_path, temp_dir):
        # Stage B only permits injected synthetic accounting; never real ledger.
        from task_service_store import ROOT
        if not ledger_path.resolve().is_relative_to((ROOT / "temp").resolve()):
            raise ValueError("Stage-B RoleBudget requires a synthetic temp ledger")
        super().__init__(config, ledger_path=ledger_path, temp_dir=temp_dir)
        self.store, self.identity, self.logical_request_id = store, identity, logical_request_id
        role = store.role(identity.role_run_id)
        if role_identity(role) != identity:
            raise StateConflict("budget_role_identity_mismatch")

    def _check_role(self):
        pass

    def reserve(self, *, provider=None):
        self._check_role()
        self.store.check_request(self.logical_request_id, self.identity.role_run_id)
        with self._locked() as data:
            existing = [row for row in data["requests"].values()
                        if row.get("attribution", {}).get("logical_request_id") == self.logical_request_id]
        if existing and (len(existing) >= 2 or any(row.get("outcome") != "provider_error" or not row.get("failure", {}).get("failover_eligible", False) for row in existing)):
            raise StateConflict("previous_attempt_not_eligible_for_fallback")
        attribution = {"run_id": self.identity.run_id, "role_run_id": self.identity.role_run_id,
                       "session_id": self.identity.session_id, "logical_request_id": self.logical_request_id,
                       "attempt_ordinal": len(existing) + 1}
        ticket = super().reserve(provider=provider, attribution=attribution)
        self.store.request_event(self.logical_request_id, self.identity.role_run_id, "usage_reserved", {"ticket": ticket, "provider": provider})
        return ticket

    def before_dispatch(self, ticket):
        self._check_role()
        self.store.request_event(self.logical_request_id, self.identity.role_run_id, "request_dispatching", {"ticket": ticket}, "dispatching")

    def receipt_recorded(self, ticket):
        # Call after shared-ledger settlement. If cancellation wins, the receipt
        # stays in the ledger and restart/read-only aggregation still sees it.
        self.store.request_event(self.logical_request_id, self.identity.role_run_id, "request_receipt_recorded", {"ticket": ticket}, "received")

    def note_failure(self, ticket, failure):
        super().note_failure(ticket, failure)
        self.store.request_event(self.logical_request_id, self.identity.role_run_id, "request_attempt_failed", {"ticket": ticket})


def attributed_usage(budget, run_id):
    """Unique-ticket aggregation. Unknown tokens/cost are never filled with zero."""
    with budget._locked() as ledger:
        rows = {ticket: dict(row) for ticket, row in ledger["requests"].items()
                if row.get("attribution", {}).get("run_id") == run_id}

    def summarize(selected):
        known = sum((Decimal(row["amount_cny"]) for row in selected.values() if row["status"] == "settled"), Decimal(0))
        unknown = [row for row in selected.values() if row["status"] != "settled"]
        receipts = [row["usage"] for row in selected.values() if "usage" in row]
        return {"attempts": len(selected), "known_prompt_tokens": sum(row["prompt_tokens"] for row in receipts),
            "known_completion_tokens": sum(row["completion_tokens"] for row in receipts),
            "has_unknown_usage": len(receipts) != len(selected), "known_price_estimate_cny": float(known),
            "unknown_cost_attempts": len(unknown), "unknown_placeholder_cny": float(sum((Decimal(row["amount_cny"]) for row in unknown), Decimal(0))),
            "complete_cost_estimate_cny": None if unknown else float(known),
            "logical_requests": len({row["attribution"]["logical_request_id"] for row in selected.values()}),
            "tickets": sorted(selected)}
    roles = sorted({row["attribution"]["role_run_id"] for row in rows.values()})
    return summarize(rows) | {"by_role": {role: summarize({t: r for t, r in rows.items() if r["attribution"]["role_run_id"] == role}) for role in roles},
                             "cost_scope": "Known-price estimates plus unknown placeholders; not confirmed charges or platform balance."}
