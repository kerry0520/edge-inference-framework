"""Bounded public callback diagnostic; no production archive replacement."""
import argparse,json,hashlib,shutil,subprocess,sys
from pathlib import Path
from phone_infer import Adb,ROOT,model_identity
A=Path("C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006")
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--serial');ap.add_argument('--metadata-only',action='store_true');ap.add_argument('--max-dump-index',type=int,default=50);ap.add_argument('--warm-prefill',action='store_true');ap.add_argument('--precision',choices=['low','high','both'],default='both');ap.add_argument('--record-dir',required=True,type=Path);a=ap.parse_args();assert 0<=a.max_dump_index<=99;d=a.record_dir.resolve();d.mkdir(parents=True,exist_ok=False)
 shutil.copyfile(__file__,d/'diagnose_kv_layers-executed.py');adb=Adb(a.serial);dev=adb.select();assert dev['hardware_serial']=='1e7c759e';save(d/'device.json',dev);before=model_identity(adb);save(d/'model-before.json',before)
 prev=ROOT/'results/android/kv-logits-diagnosis-20261007/run-2';cfg=json.loads((prev/'build.json').read_text());b=A/'build'/('kv-layers-'+d.name);b.mkdir(exist_ok=False)
 s=(prev/'llm.cpp').read_text();needle='    auto token_id = mSampler->sample(logits);';assert s.count(needle)==1;s=s.replace(needle,needle+'\n    if (std::getenv("MNN_LAYER_ACTIVE")) setenv("MNN_LAYER_ACTIVE", "0", 1);');src=b/'llm.cpp';src.write_text(s);shutil.copyfile(src,d/'llm.cpp')
 def run(cmd,name):
  r=subprocess.run(cmd,capture_output=True);(d/(name+'-stdout.log')).write_bytes(r.stdout);(d/(name+'-stderr.log')).write_bytes(r.stderr);assert r.returncode==0,r.stderr.decode(errors='replace');return cmd
 cc=list(cfg['compile']);cc[-3]=str(src);cc[-1]=str(b/'llm.cpp.o');run(cc,'compile');base=A/'build/kv-logits-run-2/libMNN-diag.a';oldsha=sha(base);lib=b/'libMNN-layer.a';shutil.copyfile(base,lib);run([str(Path(cc[0]).parent/'llvm-ar.exe'),'r',str(lib),str(b/'llm.cpp.o')],'archive')
 worker=(prev/'diagnostic.cpp').read_text().replace('#include <sstream>','#include <sstream>\n#include <fstream>\n#include <MNN/Tensor.hpp>')
 worker=worker.replace(' std::ostringstream a;llm->response(msgs,&a,"",64);',' auto warmInput=llm->tokenizer_encode(llm->apply_chat_template(msgs));std::printf("FIRST_INPUT_TOKENS=");for(int t:warmInput)std::printf("%d,",t);std::printf("\\n");\n std::ostringstream a;llm->response(msgs,&a,"",64);')
 point=' setenv("MNN_DIAG_DIR",argv[3],1);'
 hook=r''' std::ofstream ops(std::string(argv[3])+"/ops.jsonl"); unsigned idx=0;
 llm->setDebugCallback([](const std::vector<MNN::Tensor*>&, const MNN::OperatorInfo*){return true;},
 [&](const std::vector<MNN::Tensor*>& ts,const MNN::OperatorInfo* op){
 const char* active=getenv("MNN_LAYER_ACTIVE"); if(!active||std::string(active)!="1"||idx>=100)return true;
 unsigned i=idx++;ops<<"{\\"index\\":"<<i<<",\\"name\\":\\""<<op->name()<<"\\",\\"type\\":\\""<<op->type()<<"\\",\\"outputs\\":[";
 for(size_t j=0;j<ts.size();++j){if(j)ops<<",";auto t=ts[j];auto ty=t->getType();ops<<"{\\"shape\\":[";for(int k=0;k<t->dimensions();++k){if(k)ops<<",";ops<<t->length(k);}ops<<"],\\"format\\":"<<int(t->getDimensionType())<<",\\"code\\":"<<int(ty.code)<<",\\"bits\\":"<<int(ty.bits)<<",\\"lanes\\":"<<int(ty.lanes)<<",\\"bytes\\":"<<t->size();
 if(i<=50 && !std::getenv("MNN_LAYER_METADATA_ONLY") && t->elementSize()>0 && t->elementSize()<250000){MNN::Tensor host(t,t->getDimensionType()==MNN::Tensor::TENSORFLOW?MNN::Tensor::TENSORFLOW:MNN::Tensor::CAFFE);bool copied=t->copyToHostTensor(&host);ops<<" ,\"host_shape\":[";for(int k=0;k<host.dimensions();++k){if(k)ops<<",";ops<<host.length(k);}ops<<"]";ops<<",\\"host_format\\":"<<int(host.getDimensionType())<<",\\"host_bits\\":"<<int(host.getType().bits)<<",\\"host_bytes\\":"<<host.size()<<",\\"copied\\":"<<(copied?"true":"false");if(copied){std::string name="op-"+std::to_string(i)+"-out-"+std::to_string(j)+".bin";std::ofstream f(std::string(argv[3])+"/"+name,std::ios::binary);f.write(reinterpret_cast<char*>(host.host<void>()),host.size());ops<<",\\"file\\":\\""<<name<<"\\"";}}
 ops<<"}";}ops<<"]}\n";ops.flush();return true;});
 setenv("MNN_LAYER_ACTIVE","1",1);
'''
 # C++ JSON string escaping uses one backslash, not two.
 hook=hook.replace('i<=50','i<='+str(a.max_dump_index));hook=hook.replace('\\\\"','\\"')
 assert worker.count(point)==1
 if a.warm_prefill:
  assert worker.count(' ChatMessages msgs=')==1;worker=worker.replace(' ChatMessages msgs=',hook+' ChatMessages msgs=')
 else:worker=worker.replace(point,hook+point)
 w=b/'diagnostic.cpp';w.write_text(worker);shutil.copyfile(w,d/'diagnostic.cpp')
 link=list(cfg['link']);wi=next(i for i,x in enumerate(link) if x.endswith('diagnostic.cpp'));link[wi]=str(w);li=next(i for i,x in enumerate(link) if x.endswith('libMNN-diag.a'));link[li]=str(lib);link[-1]=str(b/'diagnostic');run(link,'link');assert sha(base)==oldsha;save(d/'build.json',{'compile':cc,'link':link,'base_sha256':oldsha,'library_sha256':sha(lib),'binary_sha256':sha(b/'diagnostic'),'production_unchanged':True})
 remote='/data/local/tmp/MNN/kv-layer-diagnosis-20261007/'+d.name;adb.shell('mkdir -p '+remote);adb.push_verified(b/'diagnostic',remote+'/diagnostic');adb.shell('chmod 755 '+remote+'/diagnostic');stages=[]
 for precision in (('low','high') if a.precision=='both' else (a.precision,)):
  for mode in (('candidate',) if a.warm_prefill else ('candidate','cold')):
   name=precision+'-'+mode;stage=d/name;stage.mkdir();rd=remote+'/'+name;adb.shell('mkdir -p '+rd);conf=json.loads((prev/(precision+'-force-'+mode)/'config.json').read_text());conf['enable_debug']=True;save(stage/'config.json',conf);adb.push_verified(stage/'config.json',rd+'/config.json');raw=adb.shell('cd '+rd+'; '+('MNN_LAYER_METADATA_ONLY=1 ' if a.metadata_only else '')+remote+'/diagnostic config.json '+mode+' '+rd+' 1; code=$?; echo DEVICE_DIAG_EXIT=$code',timeout=900);(stage/'stdout.log').write_bytes(raw);assert b'DEVICE_DIAG_EXIT=0' in raw,raw;pull=b/('pulled-'+name);adb.call('pull',rd,str(pull),timeout=120);shutil.copytree(pull,stage/'pulled');stages.append(name);save(d/'progress.json',stages);print(name+' DONE',flush=True)
 after=model_identity(adb);save(d/'model-after.json',after);assert before==after;save(d/'summary.json',{'device':dev,'fresh_phone_execution':True,'stages':stages,'model_unchanged':True,'warm_prefill':a.warm_prefill,'metadata_only':a.metadata_only,'max_dump_index':a.max_dump_index,'scope':'first 100 callback ops; bounded explicit dump index limit; first response warm prefill only' if a.warm_prefill else 'first 100 callback ops; bounded explicit dump index limit; second response first prefill only','adb_commands':adb.commands})
if __name__=='__main__':main()
