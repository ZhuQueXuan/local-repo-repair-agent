"""Offline provider wire, all-role accounting, cancellation and clean replay."""
import os
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
import json
import unittest
from unittest.mock import patch

from document_agent_demo import exercise
from collaboration_panel import collaboration_view


class DocumentIntegrationTests(unittest.TestCase):
    def test_five_stages_cited_handoff_tools_and_accounting(self):
        question = "What is documented? Literal {{ 7 * 7 }} and {% if missing %}KEEP{% endif %} must remain data."
        store, runner, summary, wires = exercise(question=question)
        self.assertEqual(summary["run"]["status"], "finished")
        self.assertEqual(summary["run"]["repair_result"], "passed")
        self.assertEqual(summary["run"]["review_verdict"], "approve")
        roles = store.roles(summary["run_id"])
        self.assertEqual([r["phase"] for r in roles], ["consulting", "repairing", "reviewing", "revising", "final_review"])
        self.assertTrue(all(r["status"] == "succeeded" for r in roles))
        from collaboration_runtime import ProtectedReviewModel
        review_models = [m for m in runner.models if type(m) is ProtectedReviewModel]
        self.assertEqual(len(review_models), 2)
        self.assertTrue(all(m.budget_config['max_messages_utf8_bytes'] == 32768 for m in review_models))
        self.assertTrue(all(m.budget_config['max_messages_utf8_bytes'] == 16384 for m in runner.models if m not in review_models))
        self.assertEqual(runner.accounting.config['max_messages_utf8_bytes'], 16384)
        self.assertEqual(summary["counts"]["consulting"], 3)  # blocked bash, real read, cited submission
        for wire in wires:
            if wire["phase"] == "consulting":
                self.assertEqual(set(wire["tool_names"]), {"list_files", "read_lines", "search_text", "submit_consultation"})
                self.assertNotIn("OFFLINE_REPAIR_CONTEXT", json.dumps(wire["messages"]))
                data=json.loads(wire['messages'][1]['content'].split('\n',1)[1])
                issue=data['original_issue']
                from collaboration_protocol import sha256
                self.assertEqual(issue['sha256'],sha256(runner.issue))
                self.assertEqual(issue['text'],runner.issue[:2048].decode('utf-8',errors='ignore'))
                self.assertEqual(issue['complete'],len(runner.issue)<=2048)
            else:
                self.assertIn("document_tree_digest", json.dumps(wire["messages"]))
                if wire["phase"] == "repairing":
                    self.assertIn(question, json.dumps(wire["messages"]))
        self.assertEqual(len(summary["usage"]["by_role"]), 5)
        self.assertEqual(summary["usage"]["attempts"], sum(summary["counts"].values()))
        doc_usage = summary["usage"]["by_role"][roles[0]["id"]]
        self.assertEqual(doc_usage["attempts"], 3)
        view = collaboration_view(store, summary["run_id"])
        self.assertTrue(view["synthetic"])
        self.assertEqual(len(view["consultations"]), 1)
        self.assertTrue(view["consultations"][0]["excerpts"])
        final = runner.output / "patch-replay/report.json"
        self.assertTrue(json.loads(final.read_bytes())["task_tests_passed"])

    def test_document_phase_cancel_starts_no_model_or_repair(self):
        store, runner, summary, wires = exercise(cancel_phase="consulting")
        self.assertEqual(summary["run"]["status"], "cancelled")
        self.assertFalse(wires)
        self.assertEqual(summary["usage"]["attempts"], 0)
        self.assertTrue(all(not r.get("started") for r in store.roles(summary["run_id"])[1:]))

    def test_provider_failure_retains_document_usage_and_stops_later_roles(self):
        store, runner, summary, wires = exercise(scenario="provider_failure", attack_bash=False)
        self.assertEqual(summary["run"]["status"], "failed")
        self.assertEqual(set(summary["counts"]), {"consulting"})
        self.assertGreaterEqual(summary["usage"]["unknown_cost_attempts"], 1)
        self.assertIsNone(summary["usage"]["complete_cost_estimate_cny"])
        self.assertFalse(store.records(summary["run_id"], "consultation"))
        self.assertTrue(all(not r.get("started") for r in store.roles(summary["run_id"])[1:]))


if __name__ == "__main__":
    unittest.main()
