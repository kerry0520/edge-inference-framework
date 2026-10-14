"""Bounded isolated CPUAttention observer; no production replacement."""
import argparse,json,hashlib,shutil,subprocess
from pathlib import Path
from phone_infer import Adb,ROOT,model_identity
A=Path("C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006")
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--serial');ap.add_argument('--shared-scale',type=float);ap.add_argument('--record-dir',required=True,type=Path);a=ap.parse_args();d=a.record_dir.resolve();d.mkdir(parents=True,exist_ok=False)
 shutil.copyfile(__file__,d/'diagnose_attention_internal-executed.py');adb=Adb(a.serial);dev=adb.select();assert dev['hardware_serial']=='1e7c759e';save(d/'device.json',dev);before=model_identity(adb);save(d/'model-before.json',before)
 prev=ROOT/'results/android/kv-layer-diagnosis-20261007/run-4';cfg=json.loads((prev/'build.json').read_text());b=A/'build'/('attention-internal-'+d.name);b.mkdir(exist_ok=False)
 src0=A/'build/mnn-android-source/source/backend/cpu/CPUAttention.cpp';s=src0.read_text();shutil.copyfile(src0,d/'CPUAttention-original.cpp')
 helpers=r'''
#include <fstream>
#include <string>
#include <cstdlib>
static void attRaw(const std::string& name,const void* p,size_t bytes) {
 std::ofstream f(std::string(getenv("MNN_DIAG_DIR"))+"/att-"+name+".bin",std::ios::binary); f.write((const char*)p,bytes);
}
static float attFloat(const void* p,size_t i,int bytes) {return bytes==2?(float)((const FLOAT16_T*)p)[i]:((const float*)p)[i];}
static void attContiguous(const std::string& name,const void* p,size_t count,int bytes) {
 std::vector<float> v(count);for(size_t i=0;i<count;++i)v[i]=attFloat(p,i,bytes);attRaw(name+"-f32",v.data(),v.size()*4);attRaw(name+"-raw",p,count*bytes);
}
static void attPacked(const std::string& name,const void* p,int rows,int cols,int pack,int bytes) {
 std::vector<float> v((size_t)rows*cols);for(int r=0;r<rows;++r)for(int c=0;c<cols;++c)v[r*cols+c]=attFloat(p,(c/pack)*rows*pack+r*pack+c%pack,bytes);attRaw(name+"-f32",v.data(),v.size()*4);
}
'''
 s=s.replace('namespace MNN {',helpers+'\nnamespace MNN {',1)
 point='    auto gcore = static_cast<CPUBackend*>(backend())->functions();'
 start=s.index('ErrorCode CPUAttention::onExecute');pos=s.index(point,start)
 select=r'''    static unsigned attCalls=0;
    bool attProbe=false;
    const char* act=getenv("MNN_LAYER_ACTIVE");
    if(act && std::string(act)=="1") { unsigned call=attCalls++;attProbe=(call==1); }
'''
 s=s[:pos]+select+s[pos:]
 point='    int insertLen = seqLen;'
 hook=r'''    float attNaturalQScale=q_scale,attNaturalMScale=mScale;
    if(attProbe && getenv("MNN_ATT_SHARED_SCALE")) {
      float shared=std::strtof(getenv("MNN_ATT_SHARED_SCALE"),nullptr);
      MNN_ASSERT(shared>0.0f && shared<=q_scale);
      float baseScale=(mMeta && mMeta->attn_scale>0)?mMeta->attn_scale:(1.0/sqrt(mHeadDim));
      mScale=baseScale/shared;q_scale=shared;
    }
'''
 assert s.count(point)==1;s=s.replace(point,hook+point)
 point='    // Temporary tensors for intermediate results'
 hook=r'''    if(attProbe) {
      std::ofstream f(std::string(getenv("MNN_DIAG_DIR"))+"/attention.json");
      f<<"{\"active_attention_call_index\":1,\"seq_len\":"<<seqLen<<",\"kv_len\":"<<kvSeqLen<<",\"kv_valid_offset\":"<<kvValidOffset<<",\"max_len\":"<<maxLen<<",\"heads\":"<<mNumHead<<",\"kv_heads\":"<<mKvNumHead<<",\"head_dim\":"<<mHeadDim<<",\"bytes\":"<<mBytes<<",\"pack\":"<<mPack<<",\"eP\":"<<eP<<",\"lP\":"<<lP<<",\"hP\":"<<hP<<",\"q_scale\":";f.precision(17);f<<q_scale<<" ,\"natural_q_scale\":"<<attNaturalQScale<<",\"natural_mScale\":"<<attNaturalMScale<<",\"mScale\":"<<mScale<<",\"flash\":"<<(mUseFlashAttention?"true":"false")<<",\"key_quant\":"<<int(mKeyQuantMode)<<",\"value_quant\":"<<int(mValueQuantMode)<<",\"attention_option\":"<<static_cast<CPUBackend*>(backend())->getRuntime()->hint().attentionOption<<",\"block_kv\":"<<mKVCacheManager->getFlashAttentionBlockKv()<<",\"previous\":"<<(mMeta?mMeta->previous:-1)<<",\"remove\":"<<(mMeta?mMeta->remove:-1)<<",\"add\":"<<(mMeta?mMeta->add:-1)<<",\"mask_count\":"<<(mask?mask->elementSize():0)<<",\"mask_shape\":[";
      if(mask)for(int j=0;j<mask->dimensions();++j){if(j)f<<",";f<<mask->length(j);}f<<"]}";
      attContiguous("q",queryPtr,(size_t)seqLen*mNumHead*mHeadDim,mBytes);
      attContiguous("k-input",key->host<void>(),(size_t)seqLen*mKvNumHead*mHeadDim,mBytes);
      attContiguous("v-input",value->host<void>(),(size_t)seqLen*mKvNumHead*mHeadDim,mBytes);
      if(mask)attContiguous("mask",mask->host<void>(),mask->elementSize(),mBytes);
      if(mKeyQuantMode==KVQuantMode::None && mValueQuantMode==KVQuantMode::None) {
       std::vector<float> k((size_t)kvSeqLen*mKvNumHead*mHeadDim),v(k.size());int bk=mKVCacheManager->getFlashAttentionBlockKv();
       for(int h=0;h<mKvNumHead;++h)for(int t=0;t<kvSeqLen;++t)for(int dim=0;dim<mHeadDim;++dim) {
        size_t ix=((size_t)t*mKvNumHead+h)*mHeadDim+dim;
        size_t ki=(t/hP)*ROUND_UP(mHeadDim,lP)*hP+(dim/lP)*hP*lP+(t%hP)*lP+dim%lP;
        size_t stride1=UP_DIV(bk,lP)*hP*lP,stride0=stride1*UP_DIV(mHeadDim,hP);
        size_t vi=(t/bk)*stride0+(dim/hP)*stride1+((t%bk)/lP)*hP*lP+(dim%hP)*lP+(t%bk)%lP;
        k[ix]=attFloat(mKVCacheManager->addrOfKey(h),ki,mBytes);v[ix]=attFloat(mKVCacheManager->addrOfValue(h),vi,mBytes);
       }
       attRaw("effective-k-f32",k.data(),k.size()*4);attRaw("effective-v-f32",v.data(),v.size()*4);
      }
    }
'''
 assert s.count(point)==1;s=s.replace(point,hook+'\n'+point)
 point='            // Start computing'
 hook=r'''            if(attProbe && mKeyQuantMode==KVQuantMode::None) {
              attRaw("h"+std::to_string(h)+"-packed-scaled-q-raw",qReordered,mPackQ->stride(0));
              std::vector<float> qp((size_t)seqLen*mHeadDim);
              for(int q=0;q<seqLen;++q)for(int dim=0;dim<mHeadDim;++dim){size_t ix=(q/eP)*UP_DIV(mHeadDim,lP)*eP*lP+(dim/lP)*eP*lP+(q%eP)*lP+dim%lP;qp[q*mHeadDim+dim]=attFloat(qReordered,ix,mBytes);}
              attRaw("h"+std::to_string(h)+"-scaled-q-f32",qp.data(),qp.size()*4);
            }
'''
 assert s.count(point)==1;s=s.replace(point,hook+point)
 point='                // 2. softmax scores, softmax src/dst shape: [kv_seq_len/mPack, seq_len, mPack]'
 hook='                if(attProbe)attPacked("h"+std::to_string(h)+"-b"+std::to_string(i)+"-qk-before",qkPacked,seqLen,subKvSeqLen,mPack,mBytes);\n'
 assert s.count(point)==1;s=s.replace(point,hook+point)
 point='                    gcore->MNNSoftmax(qkSoftmax,'
 hook='                    if(attProbe)attPacked("h"+std::to_string(h)+"-b"+std::to_string(i)+"-qk-masked",qkPacked,seqLen,subKvSeqLen,mPack,mBytes);\n'
 assert s.count(point)==1;s=s.replace(point,hook+point)
 point='                // 3. qk @ v'
 hook='                if(attProbe)attPacked("h"+std::to_string(h)+"-b"+std::to_string(i)+"-softmax",qkSoftmax,seqLen,subKvSeqLen,mPack,mBytes);\n'
 assert s.count(point)==1;s=s.replace(point,hook+point)
 point='                // 4. flash attention, update each sub kvSeq\'s final results'
 hook='                if(attProbe)attPacked("h"+std::to_string(h)+"-b"+std::to_string(i)+"-pv",qkvPacked,seqLen,mHeadDim,mPack,mBytes);\n'
 assert s.count(point)==1;s=s.replace(point,hook+point)
 point='                // 3. qk @ v'
 hook='                if(attProbe && runningMax && runningSum){attRaw("h"+std::to_string(h)+"-b"+std::to_string(i)+"-running-max-f32",runningMax,seqLen*4);attRaw("h"+std::to_string(h)+"-b"+std::to_string(i)+"-running-sum-f32",runningSum,seqLen*4);}'
 assert s.count(point)==1;s=s.replace(point,hook+point)
 point='            // Final results writing:'
 hook='            if(attProbe)attPacked("h"+std::to_string(h)+"-final-pv",outputPacked,seqLen,mHeadDim,mPack,mBytes);\n'
 assert s.count(point)==1;s=s.replace(point,hook+point)
 src=b/'CPUAttention.cpp';src.write_text(s);shutil.copyfile(src,d/'CPUAttention.cpp')
 def run(cmd,name):
  r=subprocess.run(cmd,capture_output=True);(d/(name+'-stdout.log')).write_bytes(r.stdout);(d/(name+'-stderr.log')).write_bytes(r.stderr);assert r.returncode==0,r.stderr.decode(errors='replace');return cmd
 cc=list(cfg['compile']);cc[-3]=str(src);cc[-1]=str(b/'CPUAttention.cpp.o');cc.insert(-4,'-I'+str(src0.parent));run(cc,'compile')
 base=A/'build/kv-layers-run-4/libMNN-layer.a';oldsha=sha(base);production=A/'build/kv-chat-delivery-20261007/libMNN-candidate.a';prodsha=sha(production);lib=b/'libMNN-attention.a';shutil.copyfile(base,lib);run([str(Path(cc[0]).parent/'llvm-ar.exe'),'r',str(lib),str(b/'CPUAttention.cpp.o')],'archive')
 w=b/'diagnostic.cpp';shutil.copyfile(prev/'diagnostic.cpp',w);shutil.copyfile(w,d/'diagnostic.cpp')
 link=list(cfg['link']);wi=next(i for i,x in enumerate(link) if x.endswith('diagnostic.cpp'));link[wi]=str(w);li=next(i for i,x in enumerate(link) if x.endswith('libMNN-layer.a'));link[li]=str(lib);link[-1]=str(b/'diagnostic');run(link,'link');assert sha(base)==oldsha and sha(production)==prodsha;save(d/'build.json',{'compile':cc,'link':link,'base_sha256':oldsha,'production_sha256':prodsha,'library_sha256':sha(lib),'binary_sha256':sha(b/'diagnostic'),'production_unchanged':True})
 remote='/data/local/tmp/MNN/attention-internal-20261008/'+d.name;adb.shell('mkdir -p '+remote);adb.push_verified(b/'diagnostic',remote+'/diagnostic');adb.shell('chmod 755 '+remote+'/diagnostic');stages=[]
 for mode in ('candidate','cold'):
  name='low-'+mode;stage=d/name;stage.mkdir();rd=remote+'/'+name;adb.shell('mkdir -p '+rd);shutil.copyfile(prev/name/'config.json',stage/'config.json');adb.push_verified(stage/'config.json',rd+'/config.json');raw=adb.shell('cd '+rd+'; '+(('MNN_ATT_SHARED_SCALE='+str(a.shared_scale)+' ') if a.shared_scale else '')+remote+'/diagnostic config.json '+mode+' '+rd+' 1; code=$?; echo DEVICE_DIAG_EXIT=$code',timeout=900);(stage/'stdout.log').write_bytes(raw);assert b'DEVICE_DIAG_EXIT=0' in raw,raw;pull=b/('pulled-'+name);adb.call('pull',rd,str(pull),timeout=120);shutil.copytree(pull,stage/'pulled');stages.append(name);save(d/'progress.json',stages);print(name+' DONE',flush=True)
 after=model_identity(adb);save(d/'model-after.json',after);assert before==after and sha(production)==prodsha;save(d/'summary.json',{'device':dev,'fresh_phone_execution':True,'stages':stages,'model_unchanged':True,'production_unchanged':True,'shared_scale':a.shared_scale,'scope':'Second active CPUAttention; first prefill of second response; low candidate and cold; observational hooks only','adb_commands':adb.commands})
if __name__=='__main__':main()
