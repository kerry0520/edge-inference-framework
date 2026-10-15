"""Link optional startup-configured plan chat worker against the accepted performance library."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[2]
ALIAS=Path('C:/Users/32065/AppData/Local/Temp/mnn-w4-contract-project-20261013')
PERFORMANCE=ROOT/'results/android/prefill-trunk-reuse-20261014/performance-build-v3/build.json'
LIBRARY_SHA='7d6147a6c41af78572ba4704c83a4c719aba01cc43c5661721e252c9213291fa'
HELPER_SHA='f53b15b844e6eb20d3c309ac2b7fa953016b311b8453aa06b3877b5e68d13e04'
COMMIT='d407447ed56c4121a11ccbd266dc184ca1ead0c2'
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,o):p.write_text(json.dumps(o,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--record-tag',default='build-v1');args=ap.parse_args()
    if not args.record_tag.isascii() or not args.record_tag.replace('-','').replace('_','').isalnum():ap.error('invalid tag')
    records=ROOT/'results/android/plan-cache-chat-20261015'/args.record_tag
    output=ALIAS/'build/plan-cache-chat-20261015'/args.record_tag
    records.mkdir(parents=True,exist_ok=False);output.mkdir(parents=True,exist_ok=False)
    perf=json.loads(PERFORMANCE.read_text(encoding='utf8'))
    library=Path(perf['candidate_library_path']);helper=ROOT/'apps/android/prefill_trunk_reuse.hpp'
    source=ROOT/'apps/android/phone_chat_plan_worker.cpp';old=ROOT/'apps/android/phone_chat_metrics_worker.cpp'
    if perf['diagnostic'] or perf['class_layout_changed'] or sha(library)!=LIBRARY_SHA or sha(helper)!=HELPER_SHA:raise RuntimeError('frozen library/helper identity differs')
    public=ROOT/'build/mnn-android-source'
    head=subprocess.check_output(['git','-C',str(public),'rev-parse','HEAD']).decode().strip()
    if head!=COMMIT:raise RuntimeError('public source identity differs')
    fixed=[library,helper,old,PERFORMANCE,public/'transformers/llm/engine/include/llm/llm.hpp']
    before={str(p):sha(p) for p in fixed};save(records/'fixed-before.json',before)
    # Reuse the exact accepted ABI/compiler/link flags. Only the entry source,
    # output, and local helper include directory change; no core compilation.
    command=json.loads((PERFORMANCE.parent/'link-worker-command.json').read_text(encoding='utf8'))
    si=next(i for i,x in enumerate(command) if x.endswith('prefill_trunk_reuse_worker.cpp'))
    worker=output/'phone_chat_plan_worker.cpp';worker.write_bytes(source.read_bytes())
    (output/helper.name).write_bytes(helper.read_bytes())
    command[si]=str(worker);command[command.index('-o')+1]=str(output/'phone_chat_plan_worker')
    accepted_include='-I'+str(Path(perf['binary_path']).parent)
    command=[('-I'+str(output)) if x==accepted_include else x for x in command]
    save(records/'plan-command.json',command)
    proc=subprocess.run(command,cwd=ROOT,capture_output=True,stdin=subprocess.DEVNULL)
    (records/'plan-stdout.log').write_bytes(proc.stdout);(records/'plan-stderr.log').write_bytes(proc.stderr)
    (records/'plan-exit.code').write_text(str(proc.returncode)+'\n',encoding='ascii')
    after={str(p):sha(p) for p in fixed};save(records/'fixed-after.json',after)
    if before!=after:raise RuntimeError('immutable assets changed')
    binary=output/'phone_chat_plan_worker'
    manifest={'source_commit':head,'variant':'full_plan','chunk':32,'metrics_version':1,'plan_cache_version':1,'diagnostic':False,'exit':proc.returncode,'actual_exit':proc.returncode,'source_path':'apps/android/phone_chat_plan_worker.cpp','source_sha256':sha(source),'helper_path':'apps/android/prefill_trunk_reuse.hpp','helper_sha256':sha(helper),'library_path':str(library),'library_sha256':sha(library),'performance_manifest_path':str(PERFORMANCE.relative_to(ROOT)).replace('\\','/'),'performance_manifest_sha256':sha(PERFORMANCE),'binary_path':str(binary),'binary_sha256':sha(binary) if binary.exists() else None,'class_layout_changed':False,'core_not_recompiled':True,'original_assets_unchanged':before==after,'arguments':command,'model_requests':0,'deployment_performed':False}
    save(records/'build.json',manifest)
    if proc.returncode:raise RuntimeError('entry link failed; evidence retained')
    print(json.dumps({'ok':True,'manifest':str(records/'build.json'),'binary_sha256':manifest['binary_sha256']}))
if __name__=='__main__':main()
