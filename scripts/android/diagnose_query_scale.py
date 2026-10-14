"""Isolated per-(query,head) FP16 scale candidate. Never replaces production."""
import argparse, json, hashlib, shutil, subprocess
from pathlib import Path
from phone_infer import Adb, ROOT, model_identity
A=Path('C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006')
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,x): p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--serial',required=True);ap.add_argument('--record-dir',type=Path,required=True);ap.add_argument('--long-context',action='store_true');ap.add_argument('--baseline-control',action='store_true');ap.add_argument('--no-graph-callback',action='store_true');ap.add_argument('--alternate-question',action='store_true');ap.add_argument('--natural',action='store_true');ap.add_argument('--max-new-tokens',type=int,default=4);a=ap.parse_args()
 assert 1<=a.max_new_tokens<=16
 d=a.record_dir.resolve();d.mkdir(parents=True,exist_ok=False);shutil.copyfile(__file__,d/'producer-executed.py')
 prev=ROOT/'results/android/kv-layer-diagnosis-20261007/run-4';cfg=json.loads((prev/'build.json').read_text());b=A/'build'/('query-scale-'+d.name);b.mkdir(exist_ok=False)
 src0=A/'build/mnn-android-source/source/backend/cpu/CPUAttention.cpp';s=src0.read_text();shutil.copyfile(src0,d/'CPUAttention-original.cpp')
 def replace(old,new):
  nonlocal s
  assert s.count(old)==1,old;s=s.replace(old,new)
 replace('bool isLowerTriangular) {','bool isLowerTriangular, const float* rowScales = nullptr) {')
 replace('source[i] *= scaleVal;','source[i] *= rowScales ? rowScales[(i / pack) % seqLen] : scaleVal;')
 replace('val *= scaleVal;','val *= rowScales ? rowScales[j] : scaleVal;')
 replace('    if (mBytes == 2 && mKeyQuantMode != KVQuantMode::Int8) {',r'''    bool rowCandidate = false;
#if defined(__aarch64__)
    bool causalMask = mask == nullptr || (mask->shape().empty() && mBytes == 2 && mask->host<FLOAT16_T>()[0] < 1e-6);
    rowCandidate = mBytes == 2 && mKeyQuantMode == KVQuantMode::None && mValueQuantMode == KVQuantMode::None &&
                   mUseFlashAttention && sinks == nullptr && lP == 1 && mHeadDim % 8 == 0 && causalMask;
#endif
    if (!rowCandidate && mBytes == 2 && mKeyQuantMode != KVQuantMode::Int8) {''')
 replace('            // Get packed Q',r'''            std::vector<float> rowScoreScale;
            // Get packed Q''')
 old='''                gcore->MNNAttenPackAndScaleSingleHead((float*)qReordered, (float*)(queryPtr + h * mHeadDim * mBytes),
                                                      mHeadDim * mNumHead, &q_scale, units, seqLen, mHeadDim);'''
 replace(old,r'''                if (rowCandidate) {
                    rowScoreScale.resize(seqLen);
                    std::vector<float> rowQScale(seqLen), rowAbsMax(seqLen);
                    for (int q=0; q<seqLen; ++q) {
                        auto row = (FLOAT16_T*)(queryPtr + ((size_t)q*mNumHead+h)*mHeadDim*mBytes);
                        float absMax=0.f;
                        for (int dim=0;dim<mHeadDim;++dim) absMax=ALIMAX(absMax,fabsf((float)row[dim]));
                        MNN_ASSERT(std::isfinite(absMax));
                        float scale=absMax>1.f ? 1.f/absMax : 1.f;
                        rowQScale[q]=scale; rowAbsMax[q]=absMax; rowScoreScale[q]=mScale/scale;
                        size_t offset=(size_t)(q/eP)*mHeadDim*eP+q%eP;
                        gcore->MNNAttenPackAndScaleSingleHead((float*)(qReordered+offset*mBytes),
                            (float*)row,mHeadDim*mNumHead,&scale,units,1,mHeadDim);
                    }
                    const char* active=std::getenv("MNN_LAYER_ACTIVE");
                    if(active && std::string(active)=="1" && seqLen>1) {
                        static std::atomic<unsigned> probeIndex{0}; unsigned index=probeIndex.fetch_add(1);
                        std::string path=std::string(std::getenv("MNN_DIAG_DIR"))+"/scale-"+std::to_string(index)+"-h"+std::to_string(h)+".json";
                        std::ofstream f(path); f.precision(17);
                        f<<"{\"head\":"<<h<<",\"seq_len\":"<<seqLen<<",\"kv_len\":"<<kvSeqLen<<",\"base_scale\":"<<mScale<<",\"q_scale\":[";
                        for(int q=0;q<seqLen;++q){if(q)f<<",";f<<rowQScale[q];}f<<"],\"absmax\":[";
                        for(int q=0;q<seqLen;++q){if(q)f<<",";f<<rowAbsMax[q];}f<<"],\"score_scale\":[";
                        for(int q=0;q<seqLen;++q){if(q)f<<",";f<<rowScoreScale[q];}f<<"]}";
                    }
                } else {
                    gcore->MNNAttenPackAndScaleSingleHead((float*)qReordered, (float*)(queryPtr + h * mHeadDim * mBytes),
                                                      mHeadDim * mNumHead, &q_scale, units, seqLen, mHeadDim);
                }''')
 replace('                                               (mKeyQuantMode == KVQuantMode::Int8), isLowerTriangular);','                                               (mKeyQuantMode == KVQuantMode::Int8), isLowerTriangular, rowCandidate ? rowScoreScale.data() : nullptr);')
 s=s.replace('namespace MNN {','#include <atomic>\n#include <fstream>\n#include <cstdlib>\n#include <string>\nnamespace MNN {',1)
 src=b/'CPUAttention.cpp';src.write_text(s);shutil.copyfile(src,d/'CPUAttention.cpp')
 def run(cmd,name):
  r=subprocess.run(cmd,capture_output=True);(d/(name+'-stdout.log')).write_bytes(r.stdout);(d/(name+'-stderr.log')).write_bytes(r.stderr);assert r.returncode==0,r.stderr.decode(errors='replace')
 cc=list(cfg['compile']);cc[-3]=str(src);cc[-1]=str(b/'CPUAttention.cpp.o');cc.insert(-4,'-I'+str(src0.parent));run(cc,'compile')
 base=A/'build/kv-layers-run-4/libMNN-layer.a';production=A/'build/kv-chat-delivery-20261007/libMNN-candidate.a';baseSha=sha(base);prodSha=sha(production);lib=b/'libMNN-query.a';shutil.copyfile(base,lib);run([str(Path(cc[0]).parent/'llvm-ar.exe'),'r',str(lib),str(b/'CPUAttention.cpp.o')],'archive')
 w=b/'diagnostic.cpp';worker=(prev/'diagnostic.cpp').read_text(encoding='utf-8')
 if a.long_context:
  worker=worker.replace('请记住我的名字是小林，只回复：记住了。',('以下是无关背景。天气晴朗，街道安静，书桌上有一本书。'*6)+'请记住我的名字是小林，只回复：记住了。')
 if a.no_graph_callback:
  start=worker.index(' llm->setDebugCallback(');end=worker.index(' setenv("MNN_LAYER_ACTIVE"',start);worker=worker[:start]+worker[end:]
 if a.alternate_question:worker=worker.replace('我的名字是什么？只回答名字。','我刚才让你记住的名字是什么？只回答两个字。')
 worker=worker.replace('llm->response(msgs,&out,"",4);','llm->response(msgs,&out,"",'+str(a.max_new_tokens)+');')
 w.write_text(worker,encoding='utf-8');shutil.copyfile(w,d/'diagnostic.cpp');link=list(cfg['link']);wi=next(i for i,x in enumerate(link) if x.endswith('diagnostic.cpp'));link[wi]=str(w);li=next(i for i,x in enumerate(link) if x.endswith('libMNN-layer.a'));link[li]=str(lib);link[-1]=str(b/'diagnostic');run(link,'link')
 if a.baseline_control:
  baseLink=list(link);baseLink[li]=str(base);baseLink[-1]=str(b/'baseline-diagnostic');run(baseLink,'link-baseline')
 save(d/'build.json',{'compile':cc,'link':link,'base_sha256':baseSha,'production_sha256':prodSha,'library_sha256':sha(lib),'binary_sha256':sha(b/'diagnostic')})
 adb=Adb(a.serial);dev=adb.select();assert dev['hardware_serial']=='1e7c759e';save(d/'device.json',dev);before=model_identity(adb);save(d/'model-before.json',before)
 remote='/data/local/tmp/MNN/query-scale-20261008/'+d.name;adb.shell('mkdir -p '+remote);adb.push_verified(b/'diagnostic',remote+'/diagnostic');adb.shell('chmod 755 '+remote+'/diagnostic')
 variants=['query']+(['baseline'] if a.baseline_control else [])
 if a.baseline_control:
  adb.push_verified(b/'baseline-diagnostic',remote+'/baseline-diagnostic');adb.shell('chmod 755 '+remote+'/baseline-diagnostic')
 for variant in variants:
  for mode in ('candidate','cold'):
   name=('baseline-' if variant=='baseline' else '')+'low-'+mode;stage=d/name;stage.mkdir();rd=remote+'/'+name;adb.shell('mkdir -p '+rd);shutil.copyfile(prev/('low-'+mode)/'config.json',stage/'config.json');adb.push_verified(stage/'config.json',rd+'/config.json');binary='baseline-diagnostic' if variant=='baseline' else 'diagnostic';raw=adb.shell('cd '+rd+'; '+remote+'/'+binary+' config.json '+mode+' '+rd+(' 0' if a.natural else ' 1')+'; code=$?; echo DEVICE_DIAG_EXIT=$code',timeout=900);(stage/'stdout.log').write_bytes(raw);assert b'DEVICE_DIAG_EXIT=0' in raw,raw;pull=b/('pulled-'+name);adb.call('pull',rd,str(pull),timeout=120);shutil.copytree(pull,stage/'pulled');print(name+' DONE',flush=True)
 after=model_identity(adb);save(d/'model-after.json',after);assert before==after and sha(base)==baseSha and sha(production)==prodSha
 save(d/'summary.json',{'device':dev,'fresh_phone_execution':True,'model_unchanged':True,'production_unchanged':True,'long_context':a.long_context,'baseline_control':a.baseline_control,'no_graph_callback':a.no_graph_callback,'alternate_question':a.alternate_question,'natural':a.natural,'max_new_tokens':a.max_new_tokens,'scope':'all eligible Attention calls, per-(query,head) scale; default chunk0 second-turn bounded response','adb_commands':adb.commands})
if __name__=='__main__':main()
