"""Optional full-history chat with a persistent prefill execution plan."""
import argparse
import json
from pathlib import Path
import sys

from phone_infer import ROOT
import phone_chat_session as full
from phone_chat_metrics import FullStatsSession

STAGE = "plan-cache-chat-20261015"
BUILD = "build-v2"
COMMIT = "d407447ed56c4121a11ccbd266dc184ca1ead0c2"
LIBRARY_SHA = "7d6147a6c41af78572ba4704c83a4c719aba01cc43c5661721e252c9213291fa"
HELPER_SHA = "f53b15b844e6eb20d3c309ac2b7fa953016b311b8453aa06b3877b5e68d13e04"
HEADER_SHA = "6ae9c5e405de20e92b91dc49ab86dbd2e3b1774d3755ff60034a5e1727108d61"


def resolve_plan_build(root=None):
    """Resolve a single frozen profile locally, before constructing ADB."""
    root = Path(ROOT if root is None else root).resolve()
    binary_rel = f"build/{STAGE}/{BUILD}/phone_chat_plan_worker"
    source_rel = "apps/android/phone_chat_plan_worker.cpp"
    helper_rel = "apps/android/prefill_trunk_reuse.hpp"
    library_rel = "build/prefill-trunk-reuse-20261014/performance-build-v3/libMNN-prefill-trunk-reuse.a"
    performance_rel = "results/android/prefill-trunk-reuse-20261014/performance-build-v3/build.json"
    manifest = root / f"results/android/{STAGE}/{BUILD}/build.json"

    def suffix(value, relative):
        return (isinstance(value, str) and
                ".." not in value.replace("\\", "/").split("/") and
                value.replace("\\", "/").endswith("/" + relative))

    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        required = {"source_commit": COMMIT, "variant": "full_plan", "chunk": 32,
                    "metrics_version": 1, "plan_cache_version": 1, "diagnostic": False,
                    "exit": 0, "actual_exit": 0, "class_layout_changed": False,
                    "core_not_recompiled": True, "original_assets_unchanged": True,
                    "source_path": source_rel, "helper_path": helper_rel,
                    "performance_manifest_path": performance_rel}
        if not isinstance(data, dict):
            raise ValueError("计划构建manifest必须是对象")
        for key, value in required.items():
            if type(data.get(key)) is not type(value) or data[key] != value:
                raise ValueError("计划构建身份不匹配: " + key)
        for key, relative in (("binary", binary_rel), ("source", source_rel),
                              ("helper", helper_rel), ("library", library_rel),
                              ("performance_manifest", performance_rel)):
            if not (root / relative).resolve().is_relative_to(root):
                raise ValueError("计划构建文件越出项目目录")
            if data.get(key + "_sha256") != full.sha(root / relative):
                raise ValueError("计划构建文件身份不匹配: " + key)
        if data["library_sha256"] != LIBRARY_SHA or data["helper_sha256"] != HELPER_SHA:
            raise ValueError("计划构建不是已验收的库与辅助代码")
        perf = json.loads((root / performance_rel).read_text(encoding="utf-8-sig"))
        if (perf.get("diagnostic") is not False or perf.get("class_layout_changed") is not False or
                perf.get("candidate_library_sha256") != LIBRARY_SHA or
                perf.get("helper_sha256") != HELPER_SHA):
            raise ValueError("计划性能库证明不匹配")
        command = data.get("arguments")
        if (not isinstance(command, list) or not command or
                any(not isinstance(x, str) for x in command) or
                command.count("-DPREFILL_REUSE_DIAGNOSTIC=0") != 1 or
                any(x.startswith("-DPREFILL_REUSE_DIAGNOSTIC=") and
                    x != "-DPREFILL_REUSE_DIAGNOSTIC=0" for x in command) or
                command.count("-o") != 1 or command.count("-Wl,--whole-archive") != 1 or
                not suffix(command[command.index("-o") + 1], binary_rel) or
                not suffix(command[command.index("-Wl,--whole-archive") + 1], library_rel) or
                not suffix(data["binary_path"], binary_rel) or
                not suffix(data["library_path"], library_rel)):
            raise ValueError("计划构建命令与固定库/输出不一致")
        snapshot_rel = f"build/{STAGE}/{BUILD}/phone_chat_plan_worker.cpp"
        sources = [arg for arg in command if arg.endswith(".cpp")]
        if (len(sources) != 1 or not suffix(sources[0], snapshot_rel) or
                full.sha(root / snapshot_rel) != data["source_sha256"]):
            raise ValueError("计划构建实际输入源码快照不匹配")
        header_rel = "build/mnn-android-source/transformers/llm/engine/include/llm/llm.hpp"
        fixed_before = json.loads((manifest.parent / "fixed-before.json").read_text(encoding="utf-8-sig"))
        fixed_after = json.loads((manifest.parent / "fixed-after.json").read_text(encoding="utf-8-sig"))
        if (fixed_before != fixed_after or full.sha(root / header_rel) != HEADER_SHA or
                sum(suffix(path, header_rel) and value == HEADER_SHA
                    for path, value in fixed_before.items()) != 1):
            raise ValueError("计划构建公共头文件/固定资产证明不匹配")
        if (json.loads((manifest.parent / "plan-command.json").read_text(encoding="utf-8-sig")) != command or
                (manifest.parent / "plan-exit.code").read_text().strip() != "0"):
            raise ValueError("计划构建原始命令/退出记录不一致")
    except (OSError, KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("计划构建缺失或记录非法: " + str(error)) from error
    return {"manifest_sha256": full.sha(manifest), "binary_sha256": data["binary_sha256"],
            "source_sha256": data["source_sha256"], "helper_sha256": HELPER_SHA,
            "library_sha256": LIBRARY_SHA,
            "performance_manifest_sha256": data["performance_manifest_sha256"]}


class PlanCacheSession(FullStatsSession):
    release = f"/data/local/tmp/MNN/{STAGE}/plan"
    record_root = ROOT / f"results/android/{STAGE}/plan-sessions"
    binary_path = ROOT / f"build/{STAGE}/{BUILD}/phone_chat_plan_worker"
    manifest_path = ROOT / f"results/android/{STAGE}/{BUILD}/build.json"
    worker_source = ROOT / "apps/android/phone_chat_plan_worker.cpp"
    plan_requested = True
    extra_config = {"chunk": 32, "backend_type": "cpu", "thread_num": 4,
                    "precision": "low", "memory": "low", "attention_mode": 8,
                    "dynamic_option": 0, "sampler_type": "greedy", "use_mmap": False,
                    "use_cached_mmap": False, "kvcache_mmap": False, "reuse_kv": False,
                    "prompt_cache": False, "prefill_head_elision": True}

    def __init__(self, serial=None, record_parent=None, max_context_tokens=512,
                 profile_path=None, adb_path=None):
        if type(max_context_tokens) is not int or not 1 <= max_context_tokens <= 512:
            raise ValueError("主干计划复用入口的部署token预算为1..512")
        if adb_path is not None and profile_path is None:
            raise ValueError("--adb-path仅用于明确的--plan-profile运行包")
        self.profile_path = Path(profile_path).expanduser().absolute() if profile_path is not None else None
        self.profile = self.resolve_runtime()
        chosen_adb = None
        if self.profile_path is not None:
            from phone_plan_profile import resolve_adb_path
            chosen_adb = resolve_adb_path(adb_path)
            self.binary_path = Path(self.profile["binary_path"])
            self.manifest_path = Path(self.profile["manifest_path"])
            self.worker_source = Path(self.profile["worker_source_path"])
            self.record_root = ROOT / "results/android/portable-plan-profile-20261015/sessions"
        self._plan_seen = False
        super().__init__(serial, record_parent, max_context_tokens=max_context_tokens)
        if chosen_adb is not None:
            self.adb.exe = chosen_adb
            self.record["runtime_profile"] = dict(self.profile)
            self.record["adb_path"] = str(chosen_adb)
        self.record["plan_build"] = self.profile
        self.record["plan_client_sha256"] = full.sha(__file__)
        self.persist()

    def resolve_runtime(self):
        if self.profile_path is None:
            return resolve_plan_build()
        from phone_plan_profile import resolve_profile
        return resolve_profile(self.profile_path)

    def start(self):
        if self.resolve_runtime() != self.profile:
            raise ValueError("计划构建在启动前发生变化；未连接手机")
        super().start()
        ready = self.record["ready"]
        required = {"feature": "persistent_trunk_plan", "plan_cache_requested": self.plan_requested,
                    "chunk": 32, "reuse_kv": False, "plan_cache_version": 1}
        if any(type(ready.get(k)) is not type(v) or ready[k] != v for k, v in required.items()):
            self.record["status"] = "ready_profile_mismatch"
            self.persist()
            raise RuntimeError("worker主干计划入口身份不匹配")
        return self

    def validate_response(self, response):
        super().validate_response(response)
        p = response.get("plan_cache")
        bools = ("requested", "eligible", "prepared", "cache_hit")
        ints = ("version", "trunk_count", "full_count", "decode_count",
                "route_check_us", "prepare_us", "release_us")
        if (not isinstance(p, dict) or any(type(p.get(k)) is not bool for k in bools) or
                any(type(p.get(k)) is not int or p[k] < 0 for k in ints) or
                p["version"] != 1 or p["requested"] is not self.plan_requested):
            raise RuntimeError("主干计划响应协议非法")
        blocks = (response["input_tokens"] + 31) // 32
        optimized = self.plan_requested and blocks > 1
        decode = response["metrics"]["decode_steps"]
        expected = {"eligible": optimized, "prepared": optimized,
                    "cache_hit": optimized and self._plan_seen,
                    "trunk_count": blocks - 1 if optimized else 0,
                    "full_count": (1 if optimized else blocks) + decode,
                    "decode_count": decode}
        if any(p[k] != v for k, v in expected.items()):
            raise RuntimeError("主干计划实际执行路径/命中计数不匹配")

    def ask(self, *args, **kwargs):
        response = super().ask(*args, **kwargs)
        # Do not change cache expectations until the base transaction commits.
        if response["plan_cache"]["prepared"]:
            self._plan_seen = True
        self.last_stats["plan_cache"] = dict(response["plan_cache"])
        self.persist()
        full.save(self.directory / (response["id"] + ".json"), self.record["requests"][-1])
        return response

    def format_stats(self):
        text = super().format_stats()
        if self.last_stats is None or "plan_cache" not in self.last_stats:
            return text
        cache = self.last_stats["plan_cache"]
        route = ("已复用" if cache["cache_hit"] else "首次准备" if cache["prepared"] else
                 "短输入直接完整执行" if cache["requested"] else "未启用")
        return text + "\n[执行计划] " + route + "；上方复用token指KV，本模式按完整历史重算。"

    def close(self):
        result = super().close()
        if self.record.get("worker_exit") == 0 and "ready" in self.record:
            try:
                events = [json.loads(line) for line in
                          (self.directory / "worker-stdout.log").read_text(encoding="utf-8").splitlines()
                          if line.startswith('{"event":')]
                stopped = [e for e in events if e.get("event") == "stopped"]
                closed = [e for e in events if e.get("event") == "plan_cache_closed"]
                if (len(stopped) != 1 or len(closed) != 1 or
                        type(stopped[0].get("pid")) is not int or
                        stopped[0].get("pid") != self.record["ready"]["pid"] or
                        type(stopped[0].get("load_count")) is not int or
                        stopped[0].get("load_count") != 1 or
                        type(stopped[0].get("exit")) is not int or stopped[0].get("exit") != 0 or
                        type(closed[0].get("registry_owner_count")) is not int or
                        closed[0]["registry_owner_count"] != 0):
                    raise ValueError("计划缓存释放未确认")
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.record["plan_close_error"] = str(error)
                if self.record.get("status") != "closed_with_unconfirmed_request":
                    self.record["status"] = "stopped_with_error"
                self.persist()
                raise RuntimeError("worker退出/计划缓存释放证据不完整") from error
        return result


class BaselinePlanSession(PlanCacheSession):
    """Same-library control for acceptance checks; no public baseline CLI."""
    plan_requested = False
    release = f"/data/local/tmp/MNN/{STAGE}/baseline"
    record_root = ROOT / f"results/android/{STAGE}/baseline-sessions"
    extra_config = {**PlanCacheSession.extra_config, "prefill_head_elision": False}


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--max-context-tokens", type=int, default=512)
    parser.add_argument("--plan-profile")
    parser.add_argument("--adb-path")
    args, forwarded = parser.parse_known_args()
    if not 1 <= args.max_context_tokens <= 512:
        parser.error("主干计划复用入口的部署token预算为1..512")
    if args.adb_path is not None and args.plan_profile is None:
        parser.error("--adb-path需要--plan-profile")
    class SelectedPlanSession(PlanCacheSession):
        def __init__(self, *values, **kwargs):
            super().__init__(*values, profile_path=args.plan_profile, adb_path=args.adb_path, **kwargs)
    original = sys.argv
    try:
        sys.argv = [original[0], *forwarded, "--max-context-tokens", str(args.max_context_tokens)]
        return full.main(session_type=SelectedPlanSession, loaded_message=(
            "模型已加载；多轮聊天（完整重算历史＋主干计划复用），/new新对话，/history看历史，/stats看统计，/exit退出。"))
    finally:
        sys.argv = original


if __name__ == "__main__":
    raise SystemExit(main())
