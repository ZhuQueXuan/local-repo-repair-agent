"""Audited tool sets on the existing accounting/failover path; no new API client."""
import json
import time

from minisweagent.exceptions import FormatError
from review_model import ReviewModel, litellm
from collaboration_protocol import canonical_bytes, sha256


class SourceFlowModel(ReviewModel):
    def __init__(self, *args, schemas, descriptions, audit, large_source_context=False, **kwargs):
        self.tool_schemas, self.descriptions, self.audit = schemas, descriptions, audit
        # Complete findings, source and runtime reports must fit together. Reuse
        # the client's registered 32KB engineering scope, not the global default.
        kwargs.setdefault('request_limits', {'max_messages_utf8_bytes': 65536, 'max_prompt_tokens': 73728}
                          if large_source_context else {'max_messages_utf8_bytes': 32768, 'max_prompt_tokens': 40960})
        super().__init__(*args, **kwargs)
        self.config.model_kwargs['parallel_tool_calls'] = False

    def _parse_actions(self, response):
        if self.contains_credentials(canonical_bytes(response.model_dump()).decode()):
            raise ValueError('模型回复触发凭据保护。')
        calls = response.choices[0].message.tool_calls or []
        # Some servers return several reads despite parallel_tool_calls=False.
        # Validate the WHOLE read batch before the controller dispatches any read.
        # Writes, submissions and reference model calls remain single-action only.
        if 2 <= len(calls) <= 4 and all(call.function.name in {'read_source', 'read_analysis_finding'} for call in calls):
            try:
                actions = []
                for call in calls:
                    arguments = json.loads(call.function.arguments)
                    if not call.id or len(canonical_bytes(arguments)) > 32768:
                        raise ValueError('Invalid read call')
                    self.tool_schemas[call.function.name].parse(arguments)
                    actions.append({'tool': call.function.name, 'arguments': arguments, 'tool_call_id': call.id})
                if len({action['tool_call_id'] for action in actions}) != len(actions):
                    raise ValueError('Duplicate read identity')
                return actions
            except (ValueError, TypeError, KeyError):
                raise FormatError({'role': 'user', 'content': 'The read batch was rejected before any read. Use valid registered read arguments and unique call IDs; submit results and edits one at a time.',
                                   'extra': {'interrupt_type': 'FormatError'}}) from None
        return super()._parse_actions(response)

    def _perform_completion(self, messages, provider):
        self.request_check()
        remaining = self.request_deadline() - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('任务期限已到，不发送新请求。')
        tools = [{'type': 'function', 'function': {'name': name, 'description': self.descriptions[name],
            'parameters': schema.model_json_schema()}} for name, schema in self.tool_schemas.items()
            if name in getattr(self, 'exposed_tools', self.tool_schemas)]
        for tool in tools:
            if tool['function']['name'] == 'read_source' and hasattr(self, 'source_missing_ranges'):
                tool['function']['description'] += ' Copy an exact path/start/end tuple from current_code.missing_ranges; previous reads remain visible.'
                tool['function']['parameters']['oneOf'] = [
                    {'properties': {key: {'const': value} for key, value in span.items()}}
                    for span in self.source_missing_ranges]
            if tool['function']['name'] == 'read_analysis_finding' and hasattr(self, 'unread_finding_ids'):
                tool['function']['parameters']['properties']['finding_id']['enum'] = list(self.unread_finding_ids)
        options = dict(self.config.model_kwargs, timeout=min(45, remaining))
        request = self.protocol_adapter.prepare(messages, tools, model=self.config.model_name, api_key=self._api_key, **options)
        record = {key: value for key, value in self.audit.items() if key != 'path'} | {
            'attempt': self.requests, 'provider': provider.name, 'model': request['model'], 'messages': request['messages'],
            'tools': [tool['function']['name'] for tool in request['tools']], 'tools_sha256': sha256(canonical_bytes(request['tools'])),
            'parameters': {key: request.get(key) for key in ('temperature', 'max_tokens', 'timeout', 'parallel_tool_calls', 'extra_body', 'num_retries')}}
        with self.audit['path'].open('a', encoding='utf8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        return litellm.completion(**request)
