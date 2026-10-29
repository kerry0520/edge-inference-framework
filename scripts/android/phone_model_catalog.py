"""Known deployment selections; this is not an arbitrary model loader."""
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODELS = {
    "qwen3-0.6b": {"name": "Qwen3-0.6B", "strategy": "existing_plan_cache",
                   "history_mode": "full_recompute", "max_context_tokens": 512,
                   "device_scope": "existing_Mi11_identity_checks",
                   "resources": "existing_default_session_identity_hook", "model_profile": None},
    "qwen3-1.7b": {"name": "Qwen3-1.7B", "strategy": "baseline_without_trunk_plan",
                   "history_mode": "full_recompute", "max_context_tokens": 512,
                   "device_scope": "existing_Mi11_identity_checks",
                   "resources": "fixed_graph_weight_tokenizer_template_profile",
                   "model_profile": "configs/models/qwen3-1.7b-mnn.json"},
}


def list_models():
    """Return independent declarations without connecting to a phone."""
    return [{"id": key, **deepcopy(value)} for key, value in MODELS.items()]


def resolve_model(model):
    if not isinstance(model, str) or model not in MODELS:
        raise ValueError("Unknown registered model; use --list-models")
    return deepcopy(MODELS[model])


def selection(model):
    """Resolve only registered session policies and already checked resources."""
    spec = resolve_model(model)
    if spec["model_profile"] is None:
        from phone_chat_plan import PlanCacheSession
        return spec, PlanCacheSession, {}
    from phone_model_session import LargerModelSession, resolve_model_profile
    path = ROOT / spec["model_profile"]
    # Same strict allowlist parser as legacy --model-profile, before any ADB.
    resolve_model_profile(path)
    return spec, LargerModelSession, {"model_profile": path}
