"""Independent sampler-boundary diagnostic; never replaces accepted libraries."""
import argparse, hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path
from phone_infer import Adb, model_identity, ROOT
A=Path("C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006")
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,v): Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("--serial");ap.add_argument("--record-dir",type=Path,required=True);args=ap.parse_args()
 d=args.record_dir.resolve();d.mkdir(parents=True,exist_ok=False)
 adb=Adb(args.serial);device=adb.select();assert device["hardware_serial"]=="1e7c759e" and device["model"]=="M2011K2C",device
 shutil.copyfile(__file__,d/"diagnose_kv_logits-executed.py");save(d/"device.json",device);print("PHONE_ONLINE "+json.dumps(device),flush=True)
 before=model_identity(adb);save(d/"model-before.json",before)
 b=A/"build"/("kv-logits-"+d.name);b.mkdir(exist_ok=False)
 original=ROOT/"results/android/kv-prefix-fix-20261007/llm.cpp";s=original.read_text();s=s.replace("#include <fstream>","#include <fstream>\n#include <cstdlib>\n#include <cstdio>",1)
 needle="    auto token_id = mSampler->sample(logits);\n    return token_id;"
 hook=r'''    auto token_id = mSampler->sample(logits);
    const char* diag = std::getenv("MNN_DIAG_DIR");
    static unsigned diagnosticIndex = 0;
    if (diag && *diag && diagnosticIndex < 4) {
        auto info = logits->getInfo();
        const int vocab = 151936;
        if (!info || info->size < vocab || info->size % vocab) std::abort();
        const float* values = logits->readMap<float>() + info->size - vocab;
        std::string base = std::string(diag) + "/sample-" + std::to_string(diagnosticIndex);
        FILE* f = std::fopen((base + ".f32").c_str(), "wb");
        if (!f || std::fwrite(values, sizeof(float), vocab, f) != vocab || std::fclose(f)) std::abort();
        int returned = token_id;
        const char* force = std::getenv("MNN_DIAG_FORCE");
        // Shared explicit sequence, never the two branches' independent argmax.
        const int common[3] = {30709, 99463, 1773};
        bool forced = force && std::string(force) == "1" && diagnosticIndex < 3;
        if (forced) returned = common[diagnosticIndex];
        std::ofstream meta(base + ".json");
        meta << "{\"sample_index\":" << diagnosticIndex << ",\"vocab_size\":" << vocab
             << ",\"tensor_size\":" << info->size << ",\"shape\":[";
        for (int i=0;i<info->dim.size();++i) { if(i) meta << ",";meta<<info->dim[i]; }
        meta << "],\"all_seq_len\":" << mContext->all_seq_len << ",\"prompt_len\":" << mContext->prompt_len
             << ",\"gen_seq_len\":" << mContext->gen_seq_len << ",\"offset\":" << offset << ",\"size_argument\":" << size
             << ",\"natural_token\":" << token_id << ",\"returned_token\":" << returned << ",\"forced\":" << (forced?"true":"false") << "}";
        ++diagnosticIndex;
        return returned;
    }
    return token_id;'''
 assert s.count(needle)==1;s=s.replace(needle,hook);src=b/"llm.cpp";src.write_text(s);shutil.copyfile(src,d/"llm.cpp")
 core=json.loads((ROOT/"results/android/kv-prefix-fix-20261007/core-build.json").read_text());flags=core["flags"]
 flags=[x.replace("kv-prefix-fix-source-20261007","mnn-android-source") for x in flags]
 compiler=core["compiler"];ar=Path(compiler).parent/"llvm-ar.exe";obj=b/"llm.cpp.o";base=A/"build/kv-prefix-fix-v2-20261007/libMNN-candidate.a";identity=sha(base);lib=b/"libMNN-diag.a"
 def run(cmd,name):
  x=subprocess.run(cmd,capture_output=True);(d/(name+"-stdout.log")).write_bytes(x.stdout);(d/(name+"-stderr.log")).write_bytes(x.stderr);assert x.returncode==0,x.stderr.decode(errors="replace");return cmd
 compilecmd=run([compiler,*flags,"-c",str(src),"-o",str(obj)],"core-build");shutil.copyfile(base,lib);run([str(ar),"r",str(lib),str(obj)],"archive")
 worker=b/"diagnostic.cpp"
 worker.write_text(r'''#include "llm/llm.hpp"
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <sstream>
using namespace MNN::Transformer;
int main(int argc,char**argv){
 if(argc!=5)return 2;
 std::unique_ptr<Llm> llm(Llm::createLLM(argv[1]));if(!llm||!llm->load())return 3;
 llm->set_config(R"({"reuse_kv":true,"prompt_cache":true,"async":false,"jinja":{"context":{"enable_thinking":false}}})");
 ChatMessages msgs={{"user","请记住我的名字是小林，只回复：记住了。"}};
 std::ostringstream a;llm->response(msgs,&a,"",64);
 std::printf("FIRST_TEXT=%s\n",a.str().c_str());
 auto ctx=llm->getContext();std::printf("FIRST_TOKENS=");for(int t:ctx->output_tokens)std::printf("%d,",t);std::printf("\n");
 msgs.emplace_back("assistant",a.str());msgs.emplace_back("user","我的名字是什么？只回答名字。");
 auto full=llm->tokenizer_encode(llm->apply_chat_template(msgs));std::printf("FULL_TOKENS=");for(int t:full)std::printf("%d,",t);std::printf("\n");
 if(std::string(argv[2])=="cold"){llm.reset(Llm::createLLM(argv[1]));if(!llm||!llm->load())return 3;llm->set_config(R"({"reuse_kv":false,"prompt_cache":false,"async":false,"jinja":{"context":{"enable_thinking":false}}})");auto coldFull=llm->tokenizer_encode(llm->apply_chat_template(msgs));std::printf("COLD_FULL_TOKENS=");for(int t:coldFull)std::printf("%d,",t);std::printf("\n");if(coldFull!=full)return 9;}
 setenv("MNN_DIAG_DIR",argv[3],1);setenv("MNN_DIAG_FORCE",argv[4],1);
 std::ostringstream out;llm->response(msgs,&out,"",4);
 ctx=llm->getContext();std::printf("SECOND_TEXT=%s\nSECOND_TOKENS=",out.str().c_str());for(int t:ctx->output_tokens)std::printf("%d,",t);
 std::printf("\nCONTEXT=%d,%d,%d,%d\n",ctx->prompt_len,ctx->all_seq_len,ctx->gen_seq_len,int(ctx->status));return ctx->status==LlmStatus::INTERNAL_ERROR?4:0;
}''');shutil.copyfile(worker,d/"diagnostic.cpp")
 cfg=json.loads((ROOT/"build/framework-integration-20261006/build.json").read_text(encoding="utf-8-sig"));wf=list(next(r["arguments"] for r in cfg["drivers"] if r["kind"]=="demo"));binary=b/"diagnostic"
 wc=run([compiler,*wf,"-fPIE","-pie","-static-libstdc++",str(worker),"-Wl,--whole-archive",str(lib),"-Wl,--no-whole-archive","-pthread","-llog","-landroid","-lm","-latomic","-o",str(binary)],"worker-build")
 assert sha(base)==identity
 build={"source_commit":cfg["source_commit"],"base_library_sha256":identity,"diagnostic_library_sha256":sha(lib),"source_sha256":sha(src),"binary_sha256":sha(binary),"compile":compilecmd,"link":wc,"accepted_library_unchanged":True};save(d/"build.json",build)
 remote="/data/local/tmp/MNN/kv-logits-diagnosis-20261007/"+d.name;adb.shell("mkdir -p "+remote);adb.push_verified(binary,remote+"/diagnostic");adb.shell("chmod 755 "+remote+"/diagnostic")
 stages=[]
 for precision in ("low","high"):
  for force in (0,1):
   for mode in ("candidate","cold"):
    name=precision+"-"+("force" if force else "natural")+"-"+mode;stage=d/name;stage.mkdir(exist_ok=False);rd=remote+"/"+name;adb.shell("mkdir -p "+rd)
    conf={"base_dir":"/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/","tokenizer_file":"tokenizer.txt","backend_type":"cpu","thread_num":4,"precision":precision,"memory":"low","sampler_type":"greedy","use_mmap":True,"use_cached_mmap":True,"kvcache_mmap":False,"reuse_kv":True,"prompt_cache":True,"async":False,"chunk":0}
    save(stage/"config.json",conf);adb.push_verified(stage/"config.json",rd+"/config.json")
    raw=adb.shell("cd "+rd+"; "+remote+"/diagnostic config.json "+mode+" "+rd+" "+str(force)+"; code=$?; echo DEVICE_DIAG_EXIT=$code",timeout=900);(stage/"stdout.log").write_bytes(raw);assert b"DEVICE_DIAG_EXIT=0" in raw,raw
    adb.call("pull",rd,str(A/stage.relative_to(ROOT)/"pulled"),timeout=60)
    files=[]
    for p in (stage/"pulled").glob("sample-*"):
     if p.suffix==".f32":assert p.stat().st_size==151936*4
     files.append({"path":str(p.relative_to(d)),"sha256":sha(p),"bytes":p.stat().st_size})
    stages.append({"name":name,"precision":precision,"mode":mode,"forced":bool(force),"files":files});save(d/"progress.json",stages);print(name+" DONE",flush=True)
 after=model_identity(adb);save(d/"model-after.json",after);assert before==after
 save(d/"summary.json",{"device":device,"stages":stages,"model_unchanged":before==after,"build":build,"dump_dtype":"little-endian float32","vocab_size":151936,"teacher_force_tokens":[30709,99463,1773],"dump_boundary":"same VARP supplied to sampler, last vocabulary row; natural sampler called before optional override","fresh_phone_execution":True,"adb_commands":adb.commands})
if __name__=="__main__":main()
