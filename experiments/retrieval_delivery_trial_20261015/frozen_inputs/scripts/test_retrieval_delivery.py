"""Delivery acknowledgement, immutable evidence and context regression checks."""
import ast
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_repository_retrieval as fixtures
from repository_retrieval import encoded_size
from run_task_batch import RecordedModel, BudgetedDeepSeekModel, ContextManagedModel
from context_budget import ContextBudget


class DeliveryTests(unittest.TestCase):
    setUp = fixtures.RetrievalTests.setUp
    write = fixtures.RetrievalTests.write
    retrieval = fixtures.RetrievalTests.retrieval

    def test_first_full_then_reference_only_after_ack(self):
        r = self.retrieval(delivery_policy="on_change")
        full = r.pinned_message()
        self.assertEqual(full, r.pinned_message())
        r.acknowledge_delivery()
        short = r.pinned_message()
        self.assertIn('"kind": "repository_retrieval_reference"', short["content"])
        self.assertLess(encoded_size([short]), encoded_size([full]))
        self.assertEqual(r.events[-1]["selected_hits"], r.events[0]["selected_hits"])

    def test_preview_never_acknowledges_or_replaces_pending(self):
        r = self.retrieval(delivery_policy="on_change")
        r.pinned_message(preview=True)
        r.acknowledge_delivery()
        self.assertIsNone(r.delivered_sha256)
        r.pinned_message()
        pending = dict(r.pending_delivery)
        r.pinned_message(preview=True)
        self.assertEqual(r.pending_delivery, pending)
        r.acknowledge_delivery()
        self.assertEqual(r.delivered_sha256, pending["snapshot_sha256"])

    def test_snapshot_recovers_exact_selected_source(self):
        r = self.retrieval(delivery_policy="on_change")
        r.pinned_message()
        event = r.events[-1]
        raw = (self.work / event["snapshot"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), event["snapshot_sha256"])
        self.assertEqual(json.loads(raw)["hits"], event["selected_hits"])
        for hit in json.loads(raw)["hits"]:
            lines = (self.work / hit["path"]).read_text(encoding="utf-8").splitlines()
            for snippet in hit["snippet"]:
                self.assertEqual(snippet["text"], lines[snippet["line"] - 1])

    def test_source_change_resends_full_and_keeps_old_snapshot(self):
        r = self.retrieval(delivery_policy="on_change")
        r.pinned_message()
        old = r.events[-1]
        saved = (self.work / old["snapshot"]).read_bytes()
        r.acknowledge_delivery()
        self.write("pkg/errors.py", "class DecodeFailure(ValueError):\n    def restore(self):\n        return 'updated'\n")
        r.pinned_message()
        current = r.events[-1]
        self.assertEqual(current["delivery_mode"], "full")
        self.assertEqual(current["delivery_reason"], "selected_evidence_changed")
        self.assertNotEqual(old["snapshot_sha256"], current["snapshot_sha256"])
        self.assertEqual((self.work / old["snapshot"]).read_bytes(), saved)
        r.acknowledge_delivery()
        r.pinned_message()
        self.assertEqual(r.events[-1]["delivery_mode"], "reference")

    def test_tampered_snapshot_stops_before_reference(self):
        r = self.retrieval(delivery_policy="on_change")
        r.pinned_message()
        r.acknowledge_delivery()
        self.write(r.events[-1]["snapshot"], "modified")
        with self.assertRaisesRegex(ValueError, "integrity"):
            r.pinned_message()

    def test_http_error_does_not_acknowledge(self):
        r = self.retrieval(delivery_policy="on_change")
        r.pinned_message()
        model = object.__new__(RecordedModel)
        model.repository_retrieval = r
        model.requests, model.request_measurements = 0, []
        with patch.object(BudgetedDeepSeekModel, "_query", side_effect=RuntimeError("offline transport failure")):
            with self.assertRaises(RuntimeError):
                model._query([])
        self.assertIsNone(r.delivered_sha256)
        r.pinned_message()
        self.assertEqual(r.events[-1]["delivery_mode"], "full")

    def test_successful_response_acknowledges_exact_pending_projection(self):
        r = self.retrieval(delivery_policy="on_change")
        r.pinned_message(preview=True)
        r.pinned_message()
        model = object.__new__(RecordedModel)
        model.repository_retrieval = r
        model.requests, model.request_measurements = 0, []
        response = object()
        with patch.object(BudgetedDeepSeekModel, "_query", return_value=response):
            self.assertIs(model._query([]), response)
        receipts = [json.loads(line) for line in (r.directory / "deliveries.jsonl").read_text().splitlines()]
        self.assertEqual(receipts[0]["projection"], 2)
        r.acknowledge_delivery()
        self.assertEqual(len((r.directory / "deliveries.jsonl").read_text().splitlines()), 1)

    def test_default_always_matches_old_payload_exactly(self):
        r = self.retrieval()
        first = r.pinned_message()
        r.acknowledge_delivery()
        self.assertEqual(first, r.pinned_message())
        old_path = Path(__file__).resolve().parent.parent / "experiments/repository_retrieval_validation_20261014/frozen_inputs/scripts/repository_retrieval.py"
        namespace = {"__name__": "frozen_retrieval"}
        exec(compile(old_path.read_bytes(), str(old_path), "exec"), namespace)
        old = namespace["RepositoryRetrieval"](self.work, self.files, source_prefix="pkg/", query=r.query)
        self.assertEqual(first, old.pinned_message())

    def test_search_and_index_implementation_unchanged(self):
        root = Path(__file__).resolve().parent.parent
        paths = [root / "scripts/repository_retrieval.py", root / "experiments/repository_retrieval_validation_20261014/frozen_inputs/scripts/repository_retrieval.py"]
        classes = [next(n for n in ast.parse(path.read_bytes()).body if isinstance(n, ast.ClassDef)) for path in paths]
        for name in ["_safe_source", "_parse", "refresh", "_neighbors", "search"]:
            nodes = [next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name) for cls in classes]
            self.assertEqual(ast.dump(nodes[0]), ast.dump(nodes[1]), name)

    def test_small_empty_payload_does_not_grow_into_reference(self):
        r = self.retrieval("the and to", delivery_policy="on_change")
        first = r.pinned_message()
        r.acknowledge_delivery()
        self.assertEqual(r.pinned_message(), first)
        self.assertEqual(r.events[-1]["delivery_reason"], "reference_not_smaller")

    def test_multibyte_messages_remain_bounded_after_ack(self):
        self.write("pkg/errors.py", 'class DecodeFailure:\n    value = "' + ('中文 ' * 200) + '"\n')
        r = self.retrieval(max_message_bytes=1024, delivery_policy="on_change")
        self.assertLessEqual(encoded_size([r.pinned_message()]), 1024)
        r.acknowledge_delivery()
        self.assertLessEqual(encoded_size([r.pinned_message()]), 1024)

    def test_reference_reservation_preserves_history_and_current_goal(self):
        model = object.__new__(ContextManagedModel)
        model.repository_retrieval = self.retrieval(delivery_policy="on_change")
        model.context_budget = ContextBudget(target_bytes=6000)
        messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": "original goal"},
                    {"role": "assistant", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c", "content": "observed"}]
        before = json.dumps(messages)
        full = model.project_context(messages, list(messages))
        model.repository_retrieval.acknowledge_delivery()
        short = model.project_context(messages, list(messages))
        self.assertEqual(json.dumps(messages), before)
        self.assertEqual(short[:2], messages[:2])
        self.assertEqual(short[-2:], messages[-2:])
        self.assertLess(encoded_size(short), encoded_size(full))

    def test_invalid_delivery_policy_rejected(self):
        with self.assertRaises(ValueError):
            self.retrieval(delivery_policy="guess")


if __name__ == "__main__":
    unittest.main(verbosity=2)
