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
    def _perform_completion(self, messages, provider):
        self.request_check()
        remaining = self.request_deadline() - time.monotonic() if self.request_deadline else 45
        if remaining <= 0:
            raise TimeoutError("No review request time remaining")
        options = dict(self.config.model_kwargs, timeout=min(45, remaining))
        return litellm.completion(model=self.config.model_name, messages=messages,
                                  tools=list(REVIEW_TOOLS), api_key=self._api_key, **options)

    def _parse_actions(self, response):
        calls = response.choices[0].message.tool_calls or []
        try:
            if len(calls) != 1:
                raise ValueError("Use exactly one read-only tool call per response")
            call = calls[0]
            if call.function.name not in SCHEMAS or not call.id:
                raise ValueError("Only the six registered review tools are permitted")
            arguments = json.loads(call.function.arguments)
            if len(canonical_bytes(arguments)) > 32768:
                raise ValueError("Review arguments exceed the bounded request size")
            SCHEMAS[call.function.name].parse(arguments)
        except (ValueError, TypeError, KeyError) as error:
            raise FormatError({"role": "user", "content": "Invalid review tool call: " + type(error).__name__ + ". Use one registered tool with valid schema.",
                               "extra": {"interrupt_type": "FormatError"}}) from None
        return [{"tool": call.function.name, "arguments": arguments, "tool_call_id": call.id}]

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "tool", "tool_call_id": action["tool_call_id"], "content": canonical_bytes(output).decode("utf-8")}
                for action, output in zip(message["extra"]["actions"], outputs)]
