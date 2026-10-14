"""Fixed-forward isolated Attention bridge: replace exactly two archive members."""
import argparse,hashlib,json,re,shlex,shutil,subprocess
from pathlib import Path
from build_runtime_dispatch import archive_members
ROOT=Path(__file__).resolve().parents[2]
ALIAS=Path('C:/Users/32065/AppData/Local/Temp/mnn-w4-contract-project-20261013')
PUBLIC=ROOT/'build/mnn-android-source'
GUARD=ROOT/'build/kv-chat-delivery-20261007/libMNN-candidate.a'
GUARD_SHA='5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf'
FILES=['express/module/StaticModule.cpp','source/backend/cpu/CPUAttention.cpp']
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,x):Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def replace(s,a,b):
 if s.count(a)!=1:raise ValueError('Nonunique patch anchor '+a[:80])
 return s.replace(a,b)
def patch(rel,s):
 s='#include "prefill_trunk_forward_trace.hpp"\n'+s
 if rel.endswith('StaticModule.cpp'):
  anchor='''                if (exe == nullptr) {
                    exe.reset(backend->onCreate({}, {}, op));'''
  bridge='''                if (exe == nullptr && PrefillTrunkBridge::enabled() && config.base &&
                    op->type() == OpType_Attention && op->main_type() == OpParameter_AttentionParam &&
                    op->main_as_AttentionParam()->kv_shared_layer_index() < 0) {
                    bool bridged = cloneBaseExecution(exe, base_executions, op, backend, backupBackend);
                    PrefillTrunkBridge::attempt(op->name() ? op->name()->c_str() : nullptr, bridged);
                }
'''
  s=replace(s,anchor,bridge+anchor)
 else:
  anchor='''    *dst = tmp;
    return true;
}

CPUAttention::CPUAttention'''
  log='''    auto traceParam = op->main_as_AttentionParam();
    PrefillTrunkBridge::clone(op->name() ? op->name()->c_str() : nullptr,
        this, tmp, mMeta, tmp->mMeta, mKVCacheManager.get(), tmp->mKVCacheManager.get(),
        mKVCacheManager->kvLength(), tmp->mKVCacheManager->kvLength(),
        traceParam ? traceParam->kv_shared_layer_index() : -2);
'''
  log += "    PrefillTrunkForward::name(this, op->name() ? op->name()->c_str() : nullptr);\n    PrefillTrunkForward::name(tmp, op->name() ? op->name()->c_str() : nullptr);\n"
  s=replace(s,anchor,log+anchor)
  start=s.index('ErrorCode CPUAttention::onExecute(');end=s.index('bool CPUAttention::onClone(',start)
  segment=s[start:end]
  trace=lambda boundary,code: '    PrefillTrunkForward::execute("'+boundary+'", this, mMeta, mKVCacheManager.get(), mKVCacheManager->kvLength(), mMeta ? mMeta->previous : 0, mMeta ? mMeta->add : 0, mMeta ? mMeta->remove : 0, mMeta ? mMeta->n_reserve : 0, mMeta ? mMeta->file_flag : -1, '+code+');\n'
  anchor='const std::vector<Tensor*>& outputs) {\n'
  segment=replace(segment,anchor,anchor+trace('before','-1'))
  segment=replace(segment,'            return OUT_OF_MEMORY;',trace('after','OUT_OF_MEMORY')+'            return OUT_OF_MEMORY;')
  segment=replace(segment,'    return NO_ERROR;',trace('after','NO_ERROR')+'    return NO_ERROR;')
  s=s[:start]+segment+s[end:]
 return s

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--record-tag',default='forward-build-v1');a=ap.parse_args()
 if not re.fullmatch(r'[A-Za-z0-9_-]+',a.record_tag):raise ValueError('bad tag')
 record=ROOT/'results/android/prefill-head-elision-20261013'/a.record_tag
 build=ALIAS/'build/prefill-head-elision-20261013'/a.record_tag
 record.mkdir(parents=True,exist_ok=False);build.mkdir(parents=True,exist_ok=False);source=record/'source';source.mkdir()
 shutil.copyfile(__file__,record/'executed-build-script.py')
 if sha(GUARD)!=GUARD_SHA:raise ValueError('guard changed')
 fixed=[GUARD,*[PUBLIC/p for p in FILES]];before={str(p):sha(p) for p in fixed};save(record/'fixed-before.json',before)
 original=json.loads((ROOT/'results/android/chat-session-20261007/build.json').read_text(encoding='utf-8-sig'))
 original['arguments']=[x.replace('mnn-android-project-32065-20261006','mnn-w4-contract-project-20261013') for x in original['arguments']]
 compiler=original['arguments'][0];ar=Path(compiler).parent/'llvm-ar.exe'
 for name in ['prefill_trunk_bridge_trace.hpp','prefill_trunk_forward_trace.hpp','prefill_trunk_forward_probe.cpp']:
  shutil.copyfile(ROOT/'apps/android'/name,build/name);shutil.copyfile(ROOT/'apps/android'/name,source/name)
 ninja=(ROOT/'build/mnn-3.6.1-android-arm64-cpu-clean-release/build.ninja').read_text();exits=[]
 def run(label,cmd):
  save(record/(label+'-command.json'),cmd);r=subprocess.run(cmd,capture_output=True,stdin=subprocess.DEVNULL,cwd=ROOT)
  (record/(label+'-stdout.log')).write_bytes(r.stdout);(record/(label+'-stderr.log')).write_bytes(r.stderr);(record/(label+'-exit.code')).write_text(str(r.returncode)+'\n');exits.append({'label':label,'actual_exit':r.returncode});save(record/'progress.json',exits)
  if r.returncode:raise RuntimeError(label+' failed')
  return r.stdout
 names=run('archive-list-before',[str(ar),'t',str(GUARD)]).decode().splitlines();objects=[];snapshots=[]
 for i,rel in enumerate(FILES):
  name=Path(rel).name;member=name+'.o'
  if names.count(member)!=1:raise ValueError('nonunique member')
  text=patch(rel,(PUBLIC/rel).read_text(encoding='utf8'))
  (build/name).write_text(text,encoding='utf8');(source/name).write_text(text,encoding='utf8');(source/(name+'.original')).write_bytes((PUBLIC/rel).read_bytes())
  blocks=[b for b in ninja.split('\n\nbuild ') if re.search(r'^.*[/]'+re.escape(member)+r':',b.split('\n')[0])]
  if len(blocks)!=1:raise ValueError('nonunique ninja block')
  fields=dict(re.findall(r'^  (DEFINES|FLAGS|INCLUDES) = (.*)$',blocks[0],re.M))
  flags=shlex.split(' '.join(fields[k] for k in ['DEFINES','FLAGS','INCLUDES']))
  flags=[f.replace('mnn-android-project-32065/','mnn-w4-contract-project-20261013/').replace('mnn-android-project-32065-20261006/','mnn-w4-contract-project-20261013/') for f in flags]
  obj=build/member;run('compile-'+str(i+1),[compiler,*flags,'-I'+str(build),'-I'+str(PUBLIC/Path(rel).parent),'-c',str(build/name),'-o',str(obj)]);objects.append(obj)
  snapshots.append({'source':rel,'original_sha256':sha(PUBLIC/rel),'patched_sha256':sha(build/name),'object_sha256':sha(obj),'member':member,'ninja_flags':fields})
 archive=build/'libMNN-prefill-trunk-bridge.a';shutil.copyfile(GUARD,archive);run('archive-replace',[str(ar),'r',str(archive),*map(str,objects)])
 base=archive_members(GUARD);current=archive_members(archive);replaced={p.name for p in objects}
 if len(base)!=len(current):raise ValueError('member count changed')
 changed=[]
 for old,new in zip(base,current):
  if old['name']!=new['name']:raise ValueError('member ordering changed')
  if old!=new:
   if old['name'] not in replaced:raise ValueError('untouched member bytes changed')
   changed.append(old['name'])
 if set(changed)!=replaced:raise ValueError('replacement mismatch')
 save(record/'archive-audit.json',{'before':base,'after':current,'changed':changed,'untouched_count':len(base)-2,'untouched_byte_hash_equal':True})
 cmd=list(original['arguments']);idx=next(i for i,x in enumerate(cmd) if x.endswith('phone_chat_worker.cpp'));cmd[idx]=str(build/'prefill_trunk_forward_probe.cpp');cmd[cmd.index('-Wl,--whole-archive')+1]=str(archive);binary=build/'prefill_trunk_forward_probe';cmd[cmd.index('-o')+1]=str(binary);cmd+=['-I'+str(build),'-I'+str(ALIAS/'build/mnn-android-source/source')]
 run('link-probe',cmd)
 after={str(p):sha(p) for p in fixed};save(record/'fixed-after.json',after)
 if before!=after:raise ValueError('original fixed assets changed')
 save(record/'build.json',{'binary_path':str(binary),'binary_sha256':sha(binary),'guard_library_sha256':GUARD_SHA,'candidate_library_path':str(archive),'candidate_library_sha256':sha(archive),'source_snapshots':snapshots,'source_sha256':sha(build/'prefill_trunk_forward_probe.cpp'),'helper_sha256':sha(build/'prefill_trunk_forward_trace.hpp'),'old_bridge_helper_sha256':sha(build/'prefill_trunk_bridge_trace.hpp'),'class_layout_changed':False,'model_forward_count':0,'deployment_performed':False,'scope':'fixed-forward correctness prototype; not yet device executed','actual_exits':exits,'original_assets_unchanged':True})
 print(json.dumps({'built':True,'manifest':str(record/'build.json'),'model_forward_count':0}))
if __name__=='__main__':main()
