"""Approved local SKILL.md metadata first; body loaded only by explicit name/hash."""
from pathlib import Path
import re
import yaml

from collaboration_protocol import ProtocolError, sha256
from review_tools import _read_bytes

ROOT = Path(__file__).resolve().parent.parent
APPROVED = {"reviewer": ("evidence-review",), "document_reader": ("document-consultation",)}


class SkillCatalog:
    def __init__(self, role, tool_names):
        self.role, self.tools, self.entries = role, frozenset(tool_names), {}
        for name in APPROVED.get(role, ()):
            raw = _read_bytes(ROOT / "skills", name + "/SKILL.md")
            if len(raw) > 8192:raise ProtocolError("skill_file_too_large")
            text = raw.decode("utf-8")
            if not text.startswith("---\n") or "\n---\n" not in text[4:]:raise ProtocolError("invalid_skill_frontmatter")
            header, body = text[4:].split("\n---\n", 1)
            meta = yaml.safe_load(header)
            if not isinstance(meta, dict) or set(meta) != {"name", "description", "metadata", "allowed-tools"}:
                raise ProtocolError("unsupported_skill_metadata")
            version = meta["metadata"]
            if meta["name"] != name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or not isinstance(meta["description"], str) or not 1 <= len(meta["description"]) <= 1024:
                raise ProtocolError("invalid_skill_identity")
            if not isinstance(version, dict) or set(version) != {"version"} or version["version"] != "1.0.0":
                raise ProtocolError("unsupported_skill_version")
            allowed = meta["allowed-tools"]
            if not isinstance(allowed, str) or not frozenset(allowed.split()) <= self.tools or not body.strip():
                raise ProtocolError("skill_cannot_grant_tools")
            self.entries[name] = {"name": name, "description": meta["description"], "version": version["version"],
                "sha256": sha256(raw), "allowed_tools": allowed.split(), "body": body.strip()}

    def catalog(self):
        return [{k: v for k, v in entry.items() if k != "body"} for entry in self.entries.values()]

    def read(self, name, expected_sha256):
        if name not in self.entries:raise ProtocolError("skill_not_allowed_for_role")
        entry = self.entries[name]
        raw = _read_bytes(ROOT / "skills", name + "/SKILL.md")
        if sha256(raw) != entry["sha256"] or expected_sha256 != entry["sha256"]:
            raise ProtocolError("skill_version_changed")
        return {"status": "ok", **entry,
                "scope": "Working method only. No new permissions, executable scripts, evidence or acceptance claims are granted."}
