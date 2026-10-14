"""Fresh opt-in query-v3 worker build; preserve all accepted default artifacts."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from prepare_pr_cache_stripped_tests import verify_derived

ROOT = Path(__file__).resolve().parents[2]
ALIAS = Path("C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006")
LIBRARY_SHA = "ae3c3f4d03b4b36dbd5371366457c2b5e8e1c21824ce9fbac6521c440f7090e1"
RECORDS = ROOT / "results/android/query-v3-chat-20261011/build-v1"
BUILD = ALIAS / "build/query-v3-chat-candidate-20261011-v1"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    if RECORDS.exists() or BUILD.exists():
        raise RuntimeError("Fresh directories required; old records will not be overwritten")
    config_path = ROOT / "build/framework-integration-20261006/build.json"
    if sha(config_path) != "21dedf6c803a8aa7fa4d48d52f545123ab64c6e23eb114e723faf184477ebfec":
        raise RuntimeError("Compiler manifest identity mismatch")
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    library = ALIAS / "build/query-scale-per-query-20261011-v3/libMNN-query-production.a"
    if sha(library) != LIBRARY_SHA:
        raise RuntimeError("Fixed candidate library identity mismatch")
    source_base = ROOT / "apps/android/phone_chat_kv_worker.cpp"
    text = source_base.read_text(encoding="utf-8")
    edits = [('str(out,"history_mode","kv_block32")', 'str(out,"history_mode","kv_prefix_query_v3")'),
             ('out.AddMember("kv_block_size",32,a)', 'out.AddMember("kv_block_size",0,a)')]
    for old, new in edits:
        if text.count(old) != 1:
            raise RuntimeError("Service source patch precondition mismatch")
        text = text.replace(old, new)
    ids_anchor = '    out.AddMember("input_tokens",static_cast<unsigned>(full.size()),a);'
    if text.count(ids_anchor) != 1:
        raise RuntimeError("Input ID observation patch precondition mismatch")
    ids_line = '    Value fullIds(rapidjson::kArrayType);for(int id:full)fullIds.PushBack(id,a);out.AddMember("full_input_ids",fullIds,a);'
    text = text.replace(ids_anchor, ids_anchor + "\n" + ids_line)
    edits.append([ids_anchor, ids_anchor + "\n" + ids_line])
    RECORDS.mkdir(parents=True)
    BUILD.mkdir(parents=True)
    source = BUILD / "phone_chat_query_v3_worker.cpp"
    source.write_text(text, encoding="utf-8")
    shutil.copyfile(source, RECORDS / source.name)
    shutil.copyfile(source_base, RECORDS / "base-phone_chat_kv_worker.cpp")
    shutil.copyfile(__file__, RECORDS / Path(__file__).name)
    flags = list(next(r["arguments"] for r in config["drivers"] if r["kind"] == "demo"))
    flags += ["-I" + str(ALIAS / "build/mnn-android-source/transformers/llm/engine/src")]
    binary = BUILD / "phone_chat_query_v3_worker"
    command = [config["compiler"], *flags, "-fPIE", "-pie", "-static-libstdc++", str(source),
               "-Wl,--whole-archive", str(library), "-Wl,--no-whole-archive", "-pthread", "-llog",
               "-landroid", "-lm", "-latomic", "-o", str(binary)]
    p = subprocess.run(command, cwd=ROOT, capture_output=True)
    (RECORDS / "stdout.log").write_bytes(p.stdout)
    (RECORDS / "stderr.log").write_bytes(p.stderr)
    (RECORDS / "compile-command.json").write_text(json.dumps({"args": command, "exit": p.returncode}, indent=2), encoding="utf-8")
    if p.returncode:
        raise RuntimeError("Compile failed; original evidence preserved")
    stripped = BUILD / "phone_chat_query_v3_worker-stripped"
    shutil.copyfile(binary, stripped)
    strip = [str(Path(config["compiler"]).parent / "llvm-strip.exe"), "--strip-all", str(stripped)]
    p = subprocess.run(strip, capture_output=True)
    (RECORDS / "strip.log").write_bytes(p.stdout + p.stderr)
    (RECORDS / "strip-command.json").write_text(json.dumps({"args": strip, "exit": p.returncode}, indent=2), encoding="utf-8")
    if p.returncode:
        raise RuntimeError("Strip failed; original evidence preserved")
    elf = verify_derived(binary, stripped)
    if sha(library) != LIBRARY_SHA:
        raise RuntimeError("Library changed during compile")
    manifest = {"source_commit": "d407447ed56c4121a11ccbd266dc184ca1ead0c2",
                "library_path": str(library), "library_sha256": LIBRARY_SHA,
                "source_sha256": sha(source), "base_worker_source_sha256": sha(source_base),
                "worker_source_edits": edits, "binary_sha256": sha(stripped),
                "original_binary_sha256": sha(binary), "original_binary_size": binary.stat().st_size,
                "deployment_binary_size": stripped.stat().st_size, "elf_allocated_sections_exact": True,
                "elf_identity": elf, "history_mode": "kv_prefix_query_v3", "chunk": 0,
                "compile_exit": 0, "strip_exit": 0, "device_tests_executed": 0,
                "scope": "Opt-in Mi11 Qwen3-0.6B CPU low FP16 flash/KV None candidate; not default or all KV modes",
                "config": {"backend_type": "cpu", "precision": "low", "memory": "low", "thread_num": 4,
                           "sampler_type": "greedy", "chunk": 0, "reuse_kv": True, "prompt_cache": True,
                           "kvcache_mmap": False, "attention_mode": 8}, "core_not_recompiled": True}
    (RECORDS / "build.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(stripped)


if __name__ == "__main__":
    main()
