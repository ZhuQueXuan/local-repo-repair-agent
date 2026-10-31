"""Program-driven, bounded case sessions on the existing protected model route."""
import json
import time
import uuid

from minisweagent.exceptions import FormatError
from collaboration_protocol import canonical_bytes, sha256
from review_model import ReviewModel, litellm
from review_cases import CASE_SCHEMAS, CASE_DESCRIPTIONS, MaterialIncomplete, RequestCaseEvidence
from task_service_store import now

MAX_CASE_STAGNANT_TURNS = 8  # Stops repeated non-progress, not total requests.


class ReviewerModels:
    """Expose aggregate metrics without resetting the shared route, ledger or deadline."""
    def __init__(self):
        self.models = []

    @property
    def requests(self): return sum(model.requests for model in self.models)

    @property
    def usage(self): return [item for model in self.models for item in model.usage]

    @property
    def actual_cost_cny(self): return sum(model.actual_cost_cny for model in self.models)

    @property
    def providers(self): return [provider for model in self.models for provider in model.providers]

    @property
    def budget_config(self):
        if self.models:
            return self.models[0].budget_config
        from deepseek_budget import load_budget_config
        return load_budget_config()

    def complete_cost_estimate(self):
        estimates = [model.complete_cost_estimate() for model in self.models]
        return sum(estimates) if all(value is not None for value in estimates) else None

    def provider_statistics(self):
        return {'scope': 'one reviewer role, fresh per-case sessions',
                'case_sessions': [model.provider_statistics() for model in self.models]}


class CaseReviewModel(ReviewModel):
    tool_schemas = CASE_SCHEMAS

    def __init__(self, *args, audit_context, references=False, current_source_refs=lambda: (), **kwargs):
        kwargs.setdefault('request_limits', {'max_messages_utf8_bytes': 32768, 'max_prompt_tokens': 40960})
        self.audit_context = audit_context
        self.current_source_refs = current_source_refs
        self.descriptions = dict(CASE_DESCRIPTIONS)
        if references:
            from reference_lookup import ConsultReferences, CONSULT_DESCRIPTION
            self.tool_schemas = CASE_SCHEMAS | {'consult_references': ConsultReferences}
            self.descriptions['consult_references'] = CONSULT_DESCRIPTION
        super().__init__(*args, **kwargs)
        self.config.model_kwargs['parallel_tool_calls'] = False

    def available_api_tools(self, tools):
        for tool in tools:
            tool['function']['description'] = self.descriptions[tool['function']['name']]
            if tool['function']['name'] == 'submit_case_review':
                field = tool['function']['parameters']['properties']['source_refs']
                field['description'] = 'Use only the current source_material ref IDs, such as R1. File paths, file:line strings and analyst evidence IDs are invalid.'
                field['items'] = {'type': 'string', 'enum': list(self.current_source_refs())}
        return tools

    def _perform_completion(self, messages, provider):
        self.request_check()
        remaining = self.request_deadline() - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('No request time remaining')
        tools = self.available_api_tools([{'type': 'function', 'function': {
            'name': name, 'description': self.descriptions[name], 'parameters': schema.model_json_schema()}}
            for name, schema in self.tool_schemas.items()])
        options = dict(self.config.model_kwargs, timeout=min(45, remaining))
        request = self.protocol_adapter.prepare(messages, tools, model=self.config.model_name, api_key=self._api_key, **options)
        effective = {key: request.get(key) for key in ('max_tokens', 'temperature', 'num_retries', 'parallel_tool_calls', 'extra_body', 'timeout')}
        audit = self.audit_context
        record = {key: value for key, value in audit.items() if key != 'path'} | {
            'next_attempt': self.requests, 'provider': provider.name, 'model': request['model'],
            'tools': [tool['function']['name'] for tool in request['tools']], 'tools_sha256': sha256(canonical_bytes(request['tools'])),
            'effective_parameters': effective, 'messages': request['messages']}
        with audit['path'].open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        # Sending specializes the tool set only. Budget, routing, usage receipts
        # and deadline guards still live in the inherited _query path.
        return litellm.completion(**request)

    def _parse_actions(self, response):
        if self.contains_credentials(canonical_bytes(response.model_dump()).decode()):
            raise ValueError('模型输出触发凭据保护。')
        calls = response.choices[0].message.tool_calls or []
        # Tolerate a server returning two pure evidence requests despite the
        # single-call flag. They form ONE bounded evidence round, never two votes.
        if len(calls) == 2 and all(call.function.name == 'request_case_evidence' for call in calls):
            actions = []
            try:
                for call in calls:
                    arguments = json.loads(call.function.arguments)
                    if not call.id or len(canonical_bytes(arguments)) > 32768:
                        raise ValueError('Invalid evidence call')
                    RequestCaseEvidence.parse(arguments)
                    actions.append({'tool': call.function.name, 'arguments': arguments, 'tool_call_id': call.id})
                if len({action['tool_call_id'] for action in actions}) != 2:
                    raise ValueError('Duplicate tool identity')
                return actions
            except (ValueError, TypeError):
                raise FormatError({'role': 'user', 'content': 'Use the two registered case tools with valid source ranges; source remains read-only.'}) from None
        return super()._parse_actions(response)


def run_case_reviews(controller, *, store, run_id, output, settings, route_state, models, roles,
                     check, save, progress, budget_factory, model_factory=None, persist_case_review=lambda: None, references=None):
    from task_service_worker import model_metrics
    group = ReviewerModels()
    models['reviewer'] = group
    roles['reviewer'] = {'session_id': uuid.uuid4().hex, 'status': 'running', 'started': now(), 'cases': [], 'metrics': {}}
    progress('analysis_role_started')

    def checkpoint():
        roles['reviewer']['metrics'] = model_metrics(group)
        save('review-progress.json', controller.report() | {'status': 'partial'})
        progress()

    for case in controller.cases:
        check()  # The same absolute reviewer/parent deadline is used for every case.
        try:
            case.prepare()
        except MaterialIncomplete as error:
            case.stop('material_incomplete', str(error))
            checkpoint()
            persist_case_review()
            continue
        session = uuid.uuid4().hex
        prefix = 'reviewer-' + case.case_id + '-'
        role_case = {'finding_id': case.case_id, 'session_id': session, 'started': now(), 'status': 'running'}
        roles['reviewer']['cases'].append(role_case)
        case.result.update(execution_status='running', session_id=session)
        save(prefix + 'input.json', {'protocol': 'case_review_v2', 'draft_sha256': controller.digest,
            'source_id': controller.row['source_id'], 'source_sha256': controller.row['source_sha256'],
            'finding_id': case.case_id, 'messages': case.messages(), 'source_bindings': case.evidence()})
        model = (model_factory or CaseReviewModel)(settings,
            references=bool(references),
            current_source_refs=lambda: tuple(case.evidence()),
            audit_context={'path': output / 'reviewer-api-requests.jsonl', 'role': 'reviewer', 'case_id': case.case_id, 'session_id': session},
            budget=budget_factory(session, case.case_id), route_state=route_state,
            ledger_path=output / (prefix + 'ledger.json'), error_path=output / (prefix + 'request_error.json'),
            request_check=check, request_deadline=controller.request_deadline, model_role='reviewer')
        group.models.append(model)
        exchanges, correction = [], None
        stagnant = 0
        previous_progress = (len(case.refs), sha256(canonical_bytes(case.reference_advice)))
        try:
            while case.result['execution_status'] == 'running':
                check()
                controller.verify()
                messages = case.messages()
                if correction:
                    messages.append({'role': 'user', 'content': correction})
                try:
                    message = model.query(messages)
                    check()
                    exchanges.append(message)
                    actions = message['extra']['actions']
                    if len(actions) == 1 and actions[0]['tool'] == 'consult_references' and references:
                        if case.reference_advice is not None: raise MaterialIncomplete('本条已查过资料，不重复查询。')
                        case.reference_advice = references.consult(actions[0]['arguments'], consumer='reviewer:' + case.case_id)
                        if not case.fits(case.refs): raise MaterialIncomplete('完整问题、源码与资料合计超过材料上限；未删除核心材料。')
                        correction = None
                        continue
                    if len(actions) == 1 and actions[0]['tool'] == 'submit_case_review':
                        case.submit(actions[0]['arguments'])
                        break
                    if not actions or any(action['tool'] != 'request_case_evidence' for action in actions):
                        raise ValueError('本条只能提交一次意见或请求一轮补证。')
                    case.add_evidence([RequestCaseEvidence.parse(action['arguments']) for action in actions])
                    correction = None
                except FormatError as error:
                    exchanges.extend(error.messages)
                    correction = '上次回复未满足工具协议。请使用当前条R编号提交判断，或明确请求缺失源码；不得执行或修改源码。'
                except MaterialIncomplete as error:
                    case.stop('material_incomplete', str(error))
                    break
                except ValueError as error:
                    # Integrity/credentials are global failures, never local verdicts.
                    controller.verify()
                    if '凭据' in str(error):
                        raise
                    exchanges.append({'role': 'user', 'content': str(error)[:500]})
                    correction = '上次提交的字段或R引用不合法。仅使用当前材料及当前条R编号；若依据不足，可提交有依据的待验证判断。'
                finally:
                    save(prefix + 'trajectory.json', {'case_id': case.case_id, 'session_id': session,
                        'exchanges': exchanges, 'current_messages': case.messages(), 'source_bindings': case.evidence()})
                    checkpoint()
                    current_progress = (len(case.refs), sha256(canonical_bytes(case.reference_advice)))
                    stagnant = stagnant + 1 if current_progress == previous_progress else 0
                    previous_progress = current_progress
                    if case.result['execution_status'] == 'running' and stagnant >= MAX_CASE_STAGNANT_TURNS:
                        case.stop('model_not_submitted', '本条连续8次没有新增依据或合法判断，停止重复动作；不按模型调用总次数截断。')
        except BaseException:
            # Outer executor classifies cancellation, timeout and provider errors.
            # Do not start another case when one of these global guards fires.
            raise
        finally:
            model.ledger.update(completed=True, run_id=run_id, analysis_role='reviewer', case_id=case.case_id, session_id=session)
            save(prefix + 'ledger.json', model.ledger)
            role_case.update(status=case.result['execution_status'], ended=now(), metrics=model_metrics(model))
            checkpoint()
            if case.result['execution_status'] not in {'running', 'not_started'}:
                persist_case_review()
    review = controller.report()
    roles['reviewer'].update(status='submitted' if review['status'] == 'completed' else 'partial', ended=now())
    save('reviewer-ledger.json', {'completed': True, 'run_id': run_id, 'role': 'reviewer',
        'attempts': group.requests, 'case_sessions': roles['reviewer']['cases']})
    return review
