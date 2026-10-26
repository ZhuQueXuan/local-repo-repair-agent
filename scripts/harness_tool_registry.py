"""Explicit tool definitions; registration never grants filesystem authority."""
from dataclasses import dataclass
from types import MappingProxyType

from collaboration_protocol import ProtocolError, canonical_bytes, sha256


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: type
    description: str
    roles: frozenset
    effect: str = "read"
    output_bytes: int = 98304
    transport: str = "local"


class ToolRegistry:
    def __init__(self, specs, role):
        items = {}
        for spec in specs:
            if spec.name in items or spec.effect not in {"read", "submit"} or role not in spec.roles:
                raise ProtocolError("invalid_explicit_tool_registration")
            items[spec.name] = spec
        self.specs = MappingProxyType(items)
        self.schemas = MappingProxyType({name: spec.schema for name, spec in items.items()})
        self.role = role

    def parse(self, name, arguments):
        if not isinstance(name, str) or name not in self.specs:
            raise ProtocolError("tool_not_registered_for_role")
        if not isinstance(arguments, dict) or len(canonical_bytes(arguments)) > 32768:
            raise ProtocolError("registry_arguments_too_large")
        return self.specs[name].schema.parse(arguments)

    def api_tools(self):
        return [{"type": "function", "function": {"name": s.name, "description": s.description,
                "parameters": s.schema.model_json_schema()}} for s in self.specs.values()]

    def catalog(self):
        return [{"name": s.name, "effect": s.effect, "transport": s.transport,
                 "output_limit_bytes": s.output_bytes,
                 "schema_sha256": sha256(canonical_bytes(s.schema.model_json_schema()))} for s in self.specs.values()]


def existing_registry(schemas, role):
    return [ToolSpec(name, schema, "Submit a version-bound claim; not a program acceptance" if name.startswith("submit_")
                    else "Read registered immutable evidence; no shell, network or writes", frozenset({role}),
                    "submit" if name.startswith("submit_") else "read") for name, schema in schemas.items()]
