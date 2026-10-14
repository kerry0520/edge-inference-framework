"""Opt-in limited Qwen3/Mi11 CPU FP16 query-scale candidate; default chat unchanged."""
import json
from pathlib import Path
from phone_chat_kv_session import Session as KvSession, main as kv_main, ROOT, MODEL, sha

LIBRARY_SHA = "92e984a259c4ebdcdefed9d0211bfaa5c4b77d0c9961f997192251fb93e46f8e"
BUILD = ROOT / "build/query-chat-candidate-20261011-v2"
RECORDS = ROOT / "results/android/query-scale-followup-20261011/chat-build-v2"
# Fixed from the accepted session's model_before, not the current phone:
# results/android/query-scale-followup-20261011/chat-device-v1/sessions/
# 1e587626fdfe4c3fb1287030e02eaa48/session.json
EXPECTED_MODEL_IDENTITY = {
    MODEL + "/llm.mnn": "d426c65a5159c938ccc237cdfbd982137f276804f27b414ca0ecf3fc0a660f8c",
    MODEL + "/llm.mnn.weight": "953afb7e0165818add34a7a6caf0af5d0ed9428da102eb22c9b98ee9da292e9f",
    MODEL + "/llm_config.json": "996215c9f9c5b3aee473db186c15e05b179105be9a80ef94b44f1577ea48528a",
    MODEL + "/tokenizer.txt": "e8ab663d07ee45b4448db392d65b4e3b2d382489083157e0debd300e4cb31401",
    MODEL + "/tokenizer-android-lf.txt": "77009625db0b18e74315dd89557c76feaab57e1d54e3240b442649a3c16ec4fb",
}
EXPECTED_CONFIG = {
    "base_dir": MODEL + "/", "tokenizer_file": "tokenizer.txt",
    "backend_type": "cpu", "thread_num": 4, "precision": "low", "memory": "low",
    "sampler_type": "greedy", "use_mmap": True, "use_cached_mmap": True,
    "kvcache_mmap": False, "reuse_kv": True, "prompt_cache": True,
    "chunk": 0, "attention_mode": 8,
}


class Session(KvSession):
    release = "/data/local/tmp/MNN/framework-query-chat-candidate-20261011-v2"
    record_root = ROOT / "results/android/query-scale-followup-20261011/chat-sessions"
    binary_path = BUILD / "phone_chat_query_worker-stripped"
    manifest_path = RECORDS / "build.json"
    worker_source = RECORDS / "phone_chat_query_worker.cpp"
    history_mode = "kv_prefix_query_v2"
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
    return kv_main(Session, description="手机本机候选多轮聊天（增量KV）；保持默认聊天入口不变。", loaded_message="模型已加载；本机候选多轮聊天（增量KV），/new新对话，/history看历史，/exit退出。")


if __name__ == "__main__":
    raise SystemExit(main())
