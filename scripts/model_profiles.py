"""Public capability declarations. Profile selection never authorizes a new API."""
from copy import deepcopy
import json
from pathlib import Path

from collaboration_protocol import canonical_bytes, sha256

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "configs/model_profiles_v1.json"
DEFAULT = "legacy_deepseek_v1"


class ModelConfigurationError(ValueError):
    pass


def configuration():
    raw = CONFIG.read_bytes()
    if len(raw) > 16384:raise ModelConfigurationError("Model capability configuration exceeds bound")
    value = json.loads(raw)
    if set(value) != {"schema_version", "default_profile", "profiles"} or type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["default_profile"] != DEFAULT:
        raise ModelConfigurationError("Unsupported model capability configuration")
    profiles = value["profiles"]
    if not isinstance(profiles, list) or [p.get('id') for p in profiles] != [DEFAULT,'anthropic_messages_offline_v1']:
        raise ModelConfigurationError("Only the declared legacy and offline candidate profiles are registered")
    fields = {"id","label","status","protocol","roles","role_mixing","tool_calls","thinking","context_limits","output_limit","cancellation","usage","pricing","route"}
    for p in profiles:
        if set(p) != fields or p['role_mixing'] is not False or any(not isinstance(p[k],str) or not 1<=len(p[k])<=200 for k in fields-{'roles','role_mixing'}):
            raise ModelConfigurationError("Invalid bounded public model declaration")
    legacy, candidate = profiles
    if (legacy['status'],legacy['protocol'],legacy['roles'],legacy['route']) != ('enabled','chat_completions_tools_v1',['single_agent','repairer','reviewer','document_reader'],'unchanged_provider_priority_json'):
        raise ModelConfigurationError("Legacy route must retain its existing protocol and roles")
    if (candidate['status'],candidate['protocol'],candidate['roles'],candidate['route']) != ('offline_only','anthropic_messages_tools_v1',['repairer','reviewer','document_reader'],'no_live_endpoint_or_credentials_registered'):
        raise ModelConfigurationError("New-vendor candidate cannot be activated by editing its declaration")
    return value


def profile_catalog():
    value = configuration()
    return {"schema_version":1,"default_profile":DEFAULT,"profiles":value['profiles'],
        "configuration_sha256":sha256(CONFIG.read_bytes()),
        "note":"声明不是账号/真实模型验收。原路由保持；Claude候选只有离线格式适配，没有可发送的客户端。"}


def selection_fields(profile_id, *, execution, phases=None):
    if profile_id is None:return {}
    if not isinstance(profile_id,str):raise ModelConfigurationError("模型配置必须是已登记的名称。")
    profile = next((p for p in configuration()['profiles'] if p['id']==profile_id),None)
    if profile is None:raise ModelConfigurationError("模型配置尚未登记，不能只替换模型名后运行。")
    if execution != 'live':raise ModelConfigurationError("离线补丁重放不选择新模型配置。")
    if profile['status'] != 'enabled':raise ModelConfigurationError("Claude候选仅离线格式适配；没有新增外发授权、凭据/费用验证或联网传输，当前不能创建运行。")
    phase_roles = {'single_agent':'single_agent'} if phases is None else {
        phase:'document_reader' if phase=='consulting' else 'repairer' if phase in {'repairing','revising'} else 'reviewer' for phase in phases}
    if any(role not in profile['roles'] for role in phase_roles.values()):raise ModelConfigurationError("所选配置缺少角色需要的工具能力。")
    snapshot = {'profile':deepcopy(profile),'configuration_sha256':sha256(CONFIG.read_bytes()),
        'role_bindings':{phase:profile_id for phase in phase_roles},'role_requirements':{
            phase:{'role':role,'tool_mode':'one_bash_call' if role in {'single_agent','repairer'} else 'one_or_two_serial_reads_or_one_submission' if role=='document_reader' else 'one_read_or_submission',
                'independent_acceptance':'external_program_checks_not_model_claims'} for phase,role in phase_roles.items()},
        'route_policy_sha256':sha256((ROOT/'configs/provider_priority.json').read_bytes())}
    return {'model_profile_id':profile_id,'model_profile_snapshot':snapshot,'model_profile_sha256':sha256(canonical_bytes(snapshot))}


def validate_record(row, *, phases=None):
    selected = selection_fields(row.get('model_profile_id'),execution='live',phases=phases)
    if any(row.get(k)!=v for k,v in selected.items()):raise ModelConfigurationError("模型能力/角色或路由配置已变化；原身份不迁移，请保留后另建任务。")
    return selected
