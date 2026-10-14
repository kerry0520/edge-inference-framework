// Isolated prefill-boundary correctness probe. No production chat or performance claim.
#include <MNN/expr/ExprCreator.hpp>
#include <MNN/expr/ExecutorScope.hpp>
#include <MNN/expr/Module.hpp>
#include <MNN/Interpreter.hpp>
#include "llm/llm.hpp"
#include "llmconfig.hpp"
#include "core/KVMeta.hpp"
#include "backend/cpu/CPUKVCacheManager.hpp"
#include "prefill_trunk_forward_trace.hpp"
#include "rapidjson/document.h"
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <map>
#include <memory>
#include <string>
#include <vector>
using namespace MNN;
using namespace MNN::Express;
using namespace MNN::Transformer;
class BoundaryForwardLlm final : public Llm {
public:
    explicit BoundaryForwardLlm(std::shared_ptr<LlmConfig> c, bool candidate) : Llm(c), candidate_(candidate) {}
    ~BoundaryForwardLlm() override { trunk_.reset(); }
    bool prepare() {
        // Reject unsupported configuration rather than silently widening this proof.
        if (mConfig->backend_type()!="cpu" || mConfig->all_logits() || mConfig->reuse_kv() || mConfig->prompt_cache() ||
            !mConfig->speculative_type().empty() || mConfig->has_talker() || mConfig->has_deepstack() || mConfig->has_ple() ||
            mConfig->is_visual() || mConfig->is_audio() || mConfig->config_.value("kvcache_mmap",false) ||
            mConfig->config_.value("async",false) || mConfig->config_.value("chunk",0)!=32 ||
            mConfig->config_.value("hidden_states",false)) return false;
        // Keep load structure identical for baseline/candidate; only per-step routing differs.
        set_config(R"({"enable_debug":true,"async":false,"speculative_type":"","reuse_kv":false,"prompt_cache":false})");
        PrefillTrunkBridge::phase("full-load",false);
        if (!Llm::load()) return false;
        Module::Config mc; mc.shapeMutable=true; mc.rearrange=true; mc.base=mModule.get();
        mRuntimeManager->setHintPtr(Interpreter::KVCACHE_INFO,mMeta.get());
        mRuntimeManager->setExternalFile(mConfig->llm_weight());
        PrefillTrunkBridge::phase("trunk-load",true);
        trunk_.reset(Module::load({"input_ids","attention_mask","position_ids","logits_index"},{"hidden_states"},
                                 mConfig->llm_model().c_str(),mRuntimeManager,&mc));
        mRuntimeManager->setExternalFile("");
        PrefillTrunkBridge::phase("",false);
        if (PrefillTrunkBridge::report(mMeta.get(),true,bool(trunk_))!=0) return false;
        for (const auto& c:PrefillTrunkBridge::state().clones)
            if (c.phase=="trunk-load") managers_[c.name]=c.dstManager;
        if (managers_.size()!=28) return false;
        setDebugCallback([this](const std::vector<Tensor*>&,const OperatorInfo* i) {
            if(active_ && i) {
                if(i->type()=="Attention") ++attentionBefore_[i->name()];
                if(i->name()=="/lm/lm_head/Linear") ++headBefore_;
            }
            return true;
        },[this](const std::vector<Tensor*>&,const OperatorInfo* i) {
            if(active_ && i) {
                if(i->type()=="Attention") ++attentionAfter_[i->name()];
                if(i->name()=="/lm/lm_head/Linear") ++headAfter_;
            }
            return true;
        });
        mContext->all_seq_len=0; mContext->gen_seq_len=0; mContext->status=LlmStatus::RUNNING;
        return true;
    }
    bool step(const std::vector<int>& ids,int index,bool decode,bool lastPrefill,const std::string& prefix) {
        ExecutorScope scope(mExecutor);
        if(ids.empty() || (decode && ids.size()!=1)) return false;
        const int prior=mContext->all_seq_len;
        if(mMeta->previous!=static_cast<size_t>(prior) || mMeta->remove || mMeta->n_reserve || mMeta->add || mMeta->file_flag!=KVMeta::NoChange) return false;
        // Phase is explicit: a retained-KV suffix is prefill again, not decode.
        mContext->gen_seq_len=decode ? 1 : 0;
        mMeta->add=ids.size();
        auto embeds=embedding(ids); auto mask=gen_attention_mask(ids.size()); auto pos=gen_position_ids(ids.size());
        if(embeds==nullptr || mask==nullptr || pos==nullptr || !pos->getInfo() || !pos->readMap<int>()) return false;
        std::printf("{\"event\":\"positions\",\"step\":%d,\"values\":[",index);
        for(int i=0;i<pos->getInfo()->size;++i) {if(i)std::putchar(',');std::printf("%d",pos->readMap<int>()[i]);}
        std::printf("]}\n");
        std::printf("{\"event\":\"step_input\",\"step\":%d,\"meta\":%llu,\"input_ids\":[",index,(unsigned long long)reinterpret_cast<uintptr_t>(mMeta.get()));
        for(size_t i=0;i<ids.size();++i){if(i)std::putchar(',');std::printf("%d",ids[i]);}std::printf("]}\n");
        if(!lengths(index,"before",prior)) return false;
        headBefore_=headAfter_=0; attentionBefore_.clear();attentionAfter_.clear();
        const bool useTrunk=candidate_ && !decode && !lastPrefill;
        PrefillTrunkForward::begin(index,useTrunk?"trunk":"full");
        active_=true;
        std::vector<VARP> out;
        if(useTrunk) {
            out=trunk_->onForward({embeds,mask,pos,logitsLastIdx});
            if(out.size()!=1 || out[0]==nullptr || !out[0]->readMap<float>()) {active_=false;PrefillTrunkForward::end();return false;}
            const_cast<Tensor*>(out[0]->getTensor())->wait(Tensor::MAP_TENSOR_READ,true);
            mMeta->sync();
        } else out=Llm::forwardRaw(embeds,mask,pos);
        active_=false;PrefillTrunkForward::end();
        if(out.size()!=1 || out[0]==nullptr || !out[0]->getInfo()) return false;
        const int expectedHead=useTrunk ? 0 : 1;
        bool valid=PrefillTrunkForward::validate(mMeta.get(),managers_,prior,ids.size()) && headBefore_==expectedHead && headAfter_==expectedHead && attentionBefore_.size()==28 && attentionAfter_.size()==28;
        for(const auto& kv:managers_) valid=valid && attentionBefore_[kv.first]==1 && attentionAfter_[kv.first]==1;
        const int expected=prior+static_cast<int>(ids.size());
        valid=valid && mMeta->previous==static_cast<size_t>(expected) && !mMeta->add && !mMeta->remove && !mMeta->n_reserve;
        mContext->all_seq_len=expected;
        valid=lengths(index,"after",expected) && valid;
        const auto info=out[0]->getInfo(); const auto values=out[0]->readMap<float>();
        if(!values || info->type.code!=halide_type_float || info->type.bits!=32) return false;
        size_t nonfinite=0;for(int i=0;i<info->size;++i) if(!std::isfinite(values[i])) ++nonfinite;
        valid=valid && nonfinite==0 && (useTrunk ? info->size==static_cast<int>(ids.size())*1024 : info->size==151936);
        // Every last-prefill and decode output includes the complete vocabulary.
        std::string file;
        if(!useTrunk && (lastPrefill || decode)) {
            file=prefix+"-step"+std::to_string(index)+".f32";
            std::ofstream f(file,std::ios::binary); if(!f) return false;
            f.write(reinterpret_cast<const char*>(values),info->size*sizeof(float));f.close();valid=valid && bool(f);
        }
        std::printf("{\"event\":\"step\",\"step\":%d,\"phase\":\"%s\",\"route\":\"%s\",\"tokens\":%zu,\"meta_before\":%d,\"meta_after\":%zu,\"head_before_count\":%d,\"head_after_count\":%d,\"attention_names\":%zu,\"output_float_count\":%zu,\"nonfinite_count\":%zu,\"logits_file\":%s,\"passed\":%s}\n",
                    index,decode?"decode":(lastPrefill?"prefill_last":"prefill_nonlast"),useTrunk?"trunk":"full",ids.size(),prior,mMeta->previous,headBefore_,headAfter_,attentionAfter_.size(),info->size,nonfinite,PrefillTrunkBridge::quote(file).c_str(),valid?"true":"false");
        std::fflush(stdout);return valid;
    }
private:
    bool lengths(int step,const char* boundary,int expected) {
        bool ok=true;
        for(const auto& p:managers_) {
            auto manager=reinterpret_cast<CPUKVCacheManager*>(p.second);if(!manager)return false;
            const int actual=manager->kvLength();ok=ok && actual==expected;
            std::printf("{\"event\":\"kv_length\",\"step\":%d,\"boundary\":\"%s\",\"name\":%s,\"manager\":%llu,\"expected\":%d,\"actual\":%d}\n",step,boundary,PrefillTrunkBridge::quote(p.first).c_str(),(unsigned long long)p.second,expected,actual);
        }
        return ok;
    }
    std::shared_ptr<Module> trunk_;
    std::map<std::string,uintptr_t> managers_;
    std::map<std::string,int> attentionBefore_,attentionAfter_;
    bool candidate_=false,active_=false;int headBefore_=0,headAfter_=0;
};
int main(int argc,char** argv) {
    if(argc!=5) return 2;
    const std::string mode=argv[4];if(mode!="baseline" && mode!="candidate")return 2;
    std::ifstream fixture(argv[2],std::ios::binary);if(!fixture)return 3;
    std::string raw((std::istreambuf_iterator<char>(fixture)),std::istreambuf_iterator<char>());
    rapidjson::Document j;j.Parse(raw.c_str());
    if(j.HasParseError() || !j.IsObject() || !j.HasMember("input_ids") || !j["input_ids"].IsArray() || j["input_ids"].Size()!=93 ||
       !j.HasMember("decode_ids") || !j["decode_ids"].IsArray() || j["decode_ids"].Size()!=2 ||
       !j.HasMember("case_name") || !j["case_name"].IsString()) return 3;
    const std::string name=j["case_name"].GetString();
    int initial=0;bool continuous=false;
    if(name=="prefix31")initial=31;
    else if(name=="prefix32")initial=32;
    else if(name=="prefix33")initial=33;
    else if(name=="prefix64")initial=64;
    else if(name=="prefix65")initial=65;
    else if(name=="continuous64_suffix33"){initial=64;continuous=true;}
    else return 3;
    std::vector<int> input,decode;
    for(auto& x:j["input_ids"].GetArray()) {if(!x.IsInt() || x.GetInt()<0 || x.GetInt()>=151936)return 3;input.push_back(x.GetInt());}
    for(auto& x:j["decode_ids"].GetArray()) {if(!x.IsInt() || x.GetInt()<0 || x.GetInt()>=151936)return 3;decode.push_back(x.GetInt());}
    std::printf("{\"event\":\"case_begin\",\"case_name\":%s,\"mode\":%s,\"initial_prefill_tokens\":%d,\"suffix_prefill_tokens\":%d,\"suffix_source_begin\":%d,\"chunk_max\":32,\"fresh_llm\":true}\n",
                PrefillTrunkBridge::quote(name).c_str(),PrefillTrunkBridge::quote(mode).c_str(),initial,continuous?33:0,continuous?60:-1);
    auto config=std::make_shared<LlmConfig>(argv[1]);BoundaryForwardLlm llm(config,mode=="candidate");
    if(!llm.prepare())return 9;
    int index=0,prefillSteps=0,decodeSteps=0,outputs=0;
    const auto prefill=[&](int begin,int length) {
        int used=0;
        while(used<length) {
            const int count=(length-used)>32 ? 32 : length-used;
            const bool last=(used+count)==length; // Includes exact multiples of 32.
            std::vector<int> part(input.begin()+begin+used,input.begin()+begin+used+count);
            if(!llm.step(part,index++,false,last,argv[3]))return false;
            ++prefillSteps;if(last)++outputs;used+=count;
        }
        return true;
    };
    if(!prefill(0,initial))return 10;
    if(!llm.step({decode[0]},index++,true,false,argv[3]))return 10;
    ++decodeSteps;++outputs;
    if(continuous) {
        // Same KV managers/meta and absolute positions; not a formal text-chat turn.
        if(!prefill(60,33))return 10;
        if(!llm.step({decode[1]},index++,true,false,argv[3]))return 10;
        ++decodeSteps;++outputs;
    }
    std::printf("{\"event\":\"boundary_summary\",\"case_name\":%s,\"mode\":%s,\"prefill_steps\":%d,\"decode_steps\":%d,\"compared_vocab_outputs\":%d,\"passed\":true}\n",
                PrefillTrunkBridge::quote(name).c_str(),PrefillTrunkBridge::quote(mode).c_str(),prefillSteps,decodeSteps,outputs);
    return 0;
}
