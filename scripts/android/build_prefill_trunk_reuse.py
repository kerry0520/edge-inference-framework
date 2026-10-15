"""Isolated same-owner trunk retention; compile only after design approval."""
import argparse,hashlib,json,re,shlex,shutil,subprocess
from pathlib import Path
from build_runtime_dispatch import archive_members
from build_prefill_elision_cost import ROOT,ALIAS,PUBLIC,GUARD,LLM,FILES,sha,save,replace

def config_key():
 return '''static std::string reuseConfigKey(const ujson::json& config) {
    // ujson copies share a RapidJSON document; parse a serialization for ownership.
    auto stable = ujson::json::parse(config.dump());
    stable.erase("prefill_head_elision");
    stable.erase("prefill_reuse_lifecycle");
    return stable.dump();
}

'''
def patch(rel,s):
 frozen=ROOT/'results/android/prefill-elision-cost-20261014/build-v1/source'/Path(rel).name
 s=frozen.read_text(encoding='utf8').replace('prefill_elision_cost.hpp','prefill_trunk_reuse.hpp')
 v5=(ROOT/'results/android/prefill-response-elision-20261014/build-v5/source'/Path(rel).name).read_text(encoding='utf8')
 if rel.endswith('CPUAttention.cpp'):
  # The cost baseline removed only these observer lines from this exact v5 file.
  s=v5.replace('prefill_response_elision.hpp','prefill_trunk_reuse.hpp')
  s='\n'.join('#if PREFILL_REUSE_DIAGNOSTIC\n'+line+'\n#endif' if 'ResponseElision::execute(' in line else line for line in s.split('\n'))
  return s
 if rel.endswith('StaticModule.cpp'):return s
 s=replace(s,'bool Llm::set_config(const std::string& content) {',config_key()+'''bool Llm::set_config(const std::string& content) {
    const auto priorKey = reuseConfigKey(mConfig->config_);''')
 s=replace(s,'    setChatTemplate();\n    mAsync =', '''    ResponseElision::lifecycle() = mConfig->config_.value("prefill_reuse_lifecycle", false);
    if (priorKey != reuseConfigKey(mConfig->config_)) ResponseElision::erase(this);
    else if (!mConfig->config_.value("prefill_head_elision", false)) ResponseElision::evict(this, "explicit_off");
    setChatTemplate();
    mAsync =''')
 s=replace(s,'mMeta.get(), model_path, weight_path);','mMeta.get(), model_path, weight_path, reuseConfigKey(mConfig->config_));')
 s=replace(s,'std::vector<int> Llm::generate(const std::vector<int>& input_ids, int max_tokens) {','''std::vector<int> Llm::generate(const std::vector<int>& input_ids, int max_tokens) {
    ResponseElision::RequestGuard reuseGuard(this, [this](){return mContext->status == LlmStatus::NORMAL_FINISHED || mContext->status == LlmStatus::MAX_TOKENS_FINISHED;});''')
 s=replace(s,'mConfig->llm_model(), mConfig->llm_weight());','mConfig->llm_model(), mConfig->llm_weight(), reuseConfigKey(mConfig->config_));')
 s=replace(s,'        const auto prepareStart=ResponseElision::now();\n        if (eligible) {','''        if (!eligible) ResponseElision::evict(this, "ineligible_or_binding_changed");
        requestTrunk = eligible ? ResponseElision::cached(this) : nullptr;
        ResponseElision::metrics().cache_hit = bool(requestTrunk);
        const auto prepareStart=ResponseElision::now();
        if (eligible && !requestTrunk) {''')
 s=replace(s,'                *mMeta = savedMeta;\n            }\n        }','''                *mMeta = savedMeta;
            }
            if (requestTrunk) ResponseElision::install(this, requestTrunk, mModule, mRuntimeManager, mMeta, mExecutor);
        }''')
 s=replace(s,'// No persistent sidecar; release trunk while full module/backend/runtime still live.','// Drop request reference; owner retains the plan until invalidation or destruction.')
 # diagnostic forward probes match the last proven v5 observer locations.
 for line in v5.splitlines():
  if any(x in line for x in ['ResponseElision::begin(', 'ResponseElision::positions(', 'ResponseElision::end(']):
   # Restore through the immediately following non-observer line; disambiguate repeated sync below.
   pass
 s=replace(s,'    std::vector<Express::VARP> outputs = selectModule->onForward(inputs);','''#if PREFILL_REUSE_DIAGNOSTIC
    ResponseElision::begin(inDecode ? "decode" : "full", seqLen, mContext->all_seq_len, mMeta->previous, mMeta->remove, mMeta.get());
    ResponseElision::positions(inputPos->readMap<int>(), inputPos->getInfo()->size);
#endif
    std::vector<Express::VARP> outputs = selectModule->onForward(inputs);''')
 s=replace(s,'    mMeta->sync();\n    ++ResponseElision::metrics().full;', '''#if PREFILL_REUSE_DIAGNOSTIC
    if (!ResponseElision::logits(outputs[0]->readMap<float>(), outputs[0]->getInfo()->size)) {
        mContext->status = LlmStatus::INTERNAL_ERROR; ResponseElision::end(false, mMeta->previous); return {};
    }
#endif
    mMeta->sync();
#if PREFILL_REUSE_DIAGNOSTIC
    ResponseElision::end(true, mMeta->previous);
#endif
    ++ResponseElision::metrics().full;''')
 s=replace(s,'                auto hidden = requestTrunk->onForward(','''#if PREFILL_REUSE_DIAGNOSTIC
                ResponseElision::begin("trunk", seq, mContext->all_seq_len, mMeta->previous, mMeta->remove, mMeta.get());
                ResponseElision::positions(position->readMap<int>(), position->getInfo()->size);
#endif
                auto hidden = requestTrunk->onForward(''')
 s=replace(s,'                mMeta->sync();\n                ++ResponseElision::metrics().trunk;','''                mMeta->sync();
#if PREFILL_REUSE_DIAGNOSTIC
                ResponseElision::end(true, mMeta->previous);
#endif
                ++ResponseElision::metrics().trunk;''')
 return s

# Build orchestration is the frozen cost builder, with only stage/name/patch and diagnostic flag changes.
def main():
 import build_prefill_elision_cost as base
 source=Path(base.__file__).read_text(encoding='utf8')
 source=source[source.index('def main():'):source.index("if __name__")]
 source=source.replace("args=ap.parse_args()", "ap.add_argument('--diagnostic',action='store_true');args=ap.parse_args()")
 source=source.replace('prefill-elision-cost-20261014','prefill-trunk-reuse-20261014').replace('prefill_elision_cost','prefill_trunk_reuse').replace('prefill-elision-cost.a','prefill-trunk-reuse.a')
 source=source.replace("'-c',str(build/name)","'-DPREFILL_REUSE_DIAGNOSTIC='+str(int(args.diagnostic)),'-c',str(build/name)")
 source=source.replace("cmd+=['-I'+str(build)","cmd+=['-DPREFILL_REUSE_DIAGNOSTIC='+str(int(args.diagnostic)),'-I'+str(build)")
 source=source.replace("'class_layout_changed':False", "'diagnostic':args.diagnostic,'semantic_source_baseline':'cost-build-v1','class_layout_changed':False")
 ns=globals().copy();exec(source,ns);ns['main']()
if __name__=='__main__':main()
