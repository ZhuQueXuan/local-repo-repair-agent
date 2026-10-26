"""Preparation packet boundaries, deterministic slots and independent red/green."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from collaboration_protocol import ProtocolError, canonical_bytes, sha256
from evaluation_preparation import (DEFAULT_PACKET, validate_plan, public_manifest, copy_public,
    apply_reference, verify_packet, qualify, check_qualification)
from review_tools import _read
from task_service_store import ROOT


def tiny_packet():
    root = ROOT / "temp/eval_preparation_tests" / uuid.uuid4().hex
    root.mkdir(parents=True)
    tasks, slots = [], []
    for i in range(6):
        task = "arithmetic-" + str(i)
        base = root / "public" / task
        files = {"pkg/__init__.py":b"", "pkg/calc.py":b"def total(xs): return max(0, sum(xs))\n",
                 "tests/test_upstream.py":b"from pkg.calc import total\ndef test_positive(): assert total([2,3]) == 5\n", "logo.png":b"\x89PNG\xff\x00"}
        for p, raw in files.items():
            target = base / "repo" / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        (base / "checks").mkdir()
        (base / "issue.md").write_text("Preserve the mathematical sum, including negative results.")
        public = b"import unittest\nfrom pkg.calc import total\nclass Cases(unittest.TestCase):\n def test_negative(self): self.assertEqual(total([-2,-3]),-5)\n"
        private = b"import unittest\nfrom pkg.calc import total\nclass Cases(unittest.TestCase):\n def test_negative(self): self.assertEqual(total([-10]),-10)\n"
        (base / "checks/test_behavior.py").write_bytes(public)
        manifest={"task_id":task,"scope":"new_public_issue_preparation_only","source_prefix":"pkg/","package":"pkg", "upstream_target":["tests/test_upstream.py"],
            "source_files_sha256":{p:sha256(raw) for p,raw in files.items()},
            "task_files_sha256":{p:sha256((base/p).read_bytes()) for p in ("issue.md","checks/test_behavior.py")}}
        (base / "manifest.json").write_bytes(canonical_bytes(manifest))
        authority=root / "evaluator" / task
        authority.mkdir(parents=True)
        (authority / "test_behavior.py").write_bytes(private)
        edits=[{"path":"pkg/calc.py","old":"return max(0, sum(xs))","new":"return sum(xs)","expected_occurrences":1}]
        (authority / "reference.json").write_bytes(canonical_bytes({"edits":edits}))
        (authority / "source.json").write_bytes(b"{}")
        tasks.append({"task":task,"repository":"repo-"+str(i%3),"splus_selected":i<3})
        for variant in (["S","D","Splus"] if i<3 else ["D","S"]):
            slots.append({"task":task,"variant":variant,"identity":task+"-"+variant,"repetition":1})
    (root / "plan.json").write_bytes(canonical_bytes({"schema_version":1,"state":"offline_prepared_not_launchable","paid_launch_authorized":False,
        "tasks":tasks,"slots":slots,"launch_blockers":["matching prompt","withheld terminal gate","batch lifecycle"]}))
    return root


def seal(root):
    hashes={p.relative_to(ROOT).as_posix():sha256(p.read_bytes()) for p in root.rglob("*") if p.is_file() and p.name!="freeze.json"}
    (root / "freeze.json").write_bytes(canonical_bytes({"version":1,"scope":"offline test packet only","files_sha256":hashes}))


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.packet=tiny_packet()

    def test_plan_has_six_tasks_three_repos_fifteen_unique_no_paid_authority(self):
        plan=json.loads((self.packet / "plan.json").read_bytes())
        self.assertEqual(validate_plan(plan),plan)
        self.assertEqual(len(plan["slots"]),15)
        for fields in ({"state":"ready"},{"paid_launch_authorized":True},{"launch_blockers":[]}):
            with self.subTest(fields=fields),self.assertRaises(ProtocolError): validate_plan(plan|fields)

    def test_duplicate_slot_unknown_task_or_missing_variant_is_rejected(self):
        plan=json.loads((self.packet / "plan.json").read_bytes())
        for fault in ("duplicate","unknown","missing"):
            bad=deepcopy(plan)
            if fault=="duplicate":bad["slots"][1]=bad["slots"][0]
            elif fault=="unknown":bad["slots"][0]["task"]="foreign"
            else:bad["slots"].pop()
            with self.subTest(fault=fault),self.assertRaises(ProtocolError):validate_plan(bad)

    def test_public_copy_omits_withheld_reference_and_full_manifest(self):
        target=ROOT / "temp/eval_preparation_tests" / uuid.uuid4().hex
        manifest=copy_public(self.packet,"arithmetic-0",target)
        expected=set(manifest["source_files_sha256"])|{"TASK.md","task_checks/test_behavior.py"}
        actual={p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
        self.assertEqual(actual,expected)
        self.assertNotIn(b"[-10]",(target / "task_checks/test_behavior.py").read_bytes())
        self.assertEqual((target / "logo.png").read_bytes(),b"\x89PNG\xff\x00")

    def test_traversal_and_non_temp_qualification_paths_are_rejected(self):
        for task in ("../arithmetic-0","arithmetic-0/.env", "C:bad"):
            with self.subTest(task=task),self.assertRaises(ProtocolError):public_manifest(self.packet,task)
        with self.assertRaises(ValueError):qualify(self.packet,"arithmetic-0",ROOT / "evaluation/forbidden-test-output")
        self.assertFalse((ROOT / "evaluation/forbidden-test-output").exists())

    def test_reference_cannot_change_upstream_tests_or_ignore_wrong_base(self):
        target=ROOT / "temp/eval_preparation_tests" / uuid.uuid4().hex
        manifest=copy_public(self.packet,"arithmetic-0",target)
        for edit in ({"path":"tests/test_upstream.py","old":"assert","new":"pass","expected_occurrences":1},
                     {"path":"pkg/calc.py","old":"wrong old source","new":"pass","expected_occurrences":1}):
            with self.subTest(edit=edit),self.assertRaises(ProtocolError):apply_reference(target,manifest,[edit])

    def test_actual_red_green_and_upstream_in_isolated_child(self):
        result=qualify(self.packet,"arithmetic-0")
        self.assertFalse(result["baseline"]["public"]["successful"])
        self.assertFalse(result["baseline"]["withheld"]["successful"])
        self.assertTrue(result["reference"]["public"]["successful"])
        self.assertTrue(result["reference"]["withheld"]["successful"])
        self.assertEqual(result["reference"]["upstream"]["passed"],1)
        self.assertEqual(result["model_calls"],0)
        self.assertTrue(result["protected_inputs_unchanged"])
        result["baseline"]["public"]["successful"]=True
        with self.assertRaisesRegex(ProtocolError,"baseline_did_not_reproduce"):check_qualification(result)

    def test_unfrozen_packet_never_claims_verified(self):
        with self.assertRaisesRegex(ProtocolError,"not_frozen"):verify_packet(self.packet)

    def test_private_mutation_is_rejected_before_child_execution(self):
        seal(self.packet)
        self.assertFalse(verify_packet(self.packet)["launchable"])
        path=self.packet / "evaluator/arithmetic-0/test_behavior.py"
        path.write_bytes(path.read_bytes()+b"# later change\n")
        with patch("evaluation_preparation.subprocess.run") as launch,self.assertRaises(RuntimeError):qualify(self.packet,"arithmetic-0")
        launch.assert_not_called()

    def test_omitting_private_inputs_from_freeze_is_rejected(self):
        seal(self.packet)
        p=self.packet / "freeze.json";value=json.loads(p.read_bytes())
        key=next(k for k in value["files_sha256"] if "/evaluator/" in k)
        value["files_sha256"].pop(key);p.write_bytes(canonical_bytes(value))
        with self.assertRaisesRegex(ProtocolError,"omits_packet"):verify_packet(self.packet)

    def test_evaluator_material_cannot_be_added_to_public_packet(self):
        p=self.packet / "public/arithmetic-0/answer.py";p.write_text("not a public input")
        seal(self.packet)
        with self.assertRaisesRegex(ProtocolError,"unexpected_public"):verify_packet(self.packet)

    def test_public_check_and_binary_tampering_are_rejected(self):
        for relative in ("checks/test_behavior.py","repo/logo.png"):
            packet=tiny_packet();p=packet / "public/arithmetic-0" / relative
            p.write_bytes(p.read_bytes()+b"change")
            with self.subTest(relative=relative),self.assertRaises(ProtocolError):public_manifest(packet,"arithmetic-0")

    def test_current_snapshot_text_reader_limitation_remains_explicit(self):
        # This preparation tool preserves binary bytes, but the existing review
        # kernel cannot read them as documents. Do not mark D/S+ launch-ready.
        with self.assertRaises(UnicodeDecodeError):_read(self.packet / "public/arithmetic-0/repo","logo.png")
        self.assertIn("not_launchable",json.loads((self.packet / "plan.json").read_bytes())["state"])

    def test_pytest_subprocess_has_its_own_network_guard(self):
        base=self.packet / "public/arithmetic-0"
        target=base / "repo/tests/test_upstream.py"
        target.write_bytes(target.read_bytes()+b"import socket,pytest\ndef test_no_dns_or_network():\n with pytest.raises(RuntimeError,match='forbids network'): socket.getaddrinfo('example.invalid',443)\n")
        manifest=json.loads((base / "manifest.json").read_bytes())
        manifest["source_files_sha256"]["tests/test_upstream.py"]=sha256(target.read_bytes())
        (base / "manifest.json").write_bytes(canonical_bytes(manifest))
        result=qualify(self.packet,"arithmetic-0")
        self.assertEqual(result["baseline"]["upstream"]["passed"],2)
        self.assertEqual(result["reference"]["upstream"]["passed"],2)


if __name__=="__main__":unittest.main(verbosity=2)
