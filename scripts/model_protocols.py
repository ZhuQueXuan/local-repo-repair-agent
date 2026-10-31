"""Protocol boundary: existing chat calls plus an explicitly offline Messages codec."""
from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Protocol

from collaboration_protocol import canonical_bytes
from model_profiles import ModelConfigurationError


class ModelAdapter(Protocol):
    protocol_id: str

    def prepare(self, messages, tools, **options): ...

    def normalize(self, response, **options): ...


class LegacyChatAdapter:
    protocol_id = 'chat_completions_tools_v1'

    def validate(self, messages, *, role, provider, limits):
        if role not in {'single_agent','repairer','reviewer','document_reader'}:
            raise ModelConfigurationError('Unsupported model role')
        if provider.name not in {'bupt_gateway','deepseek_official'}:
            raise ModelConfigurationError('Provider not registered for the legacy adapter')
        if (provider.name=='deepseek_official' and provider.model!='deepseek-flash') or (provider.name=='bupt_gateway' and provider.model!='deepseek-v4-flash'):
            raise ModelConfigurationError('Model name is not the declared provider capability')
        if not isinstance(messages,list) or not messages or any(not isinstance(m,dict) or m.get('role') not in {'system','user','assistant','tool'} for m in messages):
            raise ModelConfigurationError('Only the registered text/tool message roles are supported')
        if limits['max_output_tokens']!=2048 or limits['max_messages_utf8_bytes'] not in {16384,32768,65536}:
            raise ModelConfigurationError('Unsupported scoped request limits')
        if limits['max_messages_utf8_bytes']==65536 and role not in {'repairer','reviewer'}:
            raise ModelConfigurationError('Large source context is not registered for this role')
        if any(m.get('reasoning_content') for m in messages):
            raise ModelConfigurationError('Thinking history is not supported by this non-thinking adapter')

    def prepare(self, messages, tools, **options):
        return dict(options,messages=messages,tools=tools)

    def normalize(self,response,**options):
        return response  # Existing LiteLLM response and accounting stay intact.


@dataclass(frozen=True)
class NormalizedToolReply:
    message_id: str
    model: str
    content: str | None
    calls: tuple
    prompt_tokens: int
    completion_tokens: int
    raw_usage: dict
    finish_reason: str

    def as_upstream_response(self):
        from litellm import ModelResponse
        return ModelResponse(id=self.message_id,model=self.model,choices=[{'index':0,'message':{
            'role':'assistant','content':self.content,'tool_calls':list(self.calls)},'finish_reason':self.finish_reason}],
            usage={'prompt_tokens':self.prompt_tokens,'completion_tokens':self.completion_tokens})


class AnthropicMessagesOfflineAdapter:
    """No endpoint, key, SDK client or network method is registered in this version."""
    protocol_id = 'anthropic_messages_tools_v1'

    def complete(self,*args,**kwargs):
        raise ModelConfigurationError('anthropic_messages_offline_only_no_network_transport')

    def prepare(self,messages,tools,*,model,max_tokens=2048):
        if not isinstance(model,str) or not re.fullmatch(r'[a-zA-Z0-9._-]{1,120}',model):raise ModelConfigurationError('Invalid codec-only model identifier')
        if type(max_tokens) is not int or not 1<=max_tokens<=2048:raise ModelConfigurationError('Invalid codec output limit')
        if not isinstance(tools,list) or not 1<=len(tools)<=16:raise ModelConfigurationError('Expected a bounded explicit tool set')
        converted=[];names=set()
        for tool in tools:
            if set(tool)!={'type','function'} or tool['type']!='function':raise ModelConfigurationError('Only named function tools are supported')
            f=tool['function']
            if set(f)!={'name','description','parameters'} or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',f['name']) or f['name'] in names or not isinstance(f['parameters'],dict):
                raise ModelConfigurationError('Invalid or duplicate function definition')
            names.add(f['name']);converted.append({'name':f['name'],'description':f['description'],'input_schema':deepcopy(f['parameters'])})
        if not isinstance(messages,list) or not messages:raise ModelConfigurationError('Expected message history')
        system=[];history=[];pending=set();seen=set();started=False
        def append(role,blocks):
            if history and history[-1]['role']==role:history[-1]['content'].extend(blocks)
            else:history.append({'role':role,'content':blocks})
        for item in messages:
            if isinstance(item,dict):
                # LiteLLM's normalized assistant contains explicit null compatibility
                # fields. Non-null function calls or vendor data remain unsupported.
                for optional in ('function_call','provider_specific_fields'):
                    if item.get(optional) is not None:raise ModelConfigurationError('Nonempty legacy/vendor field is not supported')
                item={k:v for k,v in item.items() if k not in {'function_call','provider_specific_fields'}}
            if not isinstance(item,dict) or set(item)-{'role','content','tool_calls','tool_call_id'}:
                raise ModelConfigurationError('Unsupported message fields; thinking/images/cache controls are not silently dropped')
            role=item.get('role');content=item.get('content')
            if role in {'system','tool'} and set(item)-({'role','content'} if role=='system' else {'role','content','tool_call_id'}):
                raise ModelConfigurationError('Tool-call fields on system/result message')
            if content is not None and not isinstance(content,str):raise ModelConfigurationError('Only text content supported by this codec')
            if role=='system':
                if started or pending or not content:raise ModelConfigurationError('System content must precede the conversation')
                system.append(content);continue
            started=True
            if pending and role!='tool':raise ModelConfigurationError('Tool results must immediately follow their tool-use turn')
            if role=='tool':
                ident=item.get('tool_call_id')
                if ident not in pending or not isinstance(content,str) or item.get('tool_calls'):
                    raise ModelConfigurationError('Orphan, duplicate or malformed tool result')
                pending.remove(ident);append('user',[{'type':'tool_result','tool_use_id':ident,'content':content}]);continue
            if role not in {'user','assistant'}:raise ModelConfigurationError('Unsupported conversation role')
            if item.get('tool_call_id'):raise ModelConfigurationError('Tool result identity on ordinary message')
            blocks=[{'type':'text','text':content}] if content else []
            calls=item.get('tool_calls',[])
            if calls is None:calls=[]
            if not isinstance(calls,list) or len(calls)>2 or role=='user' and calls:raise ModelConfigurationError('Invalid tool-call batch')
            for call in calls:
                if not isinstance(call,dict) or set(call)!={'id','type','function'} or call['type']!='function':raise ModelConfigurationError('Invalid function-call envelope')
                ident=call['id'];f=call['function']
                if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',ident) or ident in seen:
                    raise ModelConfigurationError('Invalid or reused tool-call identity')
                if not isinstance(f,dict) or set(f)!={'name','arguments'} or f['name'] not in names or not isinstance(f['arguments'],str):raise ModelConfigurationError('Unregistered tool call')
                try:args=json.loads(f['arguments'])
                except ValueError:raise ModelConfigurationError('Tool arguments are not JSON') from None
                if not isinstance(args,dict) or len(canonical_bytes(args))>32768:raise ModelConfigurationError('Tool arguments exceed codec bound')
                blocks.append({'type':'tool_use','id':ident,'name':f['name'],'input':args});seen.add(ident);pending.add(ident)
            if not blocks:raise ModelConfigurationError('Empty message unsupported')
            append(role,blocks)
        if pending:raise ModelConfigurationError('Unanswered tool calls in request history')
        if not history or history[0]['role']!='user' or history[-1]['role']!='user':raise ModelConfigurationError('No assistant prefill is supported')
        payload={'model':model,'max_tokens':max_tokens,'messages':history,'tools':converted}
        if system:payload['system']='\n\n'.join(system)
        if len(canonical_bytes(payload))>131072:raise ModelConfigurationError('Codec envelope exceeds local bound')
        return payload

    def normalize(self,raw,*,allowed_tools):
        if not isinstance(raw,dict) or raw.get('type')!='message' or raw.get('role')!='assistant':raise ModelConfigurationError('Invalid Messages reply')
        if len(canonical_bytes(raw))>131072:raise ModelConfigurationError('Reply exceeds codec bound')
        for name in ('id','model'):
            if not isinstance(raw.get(name),str) or not 1<=len(raw[name])<=200:raise ModelConfigurationError('Missing response identity')
        usage=raw.get('usage')
        if not isinstance(usage,dict):raise ModelConfigurationError('Missing token usage; cannot record zero cost')
        for name in ('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens'):
            count=usage.get(name,0 if name.startswith('cache_') else None)
            if type(count) is not int or not 0<=count<=10000000:raise ModelConfigurationError('Missing or invalid token usage')
        if usage['output_tokens']>2048:raise ModelConfigurationError('Reply exceeds declared output token bound')
        content=raw.get('content');calls=[];texts=[];seen=set()
        if not isinstance(content,list) or not content:raise ModelConfigurationError('Missing response blocks')
        for block in content:
            if not isinstance(block,dict):raise ModelConfigurationError('Invalid content block')
            if block.get('type')=='text' and set(block)=={'type','text'} and isinstance(block['text'],str):texts.append(block['text'])
            elif block.get('type')=='tool_use' and set(block)=={'type','id','name','input'}:
                ident=block['id'];name=block['name']
                if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',ident) or ident in seen or name not in allowed_tools or not isinstance(block['input'],dict):
                    raise ModelConfigurationError('Unregistered, duplicate or invalid response tool')
                if len(canonical_bytes(block['input']))>32768:raise ModelConfigurationError('Tool input exceeds bound')
                seen.add(ident);calls.append({'id':ident,'type':'function','function':{'name':name,'arguments':canonical_bytes(block['input']).decode()}})
            else:raise ModelConfigurationError('Unsupported response block; no thinking/image/server tools in this codec')
        stop=raw.get('stop_reason')
        if stop not in {'tool_use','end_turn'} or bool(calls)!=(stop=='tool_use') or len(calls)>2:
            raise ModelConfigurationError('Truncated/refused/unsupported response is not an executable tool result')
        prompt=usage['input_tokens']+usage.get('cache_creation_input_tokens',0)+usage.get('cache_read_input_tokens',0)
        return NormalizedToolReply(raw['id'],raw['model'],'\n'.join(texts) or None,tuple(calls),prompt,usage['output_tokens'],deepcopy(usage),'tool_calls' if calls else 'stop')
