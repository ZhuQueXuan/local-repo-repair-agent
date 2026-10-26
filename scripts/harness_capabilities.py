"""Opt-in read-only capabilities around the existing trusted evidence brokers."""
from pathlib import Path

from collaboration_protocol import Digest, Identifier, Message, ProtocolError, canonical_bytes, sha256
from harness_tool_registry import ToolSpec, ToolRegistry, existing_registry
from harness_skills import SkillCatalog
from review_tools import IntegrityError, ReviewCancelled, _read_bytes

ROOT = Path(__file__).resolve().parent.parent


class ReadSkill(Message):
    skill: Identifier
    sha256: Digest


class ReadChangeSummary(Message):
    candidate_id: Identifier


class CapabilityBroker:
    def __init__(self, broker, context, output, frozen_inputs=None):
        self.broker, self.context, self.output = broker, context, Path(output)
        paths = ["scripts/" + name for name in ("harness_capabilities.py", "harness_tool_registry.py", "harness_skills.py", "harness_mcp_client.py", "harness_mcp_server.py")]
        paths += ["skills/" + name + "/SKILL.md" for name in ("evidence-review", "document-consultation")]
        self.frozen = {name: frozen_inputs[name] if frozen_inputs is not None else sha256(_read_bytes(ROOT, name)) for name in paths}
        self.verify_bundle()
        role = context.identity.role
        if role == "document_reader":
            from document_agent import DOCUMENT_SCHEMAS
            schemas = DOCUMENT_SCHEMAS
        elif role == "reviewer":
            from review_tools import SCHEMAS
            schemas = SCHEMAS
        else:raise ProtocolError("capability_role_not_supported")
        specs = existing_registry(schemas, role)
        specs.append(ToolSpec("read_skill", ReadSkill, "Load one approved working-method skill by exact name and catalog hash. No additional permissions.", frozenset({role}), output_bytes=12288))
        if role == "reviewer":
            specs.append(ToolSpec("read_change_summary", ReadChangeSummary,
                "Read a local MCP navigation summary for this immutable diff. Not citable evidence; use read_diff/read_lines for citations.",
                frozenset({role}), output_bytes=16384, transport="mcp_stdio"))
        self.registry = ToolRegistry(specs, role)
        self.skills = SkillCatalog(role, self.registry.schemas)
        self.receipts = []

    def verify_bundle(self):
        if any(sha256(_read_bytes(ROOT, name)) != expected for name, expected in self.frozen.items()):
            raise IntegrityError("capability_bundle_changed")

    def __getattr__(self, name):
        return getattr(self.broker, name)

    def prompt_catalog(self):
        return {"version": 1, "tools": self.registry.catalog(), "skills": self.skills.catalog(),
                "note": "Skills are optional methods. Load read_skill only when useful, using the exact catalog hash. Skills/MCP summaries grant no permissions, evidence IDs or test acceptance."}

    def bind_model(self, model):
        model.tool_schemas = self.registry.schemas
        model.registered_tools = self.registry

    def dispatch(self, identity, tool_name, arguments):
        receipt = {"seq": len(self.receipts) + 1, "run_id": self.context.identity.run_id,
            "role_run_id": self.context.identity.role_run_id, "phase": self.context.role["phase"],
            "tool": tool_name if isinstance(tool_name, str) and tool_name in self.registry.schemas else "unknown", "outcome": "rejected"}
        try:
            self.context.check()
            self.verify_bundle()
            if identity != self.context.identity:raise ProtocolError("registry_caller_identity_mismatch")
            if self.broker.cancelled():raise ReviewCancelled("capability_cancelled")
            if getattr(self.broker, "_blocked", False) or getattr(self.broker, "blocked", False):
                raise IntegrityError("capability_integrity_blocked")
            request = self.registry.parse(tool_name, arguments)
            receipt["arguments_sha256"] = sha256(canonical_bytes(arguments))
            spec = self.registry.specs[tool_name]
            receipt.update(effect=spec.effect, transport=spec.transport)
            self.broker.snapshot.verify()
            if tool_name in {"read_skill", "read_change_summary"}:
                if getattr(self.broker, "advice", None) is not None or getattr(self.broker, "reviews", {}):
                    raise ProtocolError("capability_role_already_submitted")
                if tool_name == "read_skill":
                    result = self.skills.read(request.skill, request.sha256)
                    receipt["skill_sha256"] = result["sha256"]
                else:
                    candidate = self.broker.candidate
                    if request.candidate_id != candidate.candidate_id:raise ProtocolError("candidate_identity_mismatch")
                    doc = next(d for d in candidate.documents if d.side == "diff")
                    self.broker._doc(doc.document_id)  # Retain the exact original handoff authorization.
                    patch = self.broker.snapshot.read(doc.document_id)
                    from harness_mcp_client import change_overview
                    result, transport = change_overview({"candidate_id": candidate.candidate_id,
                        "source_tree_digest": candidate.source_tree_digest, "patch_sha256": candidate.patch_sha256,
                        "patch": patch.decode("utf-8")}, self.context.check, self.context.deadline,
                        expected_server_sha256=self.frozen["scripts/harness_mcp_server.py"])
                    receipt["mcp"] = transport
            else:
                result = self.broker.dispatch(identity, tool_name, arguments)
            self.broker.snapshot.verify()
            self.verify_bundle()
            self.context.check()
            if self.broker.cancelled():raise ReviewCancelled("capability_cancelled")
            raw = canonical_bytes(result)
            if len(raw) > spec.output_bytes:raise ProtocolError("registered_tool_output_limit")
            receipt.update(outcome="finished", output_bytes=len(raw), output_sha256=sha256(raw))
            return result
        except Exception as error:
            receipt["reason"] = type(error).__name__
            if isinstance(error, (IntegrityError, OSError)):
                if hasattr(self.broker, "_blocked"):self.broker._blocked = True
                else:self.broker.blocked = True
            raise
        finally:
            self.receipts.append(receipt)
            self.output.write_bytes(canonical_bytes({"registry": self.prompt_catalog(), "receipts": self.receipts}))
            self.context.store.event(self.context.identity.run_id, "registered_tool_receipt", receipt, self.context.identity.role)


def configure_capabilities(broker, context, output, frozen_inputs):
    if context.store.get(context.identity.run_id).get("harness_capabilities_version") != 1:
        return broker
    return CapabilityBroker(broker, context, output, frozen_inputs)
