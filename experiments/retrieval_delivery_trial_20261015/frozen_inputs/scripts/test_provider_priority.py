"""Priority/failover tests use isolated ledgers and mocked transport only."""
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from deepseek_budget import ROOT, SharedBudget, load_budget_config
from run_deepseek_smoke import BudgetedDeepSeekModel
from provider_priority import load_providers
from minisweagent.exceptions import InterruptAgentFlow, LimitsExceeded


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / "temp/provider_priority_tests" / uuid.uuid4().hex
        self.folder.mkdir(parents=True)
        self.path = self.folder / "shared.json"
        self.path.write_text(json.dumps({"historical_estimated_cost_cny": "12.00",
            "historical_unknown_charge_reservation_cny": "0.20", "blocked": False,
            "requests": {"old_unknown": {"status": "reserved", "amount_cny": "0.10"}}}), encoding="utf-8")
        self.config = load_budget_config() | {"max_total_cost_cny": None}
        self.budget = SharedBudget(self.config, ledger_path=self.path, temp_dir=self.folder)
        self.settings = {"BUPT_API_KEY": "primary-test-secret", "BUPT_API_BASE_URL": "https://myai.bupt.edu.cn/llm-gw/v1",
            "BUPT_CHAT_MODEL": "deepseek-v4-flash", "DEEPSEEK_API_KEY": "backup-test-secret",
            "DEEPSEEK_API_BASE_URL": "https://api.deepseek.com", "DEEPSEEK_CHAT_MODEL": "deepseek-flash"}
        self.messages = [{"role": "user", "content": "offline"}]

    def model(self, settings=None, budget=None):
        return BudgetedDeepSeekModel(self.settings if settings is None else settings,
            ledger_path=self.folder / (uuid.uuid4().hex + "_run.json"), error_path=self.folder / "errors.json",
            budget=self.budget if budget is None else budget)

    @staticmethod
    def response():
        return SimpleNamespace(model="mocked-model", usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=50))

    @staticmethod
    def error(code):
        error = RuntimeError("primary-test-secret backup-test-secret sk-other-secret provider failure")
        error.status_code = code
        return error

    def settle_query(self, model):
        response = model._query(self.messages)
        return model._calculate_cost(response)

    def test_primary_success_never_calls_backup_and_price_stays_unknown(self):
        model = self.model()
        with patch("run_deepseek_smoke.LitellmModel._query", return_value=self.response()) as transport:
            for _ in range(2):
                output = self.settle_query(model)
                self.assertFalse(output["cost_known"])
            self.assertEqual(transport.call_count, 2)
            self.assertTrue(all(call.kwargs["api_key"] == self.settings["BUPT_API_KEY"] for call in transport.call_args_list))
        self.assertEqual(model.unpriced_requests, 2)
        self.assertTrue(all(entry["estimated_cost_cny"] is None for entry in model.usage))
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["known_price_estimate_cny"], 12)
        self.assertEqual(snapshot["unpriced_usage_requests"], 2)
        self.assertEqual(snapshot["accounted_cost_and_reservations_cny"], 12.5)
        self.assertFalse(model.provider_statistics()["cost_estimate_complete"])
        self.assertIsNone(model.complete_cost_estimate())
        self.assertIsNone(model.serialize()["info"]["model_budget"]["estimated_actual_cost_cny"])

    def test_primary_auth_failure_falls_back_once_and_sticks_for_run(self):
        model = self.model()
        with patch("run_deepseek_smoke.LitellmModel._query", side_effect=[self.error(401), self.response(), self.response()]) as transport:
            for _ in range(2):
                self.settle_query(model)
            self.assertEqual([call.kwargs["api_key"] for call in transport.call_args_list],
                             [self.settings["BUPT_API_KEY"], self.settings["DEEPSEEK_API_KEY"], self.settings["DEEPSEEK_API_KEY"]])
        self.assertEqual(model.requests, 3)
        self.assertNotIn("provider_failure", model.ledger)
        self.assertNotIn("blocked_auth", model.ledger)
        self.assertEqual(len(model.ledger["provider_switches"]), 1)
        self.assertAlmostEqual(model.actual_cost_cny, 0.0048)
        self.assertEqual(self.budget.snapshot()["accounted_cost_and_reservations_cny"], 12.4048)

    def test_quota_permission_rate_server_errors_switch(self):
        for code in [402, 403, 404, 429, 500, 502, 503, 504]:
            with self.subTest(code=code):
                model = self.model()
                with patch("run_deepseek_smoke.LitellmModel._query", side_effect=[self.error(code), self.response()]) as transport:
                    self.settle_query(model)
                    self.assertEqual(transport.call_count, 2)
                self.assertEqual(model.provider_index, 1)

    def test_timeout_can_switch_without_claiming_key_is_invalid(self):
        model = self.model()
        with patch("run_deepseek_smoke.LitellmModel._query", side_effect=[TimeoutError("timeout"), self.response()]):
            self.settle_query(model)
        failure = model.ledger["provider_errors"][0]
        self.assertEqual(failure["kind"], "connection_failed")
        self.assertIn("不表示凭据", failure["notice"])

    def test_bad_request_does_not_switch_or_repeat(self):
        model = self.model()
        with patch("run_deepseek_smoke.LitellmModel._query", side_effect=self.error(400)) as transport:
            for _ in range(2):
                with self.assertRaises(InterruptAgentFlow):
                    model._query(self.messages)
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(model.provider_index, 0)

    def test_both_unavailable_stop_after_two_attempts(self):
        model = self.model()
        with patch("run_deepseek_smoke.LitellmModel._query", side_effect=[self.error(402), self.error(401)]) as transport:
            for _ in range(2):
                with self.assertRaises(InterruptAgentFlow) as stopped:
                    model._query(self.messages)
                self.assertEqual(stopped.exception.messages[0]["extra"]["exit_status"], "ProviderUnavailable")
            self.assertEqual(transport.call_count, 2)
        self.assertEqual(model.ledger["provider_failure"]["provider"], "deepseek_official")

    def test_new_run_tries_primary_again_without_resetting_old_accounting(self):
        first = self.model()
        with patch("run_deepseek_smoke.LitellmModel._query", side_effect=[self.error(402), self.response()]):
            self.settle_query(first)
        before = self.path.read_bytes()
        second = self.model()
        self.assertEqual(second.provider_index, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_both_keys_absent_from_config_errors_and_provider_repr(self):
        model = self.model()
        with patch("run_deepseek_smoke.LitellmModel._query", side_effect=[self.error(401), self.response()]):
            self.settle_query(model)
        text = json.dumps(model.serialize()) + model.ledger_path.read_text(encoding="utf-8") + (self.folder / "errors.json").read_text(encoding="utf-8") + repr(model.providers)
        self.assertFalse(model.contains_credentials(text))
        self.assertNotIn("sk-other-secret", text)

    def test_context_guard_precedes_all_transport_and_reservations(self):
        model = self.model()
        before = self.path.read_bytes()
        with patch("run_deepseek_smoke.LitellmModel._query") as transport:
            with self.assertRaises(LimitsExceeded):
                model._query([{"role": "user", "content": "x" * 17000}])
            transport.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_usage_does_not_retry_or_fallback(self):
        model = self.model()
        with patch("run_deepseek_smoke.LitellmModel._query", return_value=SimpleNamespace(usage=None)) as transport:
            with self.assertRaises(RuntimeError):
                self.settle_query(model)
            with self.assertRaises(RuntimeError):
                model._query(self.messages)
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(model.provider_index, 0)

    def test_unknown_pricing_cannot_evade_finite_budget(self):
        budget = SharedBudget(self.config | {"max_total_cost_cny": "15"}, ledger_path=self.path, temp_dir=self.folder)
        with self.assertRaises(ValueError):
            self.model(budget=budget)

    def test_no_primary_credential_preserves_official_only_compatibility(self):
        settings = {key: value for key, value in self.settings.items() if key != "BUPT_API_KEY"}
        model = self.model(settings)
        self.assertEqual([provider.name for provider in model.providers], ["deepseek_official"])

    def test_unsafe_backup_url_rejected_before_transport(self):
        for url in ["http://api.deepseek.com", "https://api.deepseek.com?credential=secret", "https://unrelated.example"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                load_providers(self.settings | {"DEEPSEEK_API_BASE_URL": url})

    def test_primary_url_and_model_come_from_env_fields(self):
        providers = load_providers(self.settings)
        self.assertEqual(providers[0].api_base, self.settings["BUPT_API_BASE_URL"])
        self.assertEqual(providers[0].model, self.settings["BUPT_CHAT_MODEL"])

    def test_missing_primary_url_or_model_is_a_clear_configuration_error(self):
        for field in ["BUPT_API_BASE_URL", "BUPT_CHAT_MODEL"]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "field is missing"):
                load_providers({key: value for key, value in self.settings.items() if key != field})

    def test_unapproved_primary_url_or_model_is_rejected(self):
        for change in [{"BUPT_API_BASE_URL": "https://other.example/v1"}, {"BUPT_API_BASE_URL": "http://myai.bupt.edu.cn/llm-gw/v1"},
                       {"BUPT_API_BASE_URL": "https://myai.bupt.edu.cn/llm-gw/v1/v1"}, {"BUPT_CHAT_MODEL": "other-model"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_providers(self.settings | change)

    def test_primary_omits_unsupported_thinking_and_backup_preserves_its_setting(self):
        model = self.model()
        self.assertNotIn("extra_body", model.config.model_kwargs)
        self.assertFalse(model.providers[0].public()["supports_thinking_control"])
        model.config.model_kwargs.update(max_tokens=128, timeout=20)
        observed = []
        def transport(*args, **kwargs):
            observed.append(dict(model.config.model_kwargs))
            if len(observed) == 1:
                raise self.error(401)
            return self.response()
        with patch("run_deepseek_smoke.LitellmModel._query", side_effect=transport):
            self.settle_query(model)
        self.assertNotIn("extra_body", observed[0])
        self.assertEqual(observed[1]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(observed[1]["max_tokens"], 128)
        self.assertEqual(observed[1]["timeout"], 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
