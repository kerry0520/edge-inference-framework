"""Isolated public-callback experiment; never selected by production chat."""
from pathlib import Path
from phone_infer import ROOT
from phone_chat_metrics import FullStatsSession


def profile_session(binary, manifest, source, enabled):
    """Use the same executable/config, varying one diagnostic boolean only."""
    if type(enabled) is not bool:
        raise ValueError("profile switch must be a bool")

    class OpProfileSession(FullStatsSession):
        binary_path = Path(binary)
        manifest_path = Path(manifest)
        worker_source = Path(source)
        release = "/data/local/tmp/MNN/op-profile-20261013"
        record_root = ROOT / "results/android/op-profile-20261013/isolated-sessions"
        extra_config = {"chunk": 32, "enable_debug": True, "op_profile_enabled": enabled}

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.record["op_profile_enabled"] = enabled
            self.persist()

        def start(self):
            result = super().start()
            if self.record["ready"].get("op_profile_enabled") is not enabled:
                raise RuntimeError("ready callback mode differs from selected mode")
            return result

        def validate_response(self, response):
            super().validate_response(response)
            profile = response.get("op_profile")
            if (not isinstance(profile, dict) or profile.get("version") != 1 or
                    profile.get("enabled") is not enabled or profile.get("balance") is not True or
                    profile.get("scope") != "whole_response_visible_pipeline_callbacks"):
                raise RuntimeError("callback profile identity/balance invalid")
            counts = [profile.get(key) for key in ("before_count", "after_count", "pairs")]
            if any(type(value) is not int or value < 0 for value in counts) or len(set(counts)) != 1:
                raise RuntimeError("callback counts do not balance")
            if bool(counts[0]) != enabled:
                raise RuntimeError("callback enabled mode requires nonzero matched pairs")
            if not enabled and (profile.get("op_aggregate") or profile.get("samples")):
                raise RuntimeError("disabled callback unexpectedly reports samples")

    return OpProfileSession
