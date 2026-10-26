"""Configured primary/backup providers, safe errors and explicit price uncertainty."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Provider:
    name: str
    api_base: str
    model: str
    api_key: str = field(repr=False)
    official_pricing: bool = False
    supports_thinking_control: bool = False

    def public(self):
        return {"name": self.name, "api_base": self.api_base, "model": self.model,
                "supports_thinking_control": self.supports_thinking_control,
                "cost_status": "conservative_estimate" if self.official_pricing else "unknown_platform_price"}


def load_providers(settings):
    backup = Provider("deepseek_official", settings["DEEPSEEK_API_BASE_URL"],
                      settings["DEEPSEEK_CHAT_MODEL"], settings["DEEPSEEK_API_KEY"], True, True)
    backup_url = urlparse(backup.api_base)
    if (backup_url.hostname != "api.deepseek.com" or backup_url.username or backup_url.password or
            backup_url.query or backup_url.fragment or backup_url.port not in {None, 443} or
            backup_url.scheme != "https" or backup.model != "deepseek-flash" or not backup.api_key):
        raise ValueError("Official backup must retain the authorized DeepSeek endpoint/model.")
    providers = []
    if settings.get("BUPT_API_KEY"):
        config = json.loads((ROOT / "configs/provider_priority.json").read_text(encoding="utf-8"))
        primary = config["primary"]
        if not all(config["policy"][key] for key in ("try_each_provider_at_most_once_per_query",
                "stick_to_fallback_until_run_ends", "start_new_run_with_primary")):
            raise ValueError("Only the declared bounded failover policy is supported.")
        endpoint = settings.get(primary["endpoint_env"])
        model = settings.get(primary["model_env"])
        if not endpoint or not model:
            raise ValueError("Primary key is configured, but its API base URL or model field is missing.")
        url = urlparse(endpoint)
        if (url.scheme != "https" or url.hostname != "myai.bupt.edu.cn" or
                url.path.rstrip("/") != "/llm-gw/v1" or url.username or url.password or url.query or url.fragment or url.port not in {None, 443}):
            raise ValueError("Primary must use the user-authorized BUPT gateway URL.")
        if model != "deepseek-v4-flash" or primary["pricing"] is not None:
            raise ValueError("Expected the declared BUPT model and unknown pricing.")
        providers.append(Provider(primary["name"], endpoint, model, settings[primary["credential_env"]]))
    providers.append(backup)
    return providers


def redact(text, providers):
    for provider in providers:
        text = text.replace(provider.api_key, "[REDACTED]").replace(provider.api_base, "[API_BASE]")
    text = re.sub(r"\bsk-[a-zA-Z0-9_-]+", "[REDACTED]", text)
    return re.sub(r"Your api key:\s*\S+", "API credential", text, flags=re.IGNORECASE)


def classify_failure(error, provider, providers):
    diagnostic = redact(str(error), providers)
    value = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    try:
        code = int(value) if value is not None else None
    except (TypeError, ValueError):
        code = None
    lower = diagnostic.lower()
    connection = any(word in type(error).__name__.lower() for word in ("timeout", "connection"))
    if code == 402 or any(term in lower for term in ("insufficient balance", "insufficient_balance", "insufficientbalance", "quota exhausted", "quota_exceeded")):
        kind, notice = "insufficient_balance", "余额或可用额度不足"
    elif code == 401 or "authentication fails" in lower or "api key" in lower and "invalid" in lower:
        kind, notice = "authentication_failed", "认证失败，需要检查凭据"
    elif code == 403:
        kind, notice = "permission_denied", "当前凭据没有调用权限"
    elif code == 404:
        kind, notice = "model_unavailable", "指定模型或接口不可用"
    elif code == 429:
        kind, notice = "rate_limited", "请求达到限速或额度限制"
    elif connection:
        kind, notice = "connection_failed", "连接失败或超时；这不表示凭据已经失效"
    else:
        kind, notice = "request_failed", "API请求失败，尚不能判断是网络还是服务问题"
    policy = json.loads((ROOT / "configs/provider_priority.json").read_text(encoding="utf-8"))["policy"]
    failover = (code in policy["failover_http_statuses"] or connection and policy["failover_connection_or_timeout"]
                or kind in {"insufficient_balance", "authentication_failed"})
    return {"kind": kind, "provider": provider.name, "status_code": code,
            "notice": provider.name + "：" + notice + "。", "failover_eligible": failover}, diagnostic
