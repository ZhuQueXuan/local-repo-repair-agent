"""Persistent usage accounting with an optional project-wide monetary cap."""
from contextlib import contextmanager
from decimal import Decimal
import json
import os
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parent.parent


def load_budget_config():
    return json.loads((ROOT / "configs" / "deepseek_budget.json").read_text(encoding="utf-8"))


class BudgetExhausted(RuntimeError):
    pass


class SharedBudget:
    def __init__(self, config, *, ledger_path=None, temp_dir=None):
        self.config = config
        self.path = Path(ledger_path) if ledger_path else ROOT / config["ledger_path"]
        self.temp = Path(temp_dir) if temp_dir else ROOT / "temp"
        self.limit = Decimal(config["max_total_cost_cny"]) if config["max_total_cost_cny"] is not None else None
        self.reservation = Decimal(config["reservation_per_attempt_cny"])
        if (config["max_requests"] is not None or self.reservation <= 0
                or self.limit is not None and (not self.limit.is_finite() or self.limit <= 0)):
            raise ValueError("Expected an optional positive monetary cap without a request-count cap.")

    @property
    def agent_cost_limit(self):
        # Upstream DefaultAgent explicitly treats zero as disabling its cost cap.
        return float(self.limit) if self.limit is not None else 0.0

    @contextmanager
    def _locked(self):
        # A crash releases this OS lock; it does not release persisted reservations.
        self.temp.mkdir(parents=True, exist_ok=True)
        with (self.temp / "deepseek_budget.lock").open("a+b") as lock:
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                # Missing/corrupt accounting must never silently create a fresh allowance.
                data = json.loads(self.path.read_text(encoding="utf-8"))
                yield data
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _used(data):
        values = [data["historical_estimated_cost_cny"], data["historical_unknown_charge_reservation_cny"]]
        values += [row["amount_cny"] for row in data["requests"].values()]
        amounts = [Decimal(value) for value in values]
        if any(not amount.is_finite() or amount < 0 for amount in amounts):
            raise ValueError("Invalid budget ledger amount.")
        return sum(amounts, Decimal("0"))

    def _save(self, data):
        temporary = self.temp / ("budget_" + uuid.uuid4().hex + ".json")
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.path)

    def snapshot(self):
        with self._locked() as data:
            used = self._used(data)
            return {"max_requests": None, "max_total_cost_cny": float(self.limit) if self.limit is not None else None,
                    "accounted_cost_and_reservations_cny": float(used),
                    "known_price_estimate_cny": float(Decimal(data["historical_estimated_cost_cny"]) + sum(
                        (Decimal(row["amount_cny"]) for row in data["requests"].values() if row["status"] == "settled"), Decimal(0))),
                    "unpriced_usage_requests": sum(row["status"] == "unpriced" for row in data["requests"].values()),
                    "remaining_cny": float(max(Decimal("0"), self.limit - used)) if self.limit is not None else None,
                    "blocked": bool(data["blocked"]) if self.limit is not None else False}

    def reserve(self, *, provider=None):
        with self._locked() as data:
            used = self._used(data)  # Validate accounting even when there is no spending cap.
            if self.limit is not None and (data["blocked"] or used + self.reservation > self.limit):
                raise BudgetExhausted("Insufficient shared CNY budget for the next request.")
            ticket = uuid.uuid4().hex
            data["requests"][ticket] = {"status": "reserved", "amount_cny": str(self.reservation)}
            if provider is not None:
                data["requests"][ticket]["provider"] = provider
            self._save(data)  # Must complete before the HTTP call.
            return ticket

    def note_failure(self, ticket, failure):
        # No successful usage receipt: retain the placeholder, never call it a fee.
        with self._locked() as data:
            row = data["requests"][ticket]
            if row["status"] != "reserved":
                raise RuntimeError("Request is not awaiting a receipt.")
            row.update(outcome="provider_error", failure=failure,
                       amount_status="unknown_charge_placeholder")
            self._save(data)

    def record_unpriced(self, ticket, usage):
        if self.limit is not None:
            raise ValueError("Unknown platform pricing cannot satisfy a finite monetary cap.")
        with self._locked() as data:
            row = data["requests"][ticket]
            if row["status"] != "reserved":
                raise RuntimeError("Request already recorded.")
            row.update(status="unpriced", usage=usage, actual_cost_cny=None,
                       amount_status="unknown_price_placeholder_not_a_cost_estimate")
            self._save(data)

    def settle(self, ticket, cost):
        cost = Decimal(str(cost))
        if not cost.is_finite() or cost < 0:
            raise ValueError("Invalid response cost; retain reservation.")
        with self._locked() as data:
            row = data["requests"][ticket]
            if row["status"] != "reserved":
                raise RuntimeError("Request already settled.")
            exceeded = cost > Decimal(row["amount_cny"])
            row.update(status="settled", amount_cny=str(cost))
            if self.limit is not None:
                data["blocked"] = data["blocked"] or exceeded or self._used(data) > self.limit
            self._save(data)
            if self.limit is not None and data["blocked"]:
                raise BudgetExhausted("Provider usage exceeded reservation; further requests blocked.")
