"""Read-only tool model using the existing protected/attributed request path."""
import json
import time

from run_deepseek_smoke import BudgetedDeepSeekModel

import litellm
from minisweagent.exceptions import FormatError

from collaboration_protocol import canonical_bytes
from review_tools import SCHEMAS


REVIEW_TOOLS = tuple({"type": "function", "function": {
    "name": name, "description": "Read registered snapshot evidence" if name != "submit_review" else "Submit the version-bound review; no file write",
    "parameters": schema.model_json_schema()}} for name, schema in SCHEMAS.items())


class ReviewModel(BudgetedDeepSeekModel):
    tool_schemas = SCHEMAS

    def _perform_completion(self, messages, provider):
        self.request_check()
        remaining = self.request_deadline() - time.monotonic() if self.request_deadline else 45
        if remaining <= 0:
            raise TimeoutError("No review request time remaining")
        options = dict(self.config.model_kwargs, timeout=min(45, remaining))
        tools = self.registered_tools.api_tools() if hasattr(self, "registered_tools") else list(REVIEW_TOOLS) if self.tool_schemas is SCHEMAS else [
            {"type": "function", "function": {"name": name,
             "description": "Read frozen documentation or submit a cited interpretation; no execution",
             "parameters": schema.model_json_schema()}} for name, schema in self.tool_schemas.items()]
        request = self.protocol_adapter.prepare(messages,tools,model=self.config.model_name,api_key=self._api_key,**options)
        return litellm.completion(**request)

    def _parse_actions(self, response):
        calls = response.choices[0].message.tool_calls or []
        reason = "invalid_arguments"
        try:
            if len(calls) != 1:
                reason = "expected_one_tool_call"
                raise ValueError("Use exactly one read-only tool call per response")
            call = calls[0]
            if call.function.name not in self.tool_schemas or not call.id:
                reason = "unregistered_tool_or_missing_id"
                raise ValueError("Only this role's explicitly registered tools are permitted")
            reason = "invalid_json_arguments"
            arguments = json.loads(call.function.arguments)
            reason = "arguments_too_large"
            if len(canonical_bytes(arguments)) > 32768:
                raise ValueError("Review arguments exceed the bounded request size")
            reason = "invalid_schema_arguments"
            self.tool_schemas[call.function.name].parse(arguments)
        except (ValueError, TypeError, KeyError) as error:
            # RoleModelHooks checks the full response for credentials before
            # entering this parser. Keep a bounded index into the raw response
            # already retained by upstream FormatError accounting.
            audit = {"schema_version": 1, "reason": reason, "error_type": type(error).__name__,
                     "call_count": len(calls), "calls": [], "truncated": len(calls) > 2}
            from collaboration_protocol import sha256
            for call in calls[:2]:
                arguments = call.function.arguments or ""
                audit["calls"].append({"id": (call.id or "")[:80], "tool": (call.function.name or "")[:80],
                    "arguments_sha256": sha256(arguments.encode()), "arguments_preview": arguments[:160],
                    "arguments_truncated": len(arguments) > 160})
            while len(canonical_bytes(audit)) > 2048:
                audit["truncated"] = True
                for item in audit["calls"]:
                    for key in ("id", "tool", "arguments_preview"):
                        item[key] = item[key][:len(item[key]) // 2]
                    item["arguments_truncated"] = True
            raise FormatError({"role": "user", "content": "Invalid review tool call: " + reason + ". Use exactly one registered tool with valid schema.",
                               "extra": {"interrupt_type": "FormatError", "review_rejection": audit}}) from None
        return [{"tool": call.function.name, "arguments": arguments, "tool_call_id": call.id}]

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "tool", "tool_call_id": action["tool_call_id"], "content": canonical_bytes(output).decode("utf-8")}
                for action, output in zip(message["extra"]["actions"], outputs)]
