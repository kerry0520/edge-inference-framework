"""Explicit, isolated 1.7B full-recompute candidate; no plan optimization admission.

The existing default native bytes are verified by PlanCacheSession unchanged.
Only the model directory/asset identity is selected by this strict candidate
profile; it cannot choose another binary, backend or arbitrary runtime options.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import shlex

from phone_chat_plan import BaselinePlanSession, ROOT
from phone_chat_session import sha

MODEL_ID = "qwen3-1.7b-mnn"
MODEL_NAME = "Qwen3-1.7B"
SOURCE_REPO = "MNN/Qwen3-1.7B-MNN"
SOURCE_REVISION = "cc0cae045f11c525fba613066ce7c54da03ac4dd"
REMOTE_DIRECTORY = "/data/local/tmp/MNN/Qwen3-1.7B-MNN-cc0cae045f11"
# Official ModelScope immutable-revision metadata. Graph/weight also match
# taobao-mnn/Qwen3-1.7B-MNN HF revision 1b12fe7da4acff8593fd20cc6007f0fa85c02682.
MODEL_FILES = {
    "llm.mnn": {"size": 461520, "sha256": "a6352fda4a2063a73126d2b552e35603564545ae3ed9b1547f58736d00f880fc"},
    "llm.mnn.weight": {"size": 1231860194, "sha256": "d53dfdb7b826e4103e58a30236312978ff98ee89a2e1b9d66dd6bf227afce823"},
    "llm_config.json": {"size": 4881, "sha256": "dea06be377d9be7f6617fd421153032aee5ae75b16f53ebc3ef84417c5206aaf"},
    "tokenizer.txt": {"size": 3193569, "sha256": "77009625db0b18e74315dd89557c76feaab57e1d54e3240b442649a3c16ec4fb"},
}


def model_profile_document():
    """One exact candidate identity, separate from registered native profiles."""
    return {"schema_version": 1, "model_id": MODEL_ID, "model_name": MODEL_NAME,
            "remote_directory": REMOTE_DIRECTORY,
            "source": {"repo": SOURCE_REPO, "revision": SOURCE_REVISION},
            "files": deepcopy(MODEL_FILES)}


def resolve_model_profile(path):
    supplied = Path(path).expanduser().absolute()
    if supplied.is_symlink() or not supplied.is_file():
        raise ValueError("模型候选profile必须是普通文件")
    try:
        document = json.loads(supplied.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError("模型候选profile缺失或非法") from error
    # Canonical JSON equality is type-sensitive (unlike Python True == 1).
    if json.dumps(document, sort_keys=True) != json.dumps(model_profile_document(), sort_keys=True):
        raise ValueError("模型候选profile不匹配固定1.7B来源/文件；不允许额外运行配置")
    return document


class LargerModelSession(BaselinePlanSession):
    """Same native worker, explicit new model identity, ordinary full execution."""
    model_name = MODEL_NAME
    model_id = MODEL_ID
    release = "/data/local/tmp/MNN/larger-model-candidate-20261020/qwen3-17b-baseline"
    record_root = ROOT / "results/android/larger-model-20261020/sessions"
    plan_requested = False
    extra_config = {**BaselinePlanSession.extra_config,
                    "base_dir": REMOTE_DIRECTORY + "/", "tokenizer_file": "tokenizer.txt",
                    "prefill_head_elision": False}

    def __init__(self, model_profile, serial=None, record_parent=None,
                 max_context_tokens=512, profile_path=None, adb_path=None):
        if profile_path is not None or adb_path is not None:
            raise ValueError("模型候选仅使用默认固定native构建，不能组合plan-profile/adb-path")
        self.model_profile_path = Path(model_profile).expanduser().absolute()
        self.model_profile = resolve_model_profile(self.model_profile_path)
        super().__init__(serial=serial, record_parent=record_parent,
                         max_context_tokens=max_context_tokens)
        self.record["model_candidate"] = {"profile": deepcopy(self.model_profile),
            "profile_sha256": sha(self.model_profile_path),
            "mode": "full_recompute_without_trunk_plan", "admitted_model": False}
        self.persist()

    def start(self):
        if resolve_model_profile(self.model_profile_path) != self.model_profile:
            raise ValueError("模型候选profile在启动前变化；未连接手机")
        return super().start()

    def model_identity_for_session(self, adb):
        # Four fixed filenames + a fixed absolute directory. No arbitrary shell
        # input is accepted. The same hook is used before load and during close.
        names = list(MODEL_FILES)
        commands = []
        for name in names:
            path = shlex.quote(REMOTE_DIRECTORY + "/" + name)
            commands.extend(["sha256sum " + path, "wc -c < " + path])
        lines = adb.shell(" && ".join(commands)).decode("utf-8", "strict").splitlines()
        if len(lines) != len(names) * 2:
            raise RuntimeError("候选模型文件身份输出数量不匹配")
        identity = {}
        for index, name in enumerate(names):
            digest_line, size_line = lines[index * 2:index * 2 + 2]
            parts = digest_line.split()
            remote = REMOTE_DIRECTORY + "/" + name
            if (len(parts) != 2 or not re.fullmatch("[0-9a-f]{64}", parts[0]) or
                    parts[1] != remote or not re.fullmatch(r"\s*[0-9]+\s*", size_line)):
                raise RuntimeError("候选模型文件身份输出非法")
            observed = {"sha256": parts[0], "size": int(size_line)}
            if observed != self.model_profile["files"][name]:
                raise RuntimeError("候选模型文件SHA/大小不匹配: " + name)
            identity[remote] = observed
        return identity
