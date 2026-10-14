"""Isolated public-sample-API acceptance; no library instrumentation or deployment promotion.

Build/run/audit are explicit operations. New record directories only; no resume.
Full logits export adds overhead and is NEVER a performance measurement.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess

from phone_infer import Adb, MODEL, ROOT, model_identity
from prepare_pr_cache_stripped_tests import verify_derived

ALIAS = Path('C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006')
V = 151936
WORKER = r'''#include "llm/llm.hpp"
#include "llmconfig.hpp"
#include "rapidjson/document.h"
#include "rapidjson/writer.h"
#include "rapidjson/stringbuffer.h"
#include <cstdio>
#include <cmath>
#include <fstream>
#include <memory>
#include <sstream>
using namespace MNN::Transformer;
using rapidjson::Document; using rapidjson::Value;
static void fail(const char* text){fprintf(stderr,"acceptance error: %s\n",text);std::exit(6);}
static void write(const std::string& path,const Document& d) {
 rapidjson::StringBuffer b;rapidjson::Writer<rapidjson::StringBuffer>w(b);d.Accept(w);
 std::ofstream f(path);f<<b.GetString();f.close();if(!f)fail("metadata write");
}
static void ids(Document& d,const char* key,const std::vector<int>& x) {
 Value a(rapidjson::kArrayType);for(int n:x)a.PushBack(n,d.GetAllocator());d.AddMember(Value(key,d.GetAllocator()),a,d.GetAllocator());
}
class Capture : public Llm {
public:
 Capture(const std::string& c):Llm(std::make_shared<LlmConfig>(c)){}
 std::string dir; int step=0;
 int sample(MNN::Express::VARP logits,int offset=0,int size=0) override {
  MNN::Express::ExecutorScope scope(mExecutor);
  auto info=logits->getInfo();if(!info||info->size!=151936||offset||size)fail("unexpected sampler slice");
  const float* p=logits->readMap<float>();if(!p)fail("logits unreadable");
  for(int i=0;i<151936;++i)if(!std::isfinite(p[i]))fail("nonfinite logits");
  std::string base=dir+"/sample-"+std::to_string(step);
  FILE*f=fopen((base+".f32").c_str(),"wb");if(!f)fail("logits open");
  size_t written=fwrite(p,sizeof(float),151936,f);int closed=fclose(f);if(written!=151936||closed)fail("logits write");
  int token=Llm::sample(logits,offset,size);auto c=getContext();
  Document d;d.SetObject();auto&a=d.GetAllocator();d.AddMember("sample_index",step++,a);d.AddMember("vocab_size",151936,a);
  d.AddMember("natural_token",token,a);d.AddMember("forced",false,a);d.AddMember("all_seq_len",c->all_seq_len,a);
  d.AddMember("prompt_len",c->prompt_len,a);d.AddMember("gen_seq_len",c->gen_seq_len,a);d.AddMember("is_stop",is_stop(token),a);
  Value shape(rapidjson::kArrayType);for(int n:info->dim)shape.PushBack(n,a);d.AddMember("shape",shape,a);write(base+".json",d);return token;
 }
};
int main(int argc,char**argv) {
 if(argc!=5)return 2;
 std::ifstream f(argv[2]);std::string data((std::istreambuf_iterator<char>(f)),std::istreambuf_iterator<char>());
 Document spec;spec.Parse(data.c_str());if(spec.HasParseError()||!spec.IsArray()||spec.Empty())return 3;
 bool cold=std::string(argv[3])=="cold";if(!cold&&std::string(argv[3])!="reuse")return 3;
 std::unique_ptr<Capture> llm;ChatMessages msgs;int turn=0;
 for(auto&t:spec.GetArray()) {
  if(!t.IsObject()||!t.HasMember("question")||!t["question"].IsString()||!t.HasMember("budget")||!t["budget"].IsInt()||t["budget"].GetInt()<1||t["budget"].GetInt()>24)return 3;
  if(!llm||cold){llm.reset(new Capture(argv[1]));if(!llm->load())return 4;}
  llm->set_config(cold?R"({"reuse_kv":false,"prompt_cache":false,"async":false,"jinja":{"context":{"enable_thinking":false}}})":R"({"reuse_kv":true,"prompt_cache":true,"async":false,"jinja":{"context":{"enable_thinking":false}}})");
  msgs.emplace_back("user",t["question"].GetString());auto full=llm->tokenizer_encode(llm->apply_chat_template(msgs));
  llm->dir=std::string(argv[4])+"/turn-"+std::to_string(turn++);llm->step=0;
  std::ostringstream out;llm->response(msgs,&out,"",t["budget"].GetInt());auto c=llm->getContext();
  Document d;d.SetObject();auto&a=d.GetAllocator();ids(d,"full_input_ids",full);ids(d,"output_ids",c->output_tokens);
  d.AddMember("text",Value(out.str().c_str(),a),a);d.AddMember("status",int(c->status),a);d.AddMember("sample_count",llm->step,a);
  d.AddMember("prompt_len",c->prompt_len,a);d.AddMember("all_seq_len",c->all_seq_len,a);d.AddMember("gen_seq_len",c->gen_seq_len,a);d.AddMember("budget",t["budget"].GetInt(),a);
  write(llm->dir+"/turn.json",d);if(c->status!=LlmStatus::NORMAL_FINISHED&&c->status!=LlmStatus::MAX_TOKENS_FINISHED)return 5;
  msgs.emplace_back("assistant",out.str());
 }
 printf("COMPLETED_TURNS=%d\n",turn);return 0;
}
'''


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def read(p):
    return json.loads(Path(p).read_text(encoding='utf-8-sig'))


def save(p, x):
    Path(p).write_text(json.dumps(x, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def audit_pair(left, right, count):
    rows = []
    for turn in range(count):
        paths = [p / f'turn-{turn}' for p in (left, right)]
        a, b = [read(p/'turn.json') for p in paths]
        require(a['full_input_ids'] == b['full_input_ids'] and a['full_input_ids'], 'Different actual full input IDs')
        require(a['sample_count'] == b['sample_count'] and 0 < a['sample_count'] <= a['budget'] == b['budget'], 'Missing/extra sample or invalid budget')
        for p in paths:
            require({f.name for f in p.glob('sample-*.json')} == {f'sample-{i}.json' for i in range(a['sample_count'])},'Unexpected/missing sample metadata')
            require({f.name for f in p.glob('sample-*.f32')} == {f'sample-{i}.f32' for i in range(a['sample_count'])},'Unexpected/missing sample raw')
        natural = [[], []]
        for i in range(a['sample_count']):
            metas = [read(p/f'sample-{i}.json') for p in paths]
            for j, (p, m) in enumerate(zip(paths, metas)):
                require(m['sample_index'] == i and m['vocab_size'] == V and m['shape'] == [1, 1, V] and m['forced'] is False, 'Unexpected sampler metadata')
                require(m['all_seq_len'] == len(a['full_input_ids'])+i and m['gen_seq_len'] == i, 'Position mismatch')
                require(0 < m['prompt_len'] <= len(a['full_input_ids']), 'Invalid query length')
                raw = (p/f'sample-{i}.f32').read_bytes()
                require(len(raw) == V*4, 'Truncated logits')
                require(all(math.isfinite(v[0]) for v in struct.iter_unpack('<f', raw)), 'Nonfinite logits')
                natural[j].append(m['natural_token'])
            require((paths[0]/f'sample-{i}.f32').read_bytes() == (paths[1]/f'sample-{i}.f32').read_bytes(), f'Complete logits differ at turn {turn} step {i}; later different-input steps are not compared')
            require(metas[0]['natural_token'] == metas[1]['natural_token'] and metas[0]['is_stop'] == metas[1]['is_stop'], 'Natural selection differs; later inputs diverge')
        require(a['output_ids'] == natural[0] and b['output_ids'] == natural[1], 'Recorded output IDs differ from natural sampler')
        require(a['text'] == b['text'] and a['status'] == b['status'] and a['status'] in (1, 2), 'Natural text/stop status differs')
        last = read(paths[0]/f"sample-{a['sample_count']-1}.json")
        require((a['status'] == 1 and last['is_stop']) or (a['status'] == 2 and a['sample_count'] == a['budget'] and not last['is_stop']), 'EOS/MAX mislabeled')
        rows.append({'turn': turn, 'full_length': len(a['full_input_ids']), 'query_lengths': [read(p/'sample-0.json')['prompt_len'] for p in paths], 'samples': a['sample_count'], 'status': a['status'], 'all_logits_bytes_equal': True, 'natural_ids_equal': True})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--record-dir', type=Path, required=True)
    ap.add_argument('--candidate-lib', type=Path)
    ap.add_argument('--baseline-lib', type=Path)
    ap.add_argument('--scenario', type=Path, help='JSON array of {question,budget}, 1..24 tokens per turn')
    ap.add_argument('--precision', choices=('low','high'), default='low')
    ap.add_argument('--chunk', type=int, choices=(0,32), default=0)
    ap.add_argument('--serial')
    ap.add_argument('--execute', action='store_true', help='Explicit phone operation after parent review')
    ap.add_argument('--audit-only', action='store_true')
    a = ap.parse_args();d = a.record_dir.resolve()
    if a.audit_only:
        manifest=read(d/'run.json');require(manifest['fresh_phone_execution'] is True, 'No device execution')
        require(read(d/'model-before.json') == read(d/'model-after.json'), 'Model changed')
        pairs = []
        for name, left, right in manifest['pairs']:
            for stage in (left,right):
                log=(d/stage/'stdout.log').read_text(encoding='utf-8')
                require(re.findall(r'^DEVICE_ACCEPT_EXIT=(\d+)$',log,re.M)==['0'], 'Actual device exit missing')
                require(re.findall(r'^COMPLETED_TURNS=(\d+)$',log,re.M)==[str(manifest['turns'])], 'Zero/missing execution count')
            rows=audit_pair(d/left/'pulled',d/right/'pulled',manifest['turns'])
            if name=='candidate-cold-vs-reuse':
                require(all(r['query_lengths'][0]==r['full_length'] for r in rows),'Cold did not prefill full input')
                require(any(r['query_lengths'][1]<r['full_length'] for r in rows),'No actual retained KV prefix: numeric agreement alone is insufficient')
            pairs.append({'name':name,'turns':rows})
        save(d/'audit.json',{'pass':True,'pairs':pairs,'scope':'Complete finite logits and natural output, not performance or all gates'});print(json.dumps(pairs));return
    require(a.candidate_lib and a.baseline_lib and a.scenario,'Libraries/scenario required')
    spec=read(a.scenario);require(isinstance(spec,list) and len(spec)>0,'Nonzero scenario turns required')
    require(all(isinstance(t.get('question'),str) and t['question'].strip() and '\x00' not in t['question'] and type(t.get('budget')) is int and 1<=t['budget']<=24 for t in spec),'Invalid scenario')
    d.mkdir(parents=True,exist_ok=False);shutil.copyfile(__file__,d/'executed.py');save(d/'scenario.json',spec)
    helper=Path(__file__).with_name('prepare_pr_cache_stripped_tests.py');shutil.copyfile(helper,d/'strip-helper-executed.py')
    build=ALIAS/'build'/('query-production-accept-'+d.name);build.mkdir(exist_ok=False)
    source=build/'worker.cpp';source.write_text(WORKER,encoding='utf-8');shutil.copyfile(source,d/'worker.cpp')
    integration=read(ROOT/'build/framework-integration-20261006/build.json')
    require(integration['source_commit']=='d407447ed56c4121a11ccbd266dc184ca1ead0c2','Fixed source differs')
    flags=list(next(r['arguments'] for r in integration['drivers'] if r['kind']=='demo'))
    flags+=['-I'+str(ALIAS/'build/mnn-android-source/transformers/llm/engine/src')]
    libs={'candidate':a.candidate_lib.resolve(),'baseline':a.baseline_lib.resolve()};identities={k:sha(p) for k,p in libs.items()};binaries={}
    commands=[]
    for name,lib in libs.items():
        binary=build/name;cmd=[integration['compiler'],*flags,'-fPIE','-pie','-static-libstdc++',str(source),'-Wl,--whole-archive',str(lib),'-Wl,--no-whole-archive','-pthread','-llog','-landroid','-lm','-latomic','-o',str(binary)]
        r=subprocess.run(cmd,capture_output=True);(d/(name+'-compile-stdout.log')).write_bytes(r.stdout);(d/(name+'-compile-stderr.log')).write_bytes(r.stderr);commands.append({'command':cmd,'exit':r.returncode});save(d/'build.json',{'commands':commands,'libraries':identities,'device_tests_executed':0})
        require(r.returncode==0,'Worker compilation failed; original records retained')
        stripped=build/(name+'-stripped');shutil.copyfile(binary,stripped)
        cmd=[str(Path(integration['compiler']).parent/'llvm-strip.exe'),'--strip-all',str(stripped)]
        r=subprocess.run(cmd,capture_output=True);(d/(name+'-strip.log')).write_bytes(r.stdout+r.stderr);require(r.returncode==0,'Strip failed')
        elf=verify_derived(binary,stripped)
        save(d/(name+'-deployment.json'),{'original_path':str(binary),'original_sha256':sha(binary),'deployment_path':str(stripped),'deployment_sha256':sha(stripped),'elf_allocated_sections_exact':True,'elf_identity':elf,'command':cmd,'exit':r.returncode})
        binaries[name]=stripped
    save(d/'build.json',{'commands':commands,'libraries':identities,'binaries':{k:sha(v) for k,v in binaries.items()},'device_tests_executed':0,'library_hooks_not_injected_by_this_script':True,'public_sample_override':True,'worker_sha256':sha(source)})
    if not a.execute:print('OFFLINE_BUILD_ONLY; zero device executions');return
    adb=Adb(a.serial);device=adb.select();require(device['hardware_serial']=='1e7c759e','Wrong device');save(d/'device.json',device)
    save(d/'model-before.json',model_identity(adb));remote='/data/local/tmp/MNN/query-production-accept/'+d.name
    adb.shell('mkdir -p '+remote)
    for name,binary in binaries.items():adb.push_verified(binary,remote+'/'+name);adb.shell('chmod 755 '+remote+'/'+name)
    adb.push_verified(d/'scenario.json',remote+'/scenario.json')
    config={'base_dir':MODEL+'/','tokenizer_file':'tokenizer.txt','backend_type':'cpu','thread_num':4,'precision':a.precision,'memory':'low','sampler_type':'greedy','use_mmap':True,'use_cached_mmap':True,'kvcache_mmap':False,'async':False,'chunk':a.chunk,'attention_mode':8,'reuse_kv':True,'prompt_cache':True}
    save(d/'config.json',config);adb.push_verified(d/'config.json',remote+'/config.json')
    stages=[('candidate-reuse','candidate','reuse'),('candidate-cold','candidate','cold')] if a.precision=='low' else [('candidate-reuse','candidate','reuse'),('baseline-reuse','baseline','reuse')]
    pairs=[('candidate-cold-vs-reuse','candidate-cold','candidate-reuse')]
    if a.precision=='high':pairs=[('FP32-fallback-candidate-vs-baseline','candidate-reuse','baseline-reuse')]
    for stage,binary,mode in stages:
        sd=d/stage;sd.mkdir();rd=remote+'/'+stage;adb.shell('mkdir -p '+rd+' '+' '.join(rd+'/turn-'+str(i) for i in range(len(spec))))
        raw=adb.shell(remote+'/'+binary+' '+remote+'/config.json '+remote+'/scenario.json '+mode+' '+rd+'; code=$?; echo DEVICE_ACCEPT_EXIT=$code',timeout=1200)
        (sd/'stdout.log').write_bytes(raw);require(re.findall(rb'(?m)^DEVICE_ACCEPT_EXIT=(\d+)\r?$',raw)==[b'0'],'Device execution failed; raw retained')
        pulled=build/('pulled-'+stage)
        adb.call('pull',rd,str(pulled),timeout=240);shutil.copytree(pulled,sd/'pulled')
    save(d/'model-after.json',model_identity(adb));require(read(d/'model-before.json')==read(d/'model-after.json'),'Model identity changed')
    require(all(sha(libs[k])==v for k,v in identities.items()),'Input library changed')
    save(d/'run.json',{'fresh_phone_execution':True,'turns':len(spec),'pairs':pairs,'precision':a.precision,'device':device,'adb_commands':adb.commands,'libraries':identities,'binaries':{k:sha(v) for k,v in binaries.items()},'worker_sha256':sha(source),'config_sha256':sha(d/'config.json'),'scenario_sha256':sha(d/'scenario.json'),'stages':stages})
    print('PHONE_EXECUTED; run --audit-only to independently re-read raw records')


if __name__=='__main__':main()
