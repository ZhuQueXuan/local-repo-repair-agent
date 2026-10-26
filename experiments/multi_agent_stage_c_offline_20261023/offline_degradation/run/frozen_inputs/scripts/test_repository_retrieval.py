"""Offline retrieval provenance, freshness, context and boundary checks."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
import uuid

sys.dont_write_bytecode = True
from repository_retrieval import RepositoryRetrieval, tokens, encoded_size
from context_budget import ContextBudget
from run_task_batch import ContextManagedModel

ROOT = Path(__file__).resolve().parent.parent


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "temp/retrieval_tests" / uuid.uuid4().hex
        (self.work / "pkg").mkdir(parents=True)
        self.write("pkg/errors.py", "class DecodeFailure(ValueError):\n    def restore(self):\n        return 'payload'\n")
        self.write("pkg/response.py", "from .errors import DecodeFailure\n\nclass Response:\n    def decode(self, payload):\n        raise DecodeFailure(payload)\n")
        self.write("pkg/other.py", "def unrelated():\n    return 42\n")
        self.files = ["pkg/errors.py", "pkg/response.py", "pkg/other.py"]

    def write(self, relative, content):
        path = self.work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def retrieval(self, query="DecodeFailure decode payload", **options):
        return RepositoryRetrieval(self.work, self.files, source_prefix="pkg/", query=query, **options)

    def test_camel_and_underscore_tokens(self):
        self.assertEqual(tokens("JSONDecodeError decode_failure"), {"json", "decode", "failure"})

    def test_symbol_path_line_and_hash_are_real(self):
        report = self.retrieval("DecodeFailure").search()
        hit = report["hits"][0]
        self.assertEqual(hit["path"], "pkg/errors.py")
        self.assertEqual(hit["symbol"], "DecodeFailure")
        self.assertEqual(hit["sha256"], hashlib.sha256((self.work / hit["path"]).read_bytes()).hexdigest())
        self.assertEqual(hit["start"], 1)

    def test_relative_import_neighbors_are_hints(self):
        report = self.retrieval().search()
        response = next(h for h in report["hits"] if h["path"] == "pkg/response.py")
        self.assertIn("pkg/errors.py", response["import_neighbors"])

    def test_different_files_get_coverage_before_second_symbol(self):
        report = self.retrieval(max_results=2).search()
        self.assertEqual({h["path"] for h in report["hits"]}, {"pkg/errors.py", "pkg/response.py"})

    def test_refresh_detects_same_size_rewrite(self):
        retriever = self.retrieval("before after")
        self.write("pkg/other.py", "def before():\n    return 1\n")
        before = retriever.search()
        self.write("pkg/other.py", "def after_():\n    return 1\n")
        after = retriever.search()
        self.assertNotEqual(next(h["sha256"] for h in before["hits"] if h["path"] == "pkg/other.py"),
                            next(h["sha256"] for h in after["hits"] if h["path"] == "pkg/other.py"))
        self.assertIn("pkg/other.py", after["refreshed_files"])
        self.assertFalse(retriever.search()["refreshed_files"])

    def test_half_written_syntax_remains_searchable(self):
        self.write("pkg/errors.py", "class DecodeFailure(\n")
        hit = next(h for h in self.retrieval().search()["hits"] if h["path"] == "pkg/errors.py")
        self.assertTrue(hit["syntax_fallback"])

    def test_missing_file_drops_old_hits(self):
        retriever = self.retrieval()
        retriever.search()
        (self.work / "pkg/errors.py").rename(self.work / "pkg/moved.py")
        report = retriever.search()
        self.assertEqual(report["removed_from_index"], ["pkg/errors.py"])
        self.assertNotIn("pkg/errors.py", [h["path"] for h in report["hits"]])

    def test_no_import_or_source_execution(self):
        self.write("pkg/other.py", "raise RuntimeError('never execute')\n\ndef DecodeFailure():\n    pass\n")
        self.assertTrue(self.retrieval().search()["hits"])

    def test_checks_env_answers_and_new_files_are_excluded(self):
        for name in ["task_checks/test_behavior.py", "checks/answers.py", "pkg/.env", "pkg/new.py", "pkg/private.txt"]:
            self.write(name, "def SecretAnswer():\n    pass\n")
        self.files.extend(["task_checks/test_behavior.py", "checks/answers.py", "pkg/.env", "pkg/private.txt"])
        self.assertEqual(self.retrieval("SecretAnswer").search()["hits"], [])

    def test_parent_and_absolute_paths_are_rejected(self):
        for name in ["../outside.py", str(ROOT / "scripts/provider_priority.py")]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                RepositoryRetrieval(self.work, [name], source_prefix="pkg", query="x")
        with self.assertRaises(ValueError):
            RepositoryRetrieval(self.work, [], source_prefix="../", query="x")

    def test_file_size_cap_is_explicit(self):
        report = self.retrieval(max_file_bytes=10).search()
        self.assertEqual(report["indexed_files"], 0)
        self.assertEqual({s["reason"] for s in report["skipped"]}, {"file_byte_limit"})

    def test_bounded_message_with_multibyte_quoted_source(self):
        self.write("pkg/errors.py", 'class DecodeFailure:\n    value = "' + ('中文 \\"' * 120) + '"\n')
        retriever = self.retrieval(max_message_bytes=1024)
        message = retriever.pinned_message()
        self.assertLessEqual(encoded_size([message]), 1024)
        self.assertIn("untrusted source text", message["content"])
        self.assertTrue((retriever.directory / "0001.json").is_file())

    def test_deterministic_search_and_empty_query(self):
        retriever = self.retrieval()
        first, second = retriever.search(), retriever.search()
        self.assertEqual(first["hits"], second["hits"])
        self.assertEqual(self.retrieval("the and to").search()["hits"], [])

    def test_context_reservation_keeps_goal_state_and_tool_pairs(self):
        model = object.__new__(ContextManagedModel)
        model.repository_retrieval = self.retrieval()
        model.context_budget = ContextBudget(target_bytes=6000)
        original = [{"role": "system", "content": "policy"}, {"role": "user", "content": "original issue"},
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c1", "content": "observed"}]
        state = {"role": "user", "content": "program state"}
        before = json.dumps(original)
        projected = model.project_context(original, list(original), [state])
        self.assertEqual(projected[:2], original[:2])
        self.assertEqual(projected[2], state)
        self.assertEqual(projected[-2:], original[-2:])
        self.assertLessEqual(encoded_size(projected), 6000)
        self.assertGreater(model.context_budget.events[-1]["reserved_bytes"], 0)
        self.assertEqual(json.dumps(original), before)

    def test_disabled_does_not_add_retrieval_message(self):
        model = object.__new__(ContextManagedModel)
        model.repository_retrieval = None
        model.context_budget = ContextBudget(target_bytes=6000)
        messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": "goal"}]
        self.assertEqual(model.project_context(messages, messages), messages)

    def test_invalid_policy_is_rejected(self):
        for options in [{"max_message_bytes": 100}, {"max_results": 0}, {"max_file_bytes": 0}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.retrieval(**options)


if __name__ == "__main__":
    unittest.main(verbosity=2)
