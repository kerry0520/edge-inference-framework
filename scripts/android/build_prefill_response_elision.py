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
 s='#include "prefill_response_elision.hpp"\n#include <cmath>\n'+s
 if rel.endswith('StaticModule.cpp'):
  a='''                if (exe == nullptr) {
                    exe.reset(backend->onCreate({}, {}, op));'''
  b='''                if (exe == nullptr && ResponseElision::authorized(config.base, backend->type()) &&
                    op->type() == OpType_Attention && op->main_type() == OpParameter_AttentionParam &&
                    op->main_as_AttentionParam()->kv_shared_layer_index() < 0) {
                    bool bridged = cloneBaseExecution(exe, base_executions, op, backend, backupBackend);
                    ResponseElision::attempt(op->name() ? op->name()->c_str() : nullptr, bridged);
                }
'''
  return replace(s,a,b+a)
 if rel.endswith('CPUAttention.cpp'):
  s=replace(s,'    auto tmp = new CPUAttention(bn, mKVCache);','''    if (!ResponseElision::cloneAllowed(mMeta, bn->getMetaPtr(), backend()->type(), bn->type())) return false;
    auto tmp = new CPUAttention(bn, mKVCache);''')
  a='''    *dst = tmp;
    return true;
}

CPUAttention::CPUAttention'''
  b='''    auto traceParam = op->main_as_AttentionParam();
    ResponseElision::clone(op->name() ? op->name()->c_str() : nullptr, this, tmp,
        mMeta, tmp->mMeta, mKVCacheManager.get(), tmp->mKVCacheManager.get(),
        mKVCacheManager->kvLength(), tmp->mKVCacheManager->kvLength(),
        traceParam ? traceParam->kv_shared_layer_index() : -2);
'''
  s=replace(s,a,b+a)
  start=s.index('ErrorCode CPUAttention::onExecute(');end=s.index('bool CPUAttention::onClone(',start);seg=s[start:end]
  trace=lambda boundary,error:'    ResponseElision::execute("'+boundary+'", this, mMeta, mKVCacheManager.get(), mKVCacheManager->kvLength(), mMeta ? mMeta->previous : 0, mMeta ? mMeta->add : 0, mMeta ? mMeta->remove : 0, mMeta ? mMeta->n_reserve : 0, mMeta ? mMeta->file_flag : -1, '+error+');\n'
  seg=replace(seg,'const std::vector<Tensor*>& outputs) {\n','const std::vector<Tensor*>& outputs) {\n'+trace('before','-1'))
  seg=replace(seg,'            return OUT_OF_MEMORY;',trace('after','OUT_OF_MEMORY')+'            return OUT_OF_MEMORY;')
  seg=replace(seg,'    return NO_ERROR;',trace('after','NO_ERROR')+'    return NO_ERROR;')
  return s[:start]+seg+s[end:]
 s=replace(s,'bool Llm::load() {','bool Llm::load() {\n    ResponseElision::erase(this);')
 s=replace(s,'Llm::~Llm() {','Llm::~Llm() {\n    ResponseElision::erase(this);')
 s=replace(s,'    mContext->status = LlmStatus::RUNNING;  // Set status to RUNNING after successful load','''    ResponseElision::bind(this, mModule.get(), mRuntimeManager.get(), mMeta.get(), model_path, weight_path);
    mContext->status = LlmStatus::RUNNING;  // Set status to RUNNING after successful load''')
 # Preserve public full logits contract, observing completion before return.
 s=replace(s,'    std::vector<Express::VARP> outputs = selectModule->onForward(inputs);','''    ResponseElision::begin(inDecode ? "decode" : "full", seqLen, mContext->all_seq_len, mMeta->previous, mMeta->remove, mMeta.get());
    ResponseElision::positions(inputPos->readMap<int>(), inputPos->getInfo()->size);
    std::vector<Express::VARP> outputs = selectModule->onForward(inputs);''')
 a='''    mMeta->sync();
    return outputs;
}

VARP Llm::forward'''
 b='''    if (!ResponseElision::logits(outputs[0]->readMap<float>(), outputs[0]->getInfo()->size)) {
        mContext->status = LlmStatus::INTERNAL_ERROR;
        ResponseElision::end(false, mMeta->previous);
        return {};
    }
    mMeta->sync();
    ResponseElision::end(true, mMeta->previous);
    return outputs;
}

VARP Llm::forward'''
 s=replace(s,a,b)
 # Only the ordinary whole-request token-ID chunker knows final output demand.
 a='''        int total_size = (int)input_ids.size();
        int loop_size = UP_DIV(total_size, mBlockSize);'''
 b='''        std::shared_ptr<Module> requestTrunk;
        const bool requested = mConfig->config_.value("prefill_head_elision", false);
        const bool eligible = requested && mConfig->backend_type() == "cpu" && mBlockSize == 32 &&
            mConfig->hidden_size() == 1024 && mConfig->layer_nums() == 28 &&
            !mConfig->reuse_kv() && !mConfig->prompt_cache() && !mAsync &&
            !mConfig->config_.value("async", false) && !mInSpec && !mPrefixCacheMode &&
            mValidBlockSize.empty() && mBaseModule == nullptr && mContext->gen_seq_len == 0 &&
            !mConfig->all_logits() && mConfig->speculative_type().empty() &&
            !mConfig->has_talker() && !mConfig->has_deepstack() && !mConfig->has_ple() &&
            !mConfig->is_visual() && !mConfig->is_audio() && !mConfig->kvcache_mmap() &&
            !mConfig->config_.value("hidden_states", false) &&
            mMeta->file_flag == KVMeta::NoChange && mMeta->n_reserve == 0 &&
            mMeta->remove == mMeta->previous &&
            ResponseElision::bound(this, mModule.get(), mRuntimeManager.get(), mMeta.get(), mConfig->llm_model(), mConfig->llm_weight());
        if (eligible) {
            Module::Config mc; mc.shapeMutable = true; mc.rearrange = true; mc.base = mModule.get();
            mRuntimeManager->setHintPtr(Interpreter::KVCACHE_INFO, mMeta.get());
            ResponseElision::Scope scope(mModule.get(), mMeta.get());
            const auto savedMeta = *mMeta;
            struct ExternalFileReset {
                std::shared_ptr<Executor::RuntimeManager> runtime;
                ~ExternalFileReset() {runtime->setExternalFile("");}
            } externalReset{mRuntimeManager};
            // Match the fixed archive's no-exceptions ABI; catch only on exception-enabled builds.
#if defined(__EXCEPTIONS)
            try {
#endif
                mRuntimeManager->setExternalFile(mConfig->llm_weight());
                requestTrunk.reset(Module::load({"input_ids", "attention_mask", "position_ids", "logits_index"},
                    {"hidden_states"}, mConfig->llm_model().c_str(), mRuntimeManager, &mc));
#if defined(__EXCEPTIONS)
            } catch (...) {
                requestTrunk.reset();
            }
#endif
            const bool metaStable = mMeta->previous == savedMeta.previous && mMeta->add == savedMeta.add &&
                mMeta->remove == savedMeta.remove && mMeta->n_reserve == savedMeta.n_reserve &&
                mMeta->file_flag == savedMeta.file_flag && mMeta->layer_index == savedMeta.layer_index;
            if (!scope.passed() || !metaStable) {
                requestTrunk.reset();
                *mMeta = savedMeta;
            }
        }
        std::printf("{\\"event\\":\\"response_route\\",\\"requested\\":%s,\\"eligible\\":%s,\\"prepared\\":%s}\\n", requested?"true":"false",eligible?"true":"false",requestTrunk?"true":"false");
        int total_size = (int)input_ids.size();
        int loop_size = UP_DIV(total_size, mBlockSize);'''
 s=replace(s,a,b)
 a='''            generate(input_embeds, 0);
        }
        completePrefixWrite();'''
 b='''            // Explicit output demand: only the whole request's final chunk publishes logits.
            const bool kvOnly = requestTrunk && i + 1 < loop_size;
            if (kvOnly) {
                const int seq = input_embeds->getInfo()->dim[mSeqLenIndex];
                Timer chunkTimer;
                mGenerateParam->input_embeds = nullptr;
                mGenerateParam->outputs.clear();
                mGenerateParam->validLogitSize = 0;
                mGenerateParam->validLogitStart = 0;
                mMeta->add = seq;
                auto mask = gen_attention_mask(seq);
                auto position = gen_position_ids(seq);
                ResponseElision::begin("trunk", seq, mContext->all_seq_len, mMeta->previous, mMeta->remove, mMeta.get());
                ResponseElision::positions(position->readMap<int>(), position->getInfo()->size);
                auto hidden = requestTrunk->onForward({input_embeds, mask, position, logitsLastIdx});
                bool ok = hidden.size() == 1 && !(hidden[0] == nullptr) && hidden[0]->getInfo() &&
                    hidden[0]->getInfo()->size == seq * mConfig->hidden_size() &&
                    hidden[0]->getInfo()->type.code == halide_type_float && hidden[0]->getInfo()->type.bits == 32;
                const float* values = ok ? hidden[0]->readMap<float>() : nullptr;
                ok = ok && values != nullptr;
                if (ok) {
                    const_cast<Tensor*>(hidden[0]->getTensor())->wait(Tensor::MAP_TENSOR_READ, true);
                    for (int j=0; j<hidden[0]->getInfo()->size; ++j) if (!std::isfinite(values[j])) {ok=false;break;}
                }
                if (!ok) {
                    mContext->status = LlmStatus::INTERNAL_ERROR;
                    ResponseElision::end(false, mMeta->previous);
                    return {}; // Some Attention layers may have mutated KV: never replay full.
                }
                mMeta->sync();
                ResponseElision::end(true, mMeta->previous);
                updateContext(seq, 0);
                mContext->prompt_len = seq;
                mContext->prefill_us += chunkTimer.durationInUs();
                ExecutorScope::Current()->gc();
                if (mGenerateParam->timeout_ms > 0 && mContext->prefill_us / 1000 >= mGenerateParam->timeout_ms) {
                    mContext->status = LlmStatus::TIMEOUT;
                    return mContext->output_tokens;
                }
            } else {
                generate(input_embeds, 0);
            }
            if (mContext->status != LlmStatus::RUNNING) return mContext->output_tokens;
        }
        // No persistent sidecar; release trunk while full module/backend/runtime still live.
        requestTrunk.reset();
        completePrefixWrite();'''
 return replace(s,a,b)
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--record-tag',default='build-v1');args=ap.parse_args()
 if not re.fullmatch(r'[A-Za-z0-9_-]+',args.record_tag):raise ValueError('bad tag')
 record=ROOT/'results/android/prefill-response-elision-20261014'/args.record_tag
 build=ALIAS/'build/prefill-response-elision-20261014'/args.record_tag
 record.mkdir(parents=True,exist_ok=False);build.mkdir(parents=True,exist_ok=False);source=record/'source';source.mkdir()
 shutil.copyfile(__file__,record/'executed-build-script.py')
 if sha(GUARD)!='5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf' or sha(LLM)!='f9ba43aa7ed1ae183fbf31b40ca14570ed55493cbd4a99e88946863148e30347':raise ValueError('guard identity changed')
 fixed=[GUARD,LLM,PUBLIC/'transformers/llm/engine/include/llm/llm.hpp',*[PUBLIC/p for p in FILES[1:]]];before={str(p):sha(p) for p in fixed};save(record/'fixed-before.json',before)
 orig=json.loads((ROOT/'results/android/chat-session-20261007/build.json').read_text(encoding='utf-8-sig'));orig['arguments']=[x.replace('mnn-android-project-32065-20261006','mnn-w4-contract-project-20261013') for x in orig['arguments']]
 compiler=orig['arguments'][0];ar=Path(compiler).parent/'llvm-ar.exe';ninja=(ROOT/'build/mnn-3.6.1-android-arm64-cpu-clean-release/build.ninja').read_text();exits=[]
 for name in ['prefill_response_elision.hpp','prefill_response_worker.cpp']:
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
 archive=build/'libMNN-prefill-response.a';shutil.copyfile(GUARD,archive);run('archive-replace',[str(ar),'r',str(archive),*map(str,objects)])
 base=archive_members(GUARD);current=archive_members(archive);changed=[a['name'] for a,b in zip(base,current) if a!=b]
 if len(base)!=len(current) or set(changed)!={p.name for p in objects} or any(a['name']!=b['name'] for a,b in zip(base,current)):raise ValueError('archive audit')
 save(record/'archive-audit.json',{'before':base,'after':current,'changed':changed,'untouched_count':len(base)-3,'untouched_byte_hash_equal':True})
 cmd=list(orig['arguments']);idx=next(i for i,x in enumerate(cmd) if x.endswith('phone_chat_worker.cpp'));cmd[idx]=str(build/'prefill_response_worker.cpp');cmd[cmd.index('-Wl,--whole-archive')+1]=str(archive);binary=build/'prefill_response_worker';cmd[cmd.index('-o')+1]=str(binary);cmd+=['-I'+str(build),'-I'+str(ALIAS/'build/mnn-android-source/source')];run('link-worker',cmd)
 after={str(p):sha(p) for p in fixed};save(record/'fixed-after.json',after)
 if before!=after:raise ValueError('original changed')
 save(record/'build.json',{'binary_path':str(binary),'binary_sha256':sha(binary),'candidate_library_path':str(archive),'candidate_library_sha256':sha(archive),'source_snapshots':snapshots,'worker_sha256':sha(build/'prefill_response_worker.cpp'),'helper_sha256':sha(build/'prefill_response_elision.hpp'),'class_layout_changed':False,'model_forward_count':0,'deployment_performed':False,'actual_exits':exits,'original_assets_unchanged':True})
 print(json.dumps({'built':True,'manifest':str(record/'build.json'),'model_forward_count':0}))
if __name__=='__main__':main()
