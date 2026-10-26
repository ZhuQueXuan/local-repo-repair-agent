"""Offline budget regressions; synthetic accounting never calls a provider."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from deepseek_budget import BudgetExhausted, ROOT, SharedBudget, load_budget_config
from run_deepseek_smoke import BudgetedDeepSeekModel
from minisweagent.exceptions import LimitsExceeded


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / "temp" / "budget_tests" / uuid.uuid4().hex
        self.folder.mkdir(parents=True)
        self.path = self.folder / "ledger.json"
        self.seed()
        self.config = load_budget_config()
        self.config["max_total_cost_cny"] = "5.00"  # Existing tests retain the explicit historical boundary.
        self.budget = self.new_budget()

    def seed(self, used="0"):
        self.path.write_text(json.dumps({"historical_estimated_cost_cny": used,
            "historical_unknown_charge_reservation_cny": "0", "blocked": False, "requests": {}}), encoding="utf-8")

    def new_budget(self):
        return SharedBudget(dict(self.config), ledger_path=self.path, temp_dir=self.folder)

    def model(self, attempts=0, completed=False):
        task_path = self.folder / (uuid.uuid4().hex + "_task.json")
        task_path.write_text(json.dumps({"attempts": attempts, "completed": completed}), encoding="utf-8")
        return BudgetedDeepSeekModel({"DEEPSEEK_API_KEY": "offline-test",
            "DEEPSEEK_API_BASE_URL": "https://api.deepseek.com", "DEEPSEEK_CHAT_MODEL": "deepseek-flash"},
            ledger_path=task_path, error_path=self.folder / "error.json", budget=self.budget)

    def test_more_than_fifty_small_calls_and_restart_preserves_total(self):
        for _ in range(120):
            ticket = self.budget.reserve()
            self.budget.settle(ticket, Decimal("0.001"))
        self.assertEqual(self.new_budget().snapshot()["accounted_cost_and_reservations_cny"], 0.12)
        self.assertIsNone(self.budget.snapshot()["max_requests"])

    def test_last_call_cannot_cross_five_yuan(self):
        self.seed("4.90")
        ticket = self.budget.reserve()
        self.assertEqual(self.budget.snapshot()["remaining_cny"], 0)
        with self.assertRaises(BudgetExhausted):
            self.new_budget().reserve()
        self.budget.settle(ticket, Decimal("0.099999"))
        with self.assertRaises(BudgetExhausted):
            self.budget.reserve()
        self.assertEqual(self.budget.snapshot()["accounted_cost_and_reservations_cny"], 4.999999)

    def test_unknown_or_crashed_request_keeps_its_reservation(self):
        self.budget.reserve()
        self.assertEqual(self.new_budget().snapshot()["accounted_cost_and_reservations_cny"], 0.1)
        ticket = self.budget.reserve()
        with self.assertRaises(ValueError):
            self.budget.settle(ticket, Decimal("NaN"))
        self.assertEqual(self.budget.snapshot()["accounted_cost_and_reservations_cny"], 0.2)

    def test_authorized_top_up_preserves_spending_and_pending_reservations(self):
        self.seed("4.546950")
        data=json.loads(self.path.read_text())
        data['historical_unknown_charge_reservation_cny']='0.20'
        data['requests']['pending']={'status':'reserved','amount_cny':'0.10'}
        self.path.write_text(json.dumps(data))
        before=self.path.read_bytes()
        updated=dict(self.config,max_total_cost_cny='15.00')
        budget=SharedBudget(updated,ledger_path=self.path,temp_dir=self.folder)
        self.assertEqual(budget.snapshot()['remaining_cny'],10.15305)
        self.assertEqual(self.path.read_bytes(),before)
        ticket=budget.reserve()
        budget.settle(ticket,Decimal('0.001'))
        self.assertEqual(json.loads(self.path.read_text())['requests']['pending'],{'status':'reserved','amount_cny':'0.10'})

    def test_fifteen_yuan_boundary_still_stops_before_transport(self):
        self.seed('14.91')
        budget=SharedBudget(dict(self.config,max_total_cost_cny='15.00'),ledger_path=self.path,temp_dir=self.folder)
        with self.assertRaises(BudgetExhausted): budget.reserve()
        self.assertEqual(budget.snapshot()['accounted_cost_and_reservations_cny'],14.91)

    def test_unexpected_provider_charge_is_recorded_and_blocks_next_request(self):
        ticket = self.budget.reserve()
        with self.assertRaises(BudgetExhausted):
            self.budget.settle(ticket, Decimal("0.11"))
        self.assertTrue(self.new_budget().snapshot()["blocked"])
        with self.assertRaises(BudgetExhausted):
            self.budget.reserve()

    def test_missing_or_corrupt_ledger_does_not_reset_budget(self):
        absent = SharedBudget(self.config, ledger_path=self.folder / "absent.json", temp_dir=self.folder)
        with self.assertRaises(FileNotFoundError):
            absent.reserve()
        self.path.write_text("not json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.budget.reserve()

    def test_competing_requests_cannot_double_spend_the_balance(self):
        self.seed("4.80")
        def reserve_once(_):
            try:
                return self.new_budget().reserve()
            except (BudgetExhausted, OSError):
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            tickets = list(pool.map(reserve_once, range(40)))
        self.assertEqual(sum(ticket is not None for ticket in tickets), 2)
        self.assertEqual(self.budget.snapshot()["remaining_cny"], 0)

    def test_model_with_large_attempt_counter_still_calls_and_settles(self):
        model = self.model(attempts=999)
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=50))
        with patch("run_deepseek_smoke.LitellmModel._query", return_value=response) as transport:
            for _ in range(12):
                result = model._query([{"role": "user", "content": "hello"}])
                model._calculate_cost(result)
            self.assertEqual(transport.call_count, 12)
        self.assertEqual(model.ledger["attempts"], 1011)
        self.assertEqual(self.budget.snapshot()["accounted_cost_and_reservations_cny"], 0.0288)
        self.assertNotIn("offline-test", json.dumps(model.serialize()))

    def test_insufficient_money_stops_before_transport(self):
        self.seed("4.91")
        model = self.model()
        with patch("run_deepseek_smoke.LitellmModel._query") as transport:
            with self.assertRaises(LimitsExceeded) as stopped:
                model._query([{"role": "user", "content": "hello"}])
            self.assertEqual(stopped.exception.messages[0]["extra"]["limit_kind"], "cost")
            transport.assert_not_called()
        self.assertEqual(model.requests, 0)

    def test_closed_batches_and_large_context_still_stop_before_transport(self):
        closed = self.model(completed=True)
        opened = self.model()
        with patch("run_deepseek_smoke.LitellmModel._query") as transport:
            for model, content in ((closed, "hello"), (opened, "x" * 17000)):
                with self.assertRaises(LimitsExceeded) as stopped:
                    model._query([{"role": "user", "content": content}])
                self.assertEqual(stopped.exception.messages[0]["extra"]["limit_kind"],
                                 "completed_batch" if model is closed else "context_bytes")
            transport.assert_not_called()
        self.assertEqual(self.budget.snapshot()["accounted_cost_and_reservations_cny"], 0)

    def test_missing_usage_retains_reservation_and_prevents_model_retry(self):
        model = self.model()
        with patch("run_deepseek_smoke.LitellmModel._query", return_value=SimpleNamespace(usage=None)) as transport:
            response = model._query([{"role": "user", "content": "hello"}])
            with self.assertRaises(RuntimeError):
                model._calculate_cost(response)
            with self.assertRaises(RuntimeError):
                model._query([{"role": "user", "content": "hello"}])
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.new_budget().snapshot()["accounted_cost_and_reservations_cny"], 0.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
