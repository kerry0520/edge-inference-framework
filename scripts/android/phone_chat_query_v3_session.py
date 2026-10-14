"""Opt-in limited Qwen3/Mi11 CPU FP16 query-scale candidate; default chat unchanged."""
import json
from pathlib import Path
from phone_chat_kv_session import Session as KvSession, main as kv_main, ROOT, MODEL, sha

LIBRARY_SHA = "ae3c3f4d03b4b36dbd5371366457c2b5e8e1c21824ce9fbac6521c440f7090e1"
BUILD = ROOT / "build/query-v3-chat-candidate-20261011-v1"
RECORDS = ROOT / "results/android/query-v3-chat-20261011/build-v1"
from phone_chat_query_session import EXPECTED_MODEL_IDENTITY, EXPECTED_CONFIG


class Session(KvSession):
    release = "/data/local/tmp/MNN/framework-query-v3-chat-candidate-20261011-v1"
    record_root = ROOT / "results/android/query-v3-chat-20261011/sessions"
    binary_path = BUILD / "phone_chat_query_v3_worker-stripped"
    manifest_path = RECORDS / "build.json"
    worker_source = RECORDS / "phone_chat_query_v3_worker.cpp"
    history_mode = "kv_prefix_query_v3"
    chunk = 0
    reuse_granularity = 1
    extra_config = {"attention_mode": 8}
    require_full_input_ids = True
    expected_model_identity = EXPECTED_MODEL_IDENTITY
    expected_config = EXPECTED_CONFIG

    def start(self):
        build = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if build.get("library_sha256") != LIBRARY_SHA or build.get("history_mode") != self.history_mode or build.get("chunk") != self.chunk:
            raise RuntimeError("Candidate profile/library identity mismatch")
        if sha(ROOT / "apps/android/phone_chat_kv_worker.cpp") != build["base_worker_source_sha256"]:
            raise RuntimeError("Candidate service source changed; rebuild required")
        if sha(Path(build["library_path"])) != LIBRARY_SHA:
            raise RuntimeError("Candidate library changed")
        return super().start()


def main():
    return kv_main(Session, description="手机本机v3隔离候选多轮聊天（增量KV）；保持默认聊天入口不变。", loaded_message="模型已加载；本机v3隔离候选多轮聊天（增量KV），/new新对话，/history看历史，/exit退出。")


if __name__ == "__main__":
    raise SystemExit(main())
