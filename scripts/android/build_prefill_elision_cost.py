"""Isolated ordinary response opt-in. No production source or class layout changes."""
import argparse,hashlib,json,re,shlex,shutil,subprocess
from pathlib import Path
from build_runtime_dispatch import archive_members
ROOT=Path(__file__).resolve().parents[2]
ALIAS=Path('C:/Users/32065/AppData/Local/Temp/mnn-w4-contract-project-20261013')
PUBLIC=ROOT/'build/mnn-android-source'
GUARD=ROOT/'build/kv-chat-delivery-20261007/libMNN-candidate.a'
LLM=ROOT/'results/android/kv-chat-delivery-20261007/llm.cpp'
FILES=['transformers/llm/engine/src/llm.cpp','express/module/StaticModule.cpp','source/backend/cpu/CPUAttention.cpp']
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,x):Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def replace(s,a,b):
 if s.count(a)!=1:raise ValueError('Nonunique anchor '+a[:80])
 return s.replace(a,b)
def patch(rel,s):
 # Frozen v5 source is the proven semantic baseline, not regenerated from current public files.
 frozen=ROOT/'results/android/prefill-response-elision-20261014/build-v5/source'/Path(rel).name
 s=frozen.read_text(encoding='utf8').replace('prefill_response_elision.hpp','prefill_elision_cost.hpp')
 if rel.endswith('CPUAttention.cpp'):
  s='\n'.join(line for line in s.split('\n') if 'ResponseElision::execute(' not in line)
  return s
 if rel.endswith('StaticModule.cpp'):return s
 s='\n'.join(line for line in s.split('\n') if not any(x in line for x in ['ResponseElision::begin(', 'ResponseElision::positions(', 'ResponseElision::end(']))
 a=s.index('    if (!ResponseElision::logits(');z=s.index('    mMeta->sync();',a);s=s[:a]+s[z:]
 s=replace(s,'    mMeta->sync();\n    return outputs;\n}\n\nVARP Llm::forward','    mMeta->sync();\n    ++ResponseElision::metrics().full;\n    if (inDecode) ++ResponseElision::metrics().decode;\n    return outputs;\n}\n\nVARP Llm::forward')
 s=replace(s,'        std::shared_ptr<Module> requestTrunk;', '        const auto routeStart = ResponseElision::now();\n        std::shared_ptr<Module> requestTrunk;')
 s=replace(s,'        if (eligible) {','        ResponseElision::metrics().requested=requested;\n        ResponseElision::metrics().eligible=eligible;\n        ResponseElision::metrics().route_check_us += ResponseElision::now()-routeStart;\n        const auto prepareStart=ResponseElision::now();\n        if (eligible) {')
 s='\n'.join('        ResponseElision::metrics().prepare_us += eligible ? ResponseElision::now()-prepareStart : 0;\n        ResponseElision::metrics().prepared=bool(requestTrunk);\n        if (requestTrunk && ResponseElision::metrics().prepared_callback) ResponseElision::metrics().prepared_callback();' if 'std::printf("{\\\"event\\\":\\\"response_route' in line else line for line in s.split('\n'))
 if 'response_route' in s:raise ValueError('response_route removal failed')
 s=replace(s,'                mMeta->sync();\n                updateContext(seq, 0);','                mMeta->sync();\n                ++ResponseElision::metrics().trunk;\n                updateContext(seq, 0);')
 s=replace(s,'        requestTrunk.reset();\n        completePrefixWrite();','        const auto releaseStart=ResponseElision::now();\n        requestTrunk.reset();\n        ResponseElision::metrics().release_us += ResponseElision::now()-releaseStart;\n        completePrefixWrite();')
 return s

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--record-tag',default='build-v1');args=ap.parse_args()
 if not re.fullmatch(r'[A-Za-z0-9_-]+',args.record_tag):raise ValueError('bad tag')
 record=ROOT/'results/android/prefill-elision-cost-20261014'/args.record_tag
 build=ALIAS/'build/prefill-elision-cost-20261014'/args.record_tag
 record.mkdir(parents=True,exist_ok=False);build.mkdir(parents=True,exist_ok=False);source=record/'source';source.mkdir()
 shutil.copyfile(__file__,record/'executed-build-script.py')
 if sha(GUARD)!='5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf' or sha(LLM)!='f9ba43aa7ed1ae183fbf31b40ca14570ed55493cbd4a99e88946863148e30347':raise ValueError('guard identity changed')
 fixed=[GUARD,LLM,PUBLIC/'transformers/llm/engine/include/llm/llm.hpp',*[PUBLIC/p for p in FILES[1:]]];before={str(p):sha(p) for p in fixed};save(record/'fixed-before.json',before)
 orig=json.loads((ROOT/'results/android/chat-session-20261007/build.json').read_text(encoding='utf-8-sig'));orig['arguments']=[x.replace('mnn-android-project-32065-20261006','mnn-w4-contract-project-20261013') for x in orig['arguments']]
 compiler=orig['arguments'][0];ar=Path(compiler).parent/'llvm-ar.exe';ninja=(ROOT/'build/mnn-3.6.1-android-arm64-cpu-clean-release/build.ninja').read_text();exits=[]
 for name in ['prefill_elision_cost.hpp','prefill_elision_cost_worker.cpp']:
  shutil.copyfile(ROOT/'apps/android'/name,build/name);shutil.copyfile(ROOT/'apps/android'/name,source/name)
 def run(label,cmd):
  save(record/(label+'-command.json'),cmd);r=subprocess.run(cmd,capture_output=True,stdin=subprocess.DEVNULL,cwd=ROOT);(record/(label+'-stdout.log')).write_bytes(r.stdout);(record/(label+'-stderr.log')).write_bytes(r.stderr);(record/(label+'-exit.code')).write_text(str(r.returncode)+'\n');exits.append({'label':label,'actual_exit':r.returncode});save(record/'progress.json',exits)
  if r.returncode:raise RuntimeError(label+' failed')
  return r.stdout
 names=run('archive-list-before',[str(ar),'t',str(GUARD)]).decode().splitlines();objects=[];snapshots=[]
 for i,rel in enumerate(FILES):
  name=Path(rel).name;member=name+'.o';original=LLM if i==0 else PUBLIC/rel
  if names.count(member)!=1:raise ValueError('nonunique member')
  text=patch(rel,original.read_text(encoding='utf8'));(build/name).write_text(text,encoding='utf8');(source/name).write_text(text,encoding='utf8');shutil.copyfile(original,source/(name+'.original'))
  blocks=[b for b in ninja.split('\n\nbuild ') if re.search(r'^.*[/]'+re.escape(member)+r':',b.split('\n')[0])]
  if len(blocks)!=1:raise ValueError('nonunique ninja '+member)
  fields=dict(re.findall(r'^  (DEFINES|FLAGS|INCLUDES) = (.*)$',blocks[0],re.M));flags=shlex.split(' '.join(fields[k] for k in ['DEFINES','FLAGS','INCLUDES']))
  flags=[f.replace('mnn-android-project-32065/','mnn-w4-contract-project-20261013/').replace('mnn-android-project-32065-20261006/','mnn-w4-contract-project-20261013/') for f in flags]
  obj=build/member;run('compile-'+str(i+1),[compiler,*flags,'-I'+str(build),'-I'+str(PUBLIC/Path(rel).parent),'-c',str(build/name),'-o',str(obj)]);objects.append(obj);snapshots.append({'source':rel,'original_sha256':sha(original),'patched_sha256':sha(build/name),'object_sha256':sha(obj),'member':member,'ninja_flags':fields})
 archive=build/'libMNN-prefill-elision-cost.a';shutil.copyfile(GUARD,archive);run('archive-replace',[str(ar),'r',str(archive),*map(str,objects)])
 base=archive_members(GUARD);current=archive_members(archive);changed=[a['name'] for a,b in zip(base,current) if a!=b]
 if len(base)!=len(current) or set(changed)!={p.name for p in objects} or any(a['name']!=b['name'] for a,b in zip(base,current)):raise ValueError('archive audit')
 save(record/'archive-audit.json',{'before':base,'after':current,'changed':changed,'untouched_count':len(base)-3,'untouched_byte_hash_equal':True})
 cmd=list(orig['arguments']);idx=next(i for i,x in enumerate(cmd) if x.endswith('phone_chat_worker.cpp'));cmd[idx]=str(build/'prefill_elision_cost_worker.cpp');cmd[cmd.index('-Wl,--whole-archive')+1]=str(archive);binary=build/'prefill_elision_cost_worker';cmd[cmd.index('-o')+1]=str(binary);cmd+=['-I'+str(build),'-I'+str(ALIAS/'build/mnn-android-source/source')];run('link-worker',cmd)
 after={str(p):sha(p) for p in fixed};save(record/'fixed-after.json',after)
 if before!=after:raise ValueError('original changed')
 save(record/'build.json',{'binary_path':str(binary),'binary_sha256':sha(binary),'candidate_library_path':str(archive),'candidate_library_sha256':sha(archive),'source_snapshots':snapshots,'worker_sha256':sha(build/'prefill_elision_cost_worker.cpp'),'helper_sha256':sha(build/'prefill_elision_cost.hpp'),'class_layout_changed':False,'model_forward_count':0,'deployment_performed':False,'actual_exits':exits,'original_assets_unchanged':True})
 print(json.dumps({'built':True,'manifest':str(record/'build.json'),'model_forward_count':0}))
if __name__=='__main__':main()
