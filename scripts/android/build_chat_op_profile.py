"""Compile a separate public-callback worker against the unchanged guarded MNN archive."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
ROOT = Path(__file__).resolve().parents[2]
ALIAS = Path("C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006")
COMMIT = "d407447ed56c4121a11ccbd266dc184ca1ead0c2"
GUARD_SHA = "5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf"
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,v): Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-tag",default="worker-build-v1")
    args=parser.parse_args()
    if not args.record_tag.isascii() or not args.record_tag.replace("-", "").replace("_", "").isalnum(): parser.error("ASCII record tag required")
    records=ROOT/"results/android/op-profile-20261013"/args.record_tag
    build=ALIAS/"build/op-profile-20261013"/args.record_tag
    records.mkdir(parents=True,exist_ok=False);build.mkdir(parents=True,exist_ok=False)
    source=ALIAS/"apps/android/phone_chat_op_profile_worker.cpp"
    binary=build/"phone_chat_worker"
    library=ALIAS/"build/kv-chat-delivery-20261007/libMNN-candidate.a"
    original_path=ROOT/"results/android/chat-session-20261007/build.json"
    original=json.loads(original_path.read_text(encoding="utf-8-sig"))
    cmd=list(original["arguments"])
    source_index=next(i for i,v in enumerate(cmd) if v.endswith("phone_chat_worker.cpp"))
    cmd[source_index]=str(source)
    cmd[cmd.index("-Wl,--whole-archive")+1]=str(library)
    cmd[cmd.index("-o")+1]=str(binary)
    public=ROOT/"build/mnn-android-source"
    head=subprocess.check_output(["git","-C",str(public),"rev-parse","HEAD"]).decode().strip()
    if head!=COMMIT or sha(library)!=GUARD_SHA: raise RuntimeError("Fixed source/library identity mismatch")
    immutable=[library,ROOT/"apps/android/phone_chat_metrics_worker.cpp",ROOT/"apps/android/phone_chat_kv_metrics_worker.cpp",public/"transformers/llm/engine/src/llm.cpp",public/"source/core/Pipeline.cpp",public/"express/module/StaticModule.cpp",public/"express/Executor.cpp"]
    before={str(p):sha(p) for p in immutable}
    save(records/"command.json",cmd)
    result=subprocess.run(cmd,cwd=ROOT,capture_output=True,stdin=subprocess.DEVNULL)
    (records/"stdout.log").write_bytes(result.stdout);(records/"stderr.log").write_bytes(result.stderr)
    (records/"exit.code").write_text(str(result.returncode)+"\n")
    after={str(p):sha(p) for p in immutable}
    header=public/"transformers/llm/engine/include/llm/llm.hpp"
    manifest={"source_commit":head,"source_path":"apps/android/phone_chat_op_profile_worker.cpp","source_sha256":sha(source),"public_header_sha256":sha(header),"public_ar_generation_sha256":sha(public/"transformers/llm/engine/src/speculative_decoding/generate.cpp"),"library_sha256":GUARD_SHA,"arguments":cmd,"binary_path":str(binary),"binary_sha256":sha(binary) if binary.exists() else None,"exit":result.returncode,"actual_compiler_exit":result.returncode,"chunk":32,"enable_debug":True,"reuse_kv":False,"op_profile_version":1,"op_profile_switch":"config.op_profile_enabled","core_not_recompiled":True,"existing_worker_not_overwritten":True}
    save(records/"build.json",manifest);save(records/"immutable-identities.json",{"before":before,"after":after,"unchanged":before==after})
    if result.returncode or before!=after: raise RuntimeError("Build failed or fixed assets changed; evidence retained")
    save(records/"summary.json",{"ok":True,"actual_compiler_exit":0,"model_requests":0,"binary":str(binary),"manifest":str(records/"build.json")})
    print(json.dumps({"ok":True,"actual_compiler_exit":0,"model_requests":0,"manifest":str(records/"build.json")}))
if __name__=="__main__": main()
