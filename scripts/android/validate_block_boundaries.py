"""Real-model guard boundary test via public token-ID adapter, never natural QA."""
import argparse
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
from phone_infer import Adb, ROOT, MODEL, model_identity
from phone_chat_query_session import EXPECTED_MODEL_IDENTITY
from validate_block_policy import LIB, LIB_SHA, snapshot_helpers
from validate_query_scale_production import ALIAS, WORKER, read, save, sha, require, audit_pair
from prepare_pr_cache_stripped_tests import verify_derived
from time_query_scale_production import durable_process

ADAPTER = r'''
 std::vector<int> fixture; std::string fixtureId; int encodeCalls=0;
 std::vector<int> tokenizer_encode(const std::string& query) override {
  Document d;d.SetObject();auto&a=d.GetAllocator();
  d.AddMember("fixture_id",Value(fixtureId.c_str(),a),a);
  d.AddMember("call_index",encodeCalls,a);d.AddMember("query_bytes",int(query.size()),a);
  ids(d,"returned_ids",fixture);write(dir+"/encode-"+std::to_string(encodeCalls++)+".json",d);
  return fixture;
 }
'''
MAIN = r'''
int main(int argc,char**argv) {
 if(argc!=5)return 2;
 std::ifstream f(argv[2]);std::string data((std::istreambuf_iterator<char>(f)),std::istreambuf_iterator<char>());
 Document spec;spec.Parse(data.c_str());if(spec.HasParseError()||!spec.IsArray()||spec.Empty())return 3;
 bool cold=std::string(argv[3])=="cold";if(!cold&&std::string(argv[3])!="reuse")return 3;
 int turn=0,loads=0,caseIndex=0;
 for(auto&t:spec.GetArray()) {
  if(!t.IsObject()||!t.HasMember("id")||!t["id"].IsString()||!t.HasMember("budget")||!t["budget"].IsInt()||t["budget"].GetInt()!=2)return 3;
  std::unique_ptr<Capture> llm(new Capture(argv[1]));if(!llm->load())return 4;++loads;
  llm->set_config(cold?R"({"reuse_kv":false,"prompt_cache":false,"async":false,"chunk":32,"jinja":{"context":{"enable_thinking":false}}})":R"({"reuse_kv":true,"prompt_cache":true,"async":false,"chunk":32,"jinja":{"context":{"enable_thinking":false}}})");
  for(int phase=0;phase<2;++phase) {
   const char* key=phase?"new_input_ids":"old_input_ids";
   if(!t.HasMember(key)||!t[key].IsArray()||t[key].Empty())return 3;
   llm->fixture.clear();for(auto&v:t[key].GetArray()){if(!v.IsInt()||v.GetInt()<0||v.GetInt()>=151936)return 3;llm->fixture.push_back(v.GetInt());}
   llm->dir=std::string(argv[4])+"/turn-"+std::to_string(turn);llm->step=0;llm->encodeCalls=0;llm->fixtureId=t["id"].GetString();
   // Placeholder text deliberately does not pretend to encode these fixtures.
   // The actual library ChatMessages guard sees the adapter-returned token IDs.
   ChatMessages msgs;msgs.emplace_back("user",phase?"adapter boundary new":"adapter boundary old");
   int beforeSeq=llm->getContext()->all_seq_len;
   std::ostringstream out;llm->response(msgs,&out,"",t["budget"].GetInt());auto c=llm->getContext();
   Document d;d.SetObject();auto&a=d.GetAllocator();ids(d,"full_input_ids",llm->fixture);ids(d,"output_ids",c->output_tokens);
   d.AddMember("text",Value("Token-ID adapter: text semantics not scored",a),a);
   d.AddMember("text_semantics_scored",false,a);
   d.AddMember("fixture_id",Value(t["id"].GetString(),a),a);d.AddMember("case_index",caseIndex,a);d.AddMember("phase",phase,a);
   d.AddMember("status",int(c->status),a);d.AddMember("sample_count",llm->step,a);d.AddMember("encode_calls",llm->encodeCalls,a);
   d.AddMember("before_all_seq_len",beforeSeq,a);d.AddMember("prompt_len",c->prompt_len,a);d.AddMember("all_seq_len",c->all_seq_len,a);
   d.AddMember("gen_seq_len",c->gen_seq_len,a);d.AddMember("budget",t["budget"].GetInt(),a);
   write(llm->dir+"/turn.json",d);if(c->status!=LlmStatus::NORMAL_FINISHED&&c->status!=LlmStatus::MAX_TOKENS_FINISHED)return 5;
   if(llm->encodeCalls!=1)return 6;++turn;
  }
  ++caseIndex;
 }
 printf("MODEL_LOAD_COUNT=%d\nCOMPLETED_TURNS=%d\nCOMPLETED_CASES=%d\n",loads,turn,caseIndex);return 0;
}
'''
SOURCE = WORKER[:WORKER.index('int main(')].replace(' std::string dir; int step=0;', ' std::string dir; int step=0;'+ADAPTER)+MAIN


def build(d, fixtures):
    require(sha(LIB)==LIB_SHA,'Frozen guard library differs')
    cases=read(fixtures/'cases.json');require(len(cases)==12,'Frozen12 pairs required')
    d.mkdir(parents=True,exist_ok=False)
    shutil.copyfile(__file__,d/'executed.py');snapshot_helpers(d)
    shutil.copyfile(fixtures/'cases.json',d/'cases.json');shutil.copyfile(fixtures/'provenance.json',d/'provenance.json')
    b=ALIAS/'build'/('block-boundaries-'+d.parent.name+'-'+d.name);b.mkdir(exist_ok=False)
    source=b/'worker.cpp';source.write_bytes(SOURCE.encode('utf-8'));shutil.copyfile(source,d/'worker.cpp')
    integration=read(ROOT/'build/framework-integration-20261006/build.json')
    require(integration['source_commit']=='d407447ed56c4121a11ccbd266dc184ca1ead0c2','Fixed source differs')
    flags=list(next(r['arguments'] for r in integration['drivers'] if r['kind']=='demo'))
    flags+=['-I'+str(ALIAS/'build/mnn-android-source/transformers/llm/engine/src')]
    binary=b/'worker';cmd=[integration['compiler'],*flags,'-fPIE','-pie','-static-libstdc++',str(source),'-Wl,--whole-archive',str(LIB),'-Wl,--no-whole-archive','-pthread','-llog','-landroid','-lm','-latomic','-o',str(binary)]
    proc=subprocess.run(cmd,capture_output=True);(d/'compile-stdout.log').write_bytes(proc.stdout);(d/'compile-stderr.log').write_bytes(proc.stderr)
    save(d/'compile.json',{'command':cmd,'exit':proc.returncode,'device_executions':0});require(proc.returncode==0,'Build failed; original evidence retained')
    stripped=b/'worker-stripped';shutil.copyfile(binary,stripped)
    cmd=[str(Path(integration['compiler']).parent/'llvm-strip.exe'),'--strip-all',str(stripped)]
    proc=subprocess.run(cmd,capture_output=True);(d/'strip.log').write_bytes(proc.stdout+proc.stderr);require(proc.returncode==0,'Strip failed')
    elf=verify_derived(binary,stripped)
    save(d/'build.json',{'source_commit':integration['source_commit'],'library_sha256':LIB_SHA,'worker_path':str(source),'worker_sha256':sha(source),'deployment_path':str(stripped),'deployment_sha256':sha(stripped),'unstripped_sha256':sha(binary),'elf_identity':elf,'allocated_sections_exact':True,'cases_sha256':sha(d/'cases.json'),'public_sample_override':True,'public_tokenizer_encode_adapter':True,'attention_modified':False,'device_executions':0})
    print('OFFLINE_BUILD_ONLY; zero model executions')


def rebind(d, prepared, fixtures):
    m=read(prepared/'build.json');binary=Path(m['deployment_path'])
    require(sha(binary)==m['deployment_sha256'] and sha(Path(m['worker_path']))==m['worker_sha256'],'Reviewed build changed')
    require(m['library_sha256']==LIB_SHA==sha(LIB),'Frozen library differs')
    cases=read(fixtures/'cases.json');old=read(prepared/'cases.json')
    require(len(cases)==13 and cases[:12]==old and cases[-1]['expected_keep']==32,'New fixture relation differs')
    d.mkdir(parents=True,exist_ok=False);shutil.copyfile(__file__,d/'executed.py');snapshot_helpers(d)
    for name in ('cases.json','provenance.json'):shutil.copyfile(fixtures/name,d/name)
    shutil.copyfile(prepared/'worker.cpp',d/'worker.cpp')
    save(d/'prior-build-binding.json',m)
    m.update(cases_sha256=sha(d/'cases.json'),planned_cases=len(cases),planned_requests_per_mode=2*len(cases),planned_loads_per_mode=len(cases),
             reused_reviewed_elf=True,prior_prepared=str(prepared),recompile=False,device_executions=0)
    save(d/'build.json',m)
    print('OFFLINE_REBIND_ONLY; same reviewed ELF; zero model executions')


def execute(d, prepared, serial):
    m=read(prepared/'build.json');require(m['library_sha256']==LIB_SHA==sha(LIB),'Frozen library changed')
    binary=Path(m['deployment_path']);require(sha(binary)==m['deployment_sha256'] and sha(Path(m['worker_path']))==m['worker_sha256'],'Build changed')
    cases=read(prepared/'cases.json');require(len(cases)>0 and sha(prepared/'cases.json')==m['cases_sha256'],'Fixture changed')
    count=len(cases)*2
    d.mkdir(parents=True,exist_ok=False);shutil.copyfile(__file__,d/'executed.py');snapshot_helpers(d)
    for name in ('cases.json','provenance.json'):shutil.copyfile(prepared/name,d/name)
    save(d/'build-binding.json',m);adb=Adb(serial);device=adb.select();require(device['hardware_serial']=='1e7c759e','Wrong phone');save(d/'device.json',device)
    before=model_identity(adb);save(d/'model-before.json',before);require(before==EXPECTED_MODEL_IDENTITY,'Model differs')
    remote='/data/local/tmp/MNN/block-boundaries/'+d.parent.name+'-'+d.name
    save(d/'remote.json',{'remote':remote,'rule':'Unknown submission never repeated'})
    adb.shell('mkdir -p '+remote);adb.push_verified(binary,remote+'/worker');adb.shell('chmod 755 '+remote+'/worker');adb.push_verified(d/'cases.json',remote+'/cases.json')
    config={'base_dir':MODEL+'/','tokenizer_file':'tokenizer.txt','backend_type':'cpu','thread_num':4,'precision':'low','memory':'low','sampler_type':'greedy','use_mmap':True,'use_cached_mmap':True,'kvcache_mmap':False,'async':False,'chunk':32,'attention_mode':8,'reuse_kv':True,'prompt_cache':True}
    save(d/'config.json',config);adb.push_verified(d/'config.json',remote+'/config.json')
    pullroot=ALIAS/'build'/('block-boundaries-pull-'+d.parent.name+'-'+d.name);pullroot.mkdir(exist_ok=False)
    for mode in ('cold','reuse'):
        sd=d/mode;sd.mkdir();rd=remote+'/'+mode
        adb.shell('mkdir -p '+rd+' '+' '.join(rd+'/turn-'+str(i) for i in range(count)))
        result=durable_process(adb,sd,rd,remote+'/worker '+remote+'/config.json '+remote+'/cases.json '+mode+' '+rd)
        require(read(sd/'completion.json')['device_exit']==0,'Device failed; original evidence retained')
        require(re.findall(rb'(?m)^COMPLETED_TURNS=(\d+)\r?$',result.stdout)==[str(count).encode()],'Missing actual count')
        pulled=pullroot/mode;adb.call('pull',rd,str(pulled),timeout=240);shutil.copytree(pulled,sd/'pulled')
    after=model_identity(adb);save(d/'model-after.json',after);require(after==before,'Model changed')
    save(d/'run.json',{'fresh_phone_execution':True,'actual_processes':2,'turns':count,'cases':len(cases),'chunk':32,'library_sha256':LIB_SHA,'worker_sha256':m['worker_sha256'],'cases_sha256':sha(d/'cases.json'),'device':device,'adb_commands':adb.commands})
    audit(d)


def audit(d):
    require(read(d/'model-before.json')==read(d/'model-after.json')==EXPECTED_MODEL_IDENTITY,'Model differs')
    cases=read(d/'cases.json')
    for mode in ('cold','reuse'):
        sd=d/mode;require(read(sd/'completion.json')['device_exit']==0,'Actual device failure')
        raw=(sd/'stdout.log').read_bytes()
        for key,value in (('DEVICE_TIMER_EXIT',0),('MODEL_LOAD_COUNT',len(cases)),('COMPLETED_TURNS',len(cases)*2),('COMPLETED_CASES',len(cases))):
            require(re.findall(('(?m)^'+key+'=(\\d+)\\r?$').encode(),raw)==[str(value).encode()],'Missing actual count/exit '+key)
    rows=audit_pair(d/'cold/pulled',d/'reuse/pulled',len(cases)*2)
    for turn,row in enumerate(rows):
        case=cases[turn//2];phase=turn%2;expected=case['new_input_ids' if phase else 'old_input_ids']
        row['sample_last_chunk_prompt_lengths']=row.pop('query_lengths')
        whole=[]
        for mode in ('cold','reuse'):
            p=d/mode/'pulled'/('turn-'+str(turn));meta=read(p/'turn.json')
            require(meta['full_input_ids']==expected and meta['fixture_id']==case['id'] and meta['case_index']==turn//2 and meta['phase']==phase,'Actual fixture mismatch')
            require(meta['encode_calls']==1 and {x.name for x in p.glob('encode-*.json')}=={'encode-0.json'},'Actual encode count differs')
            enc=read(p/'encode-0.json');require(enc['returned_ids']==expected and enc['fixture_id']==case['id'] and enc['call_index']==0 and enc['query_bytes']>0,'Adapter actual input differs')
            require(meta['text_semantics_scored'] is False,'Adapter scope changed')
            for sample in range(meta['sample_count']):
                vals=[v[0] for v in struct.iter_unpack('<f',(p/('sample-%d.f32'%sample)).read_bytes())]
                selected=read(p/('sample-%d.json'%sample))['natural_token']
                require(vals[selected]==max(vals),'Native greedy token not vocabulary maximum')
            keep=case['expected_keep'] if phase and mode=='reuse' else 0
            require(meta['prompt_len']==len(expected)-keep,'Whole prompt length violates frozen expectedkeep')
            whole.append(meta['prompt_len'])
        row.update(whole_prompt_lengths=whole,expected_keep=case['expected_keep'] if phase else 0,fixture_id=case['id'],phase=phase)
    save(d/'audit.json',{'pass':True,'actual_processes':2,'actual_requests':len(cases)*4,'paired_full_vocab_steps':sum(r['samples'] for r in rows),'rows':rows,'scope':read(d/'provenance.json')['scope'],'not_verified':'natural text QA, actual tokenizer behavior, shape-limit/padding fallback, dynamic generated-token clipping, timing'})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--record-dir',type=Path,required=True);p.add_argument('--fixtures',type=Path);p.add_argument('--prepared',type=Path);p.add_argument('--serial');p.add_argument('--build',action='store_true');p.add_argument('--rebind',action='store_true');p.add_argument('--execute',action='store_true');p.add_argument('--audit-only',action='store_true');a=p.parse_args()
    if a.build:build(a.record_dir.resolve(),a.fixtures.resolve())
    elif a.rebind:rebind(a.record_dir.resolve(),a.prepared.resolve(),a.fixtures.resolve())
    elif a.execute:execute(a.record_dir.resolve(),a.prepared.resolve(),a.serial)
    elif a.audit_only:audit(a.record_dir.resolve())
    else:p.error('Choose build/execute/audit-only')
