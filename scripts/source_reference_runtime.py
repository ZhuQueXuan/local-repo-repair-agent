"""Attach one shared reference role to either analysis or repair workflows."""
import uuid

from analysis_sources import load_source, source_bytes
from analysis_execution import RoleBudget
from case_review_execution import ReviewerModels
from reference_lookup import ReferenceService, REFERENCE_SCHEMAS, REFERENCE_DESCRIPTIONS
from source_flow_model import SourceFlowModel
from task_service_store import now


def reference_service(*, store, row, models, roles, settings, route_state, output, check, deadline, save, progress,
                      files=None, scope=None):
    from task_service_worker import RunBudget
    mode = row.get('reference_mode', 'off')
    if mode == 'off': return None

    def selected_documents():
        source = load_source(store.home, row['source_id'], row['source_sha256'])
        return {path: source_bytes(store.home, source, path) for path in row['paths'] if path.lower().endswith(('.md', '.rst', '.txt'))}

    def make_model(identity, request_check, request_deadline):
        group = models.setdefault('reference', ReviewerModels())
        role = roles.setdefault('reference', {'session_id': uuid.uuid4().hex, 'status': 'running', 'started': now(), 'sessions': []})
        role.update(status='running')
        role['sessions'].append(identity)
        model = SourceFlowModel(settings, schemas=REFERENCE_SCHEMAS, descriptions=REFERENCE_DESCRIPTIONS,
            audit={'path': output / 'reference-api-requests.jsonl', 'role': 'reference', 'session_id': identity},
            budget=RoleBudget(RunBudget(store, row['id']), store, row['id'], 'reference', identity), route_state=route_state,
            ledger_path=output / (identity + '-ledger.json'), error_path=output / (identity + '-error.json'),
            request_check=request_check, request_deadline=request_deadline, model_role='document_reader')
        group.models.append(model)
        return model

    def update():
        if 'reference' in models:
            from task_service_worker import model_metrics
            roles['reference'].update(metrics=model_metrics(models['reference']), status='consulted', ended=now())
        progress()

    return ReferenceService(mode=mode, files=files or selected_documents,
        scope=scope or (lambda: {'source_id': row['source_id'], 'source_sha256': row['source_sha256']}),
        make_model=make_model, save=save, check=check, deadline=deadline, progress=update,
        secrets=[value for key, value in settings.items() if 'KEY' in key and value])
