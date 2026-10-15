// No operator tracing: response costs include request-local trunk construction.
#include "llm/llm.hpp"
#include "prefill_trunk_reuse.hpp"
#include "rapidjson/document.h"
#include "rapidjson/writer.h"
#include "rapidjson/stringbuffer.h"
#include <map>
#include <atomic>
#include <thread>
#include <fstream>
#include <iostream>
#include <iterator>
#include <memory>
#include <sstream>
#include <vector>
using namespace MNN; using namespace MNN::Transformer;
struct Memory {long rss=-1,hwm=-1;};
static Memory memory(){std::ifstream f("/proc/self/status");std::string line;Memory m;while(std::getline(f,line)){std::istringstream s(line);std::string k,u;long n;if(s>>k>>n>>u){if(k=="VmRSS:")m.rss=n;if(k=="VmHWM:")m.hwm=n;}}return m;}
static Memory preparedMemory;
static void prepared(){preparedMemory=memory();}
static std::string read(const char* p){std::ifstream f(p);return std::string(std::istreambuf_iterator<char>(f),{});}
static void ids(const std::vector<int>& x){std::printf("[");for(size_t i=0;i<x.size();++i)std::printf("%s%d",i?",":"",x[i]);std::printf("]");}
static const char* status(LlmStatus s){switch(s){case LlmStatus::NORMAL_FINISHED:return "normal_finished";case LlmStatus::MAX_TOKENS_FINISHED:return "max_tokens_finished";case LlmStatus::INTERNAL_ERROR:return "internal_error";case LlmStatus::TIMEOUT:return "timeout";case LlmStatus::RUNNING:return "running";case LlmStatus::USER_CANCEL:return "user_cancel";default:return "not_loaded";}}
static std::string json(const rapidjson::Value& value){rapidjson::StringBuffer b;rapidjson::Writer<rapidjson::StringBuffer> writer(b);value.Accept(writer);return b.GetString();}
struct Owner {std::unique_ptr<Llm> llm;bool loaded=false;};
static void callbacks(Llm* llm){
#if PREFILL_REUSE_DIAGNOSTIC
 llm->setDebugCallback([](const std::vector<Tensor*>&,const OperatorInfo* i){if(ResponseElision::trace().active&&i&&i->name()=="/lm/lm_head/Linear")std::printf("{\"event\":\"response_head\",\"step\":%d,\"boundary\":\"before\"}\n",ResponseElision::trace().step);return true;},[](const std::vector<Tensor*>&,const OperatorInfo* i){if(ResponseElision::trace().active&&i&&i->name()=="/lm/lm_head/Linear")std::printf("{\"event\":\"response_head\",\"step\":%d,\"boundary\":\"after\"}\n",ResponseElision::trace().step);return true;});
#endif
}
static int step(){
#if PREFILL_REUSE_DIAGNOSTIC
 return ResponseElision::trace().step;
#else
 return -1;
#endif
}
int main(int argc,char** argv){
 if(argc!=4&&argc!=5){std::cerr<<"usage: worker config fixture outputdir [--sample-rss]\n";return 2;}
 const bool sample=argc==5&&std::string(argv[4])=="--sample-rss";if(argc==5&&!sample)return 2;
 rapidjson::Document fixture;auto raw=read(argv[2]);fixture.Parse(raw.c_str());
 if(fixture.HasParseError()||!fixture.IsObject()||!fixture.HasMember("operations")||!fixture["operations"].IsArray()||fixture["operations"].Empty())return 3;
 std::map<std::string,Owner> owners;
#if PREFILL_REUSE_DIAGNOSTIC
 ResponseElision::lifecycle()=true;ResponseElision::trace().dir=argv[3];
#endif
 int index=0;bool all=true;
 for(const auto& c:fixture["operations"].GetArray()){
  if(!c.IsObject()||!c.HasMember("action")||!c["action"].IsString()||!c.HasMember("instance")||!c["instance"].IsString())return 6;
  const std::string action=c["action"].GetString(),instance=c["instance"].GetString();
  if(action=="create"){
   if(owners.count(instance))return 12;Owner o;o.llm.reset(Llm::createLLM(argv[1]));if(!o.llm)return 4;
   o.llm->set_config(R"({"prefill_head_elision":false,"enable_debug":false,"sampler_type":"greedy","async":false,"reuse_kv":false,"prompt_cache":false,"jinja":{"context":{"enable_thinking":false}}})");
#if PREFILL_REUSE_DIAGNOSTIC
   o.llm->set_config(R"({"enable_debug":true,"prefill_reuse_lifecycle":true})");
#endif
   if(c.HasMember("patch"))o.llm->set_config(json(c["patch"]));callbacks(o.llm.get());owners[instance]=std::move(o);
  }else if(action=="destroy"){
   if(!owners.count(instance))return 13;owners.erase(instance);
  }else if(action=="config"){
   if(!owners.count(instance)||!c.HasMember("patch")||!c["patch"].IsObject())return 14;owners[instance].llm->set_config(json(c["patch"]));
  }else if(action=="load"){
   if(!owners.count(instance)||!c.HasMember("expected")||!c["expected"].IsBool())return 15;
   auto& o=owners[instance];auto t=ResponseElision::now();o.loaded=o.llm->load();auto m=memory();
   std::printf("{\"event\":\"reuse_operation\",\"action\":\"load\",\"instance\":%s,\"expected\":%s,\"actual\":%s,\"ok\":%s,\"load_wall_us\":%lld,\"rss_kb\":%ld,\"hwm_kb\":%ld}\n",ResponseElision::quote(instance).c_str(),c["expected"].GetBool()?"true":"false",o.loaded?"true":"false",o.loaded==c["expected"].GetBool()?"true":"false",(long long)(ResponseElision::now()-t),m.rss,m.hwm);std::fflush(stdout);
   if(o.loaded!=c["expected"].GetBool())return 16;continue;
  }else if(action!="request")return 17;
  if(action!="request"){std::printf("{\"event\":\"reuse_operation\",\"action\":%s,\"instance\":%s,\"ok\":true}\n",ResponseElision::quote(action).c_str(),ResponseElision::quote(instance).c_str());std::fflush(stdout);continue;}
  if(!owners.count(instance)||!owners[instance].loaded)return 18;auto& llm=owners[instance].llm;
  if(!c.HasMember("name")||!c["name"].IsString()||!c.HasMember("mode")||!c["mode"].IsString()||!c.HasMember("phase")||!c["phase"].IsString()||!c.HasMember("max_new_tokens")||!c["max_new_tokens"].IsInt())return 6;
  const std::string mode=c["mode"].GetString();if(mode!="baseline"&&mode!="candidate"&&mode!="unchanged")return 6;
  bool text=c.HasMember("prompt");std::vector<int> input;
  if(text){if(!c["prompt"].IsString())return 7;input=llm->tokenizer_encode(llm->apply_chat_template(c["prompt"].GetString()));}
  else {if(!c.HasMember("input_ids")||!c["input_ids"].IsArray())return 8;for(auto& v:c["input_ids"].GetArray()){if(!v.IsInt())return 9;input.push_back(v.GetInt());}}
  if(input.empty()||input.size()>512||c["max_new_tokens"].GetInt()<1||c["max_new_tokens"].GetInt()>64)return 10;
  std::printf("{\"event\":\"request_begin\",\"request_index\":%d,\"instance\":%s,\"name\":%s,\"mode\":%s,\"phase\":%s,\"first_step\":%d,\"max_new_tokens\":%d,\"input_ids\":",index,ResponseElision::quote(instance).c_str(),ResponseElision::quote(c["name"].GetString()).c_str(),ResponseElision::quote(mode).c_str(),ResponseElision::quote(c["phase"].GetString()).c_str(),step(),c["max_new_tokens"].GetInt());ids(input);std::printf("}\n");std::fflush(stdout);
  ResponseElision::resetMetrics(mode=="candidate");preparedMemory=Memory();if(sample)ResponseElision::metrics().prepared_callback=prepared;
  auto before=memory();std::atomic<bool> stop(false);std::atomic<long> peak(before.rss);std::atomic<int> samples(0);std::thread sampler;
  if(sample)sampler=std::thread([&](){while(!stop.load()){auto m=memory();if(m.rss>peak.load())peak.store(m.rss);++samples;std::this_thread::sleep_for(std::chrono::milliseconds(10));}});
  std::ostringstream stream;const auto requestStart=ResponseElision::now();
  if(mode!="unchanged")llm->set_config(mode=="candidate"?R"({"prefill_head_elision":true})":R"({"prefill_head_elision":false})");
  if(c.HasMember("patch")){if(!c["patch"].IsObject())return 19;llm->set_config(json(c["patch"]));}
  llm->reset();
  if(text)llm->response(std::string(c["prompt"].GetString()),&stream,"",c["max_new_tokens"].GetInt());else llm->response(input,&stream,"",c["max_new_tokens"].GetInt());
  const auto wall=ResponseElision::now()-requestStart;
  if(sample){stop=true;sampler.join();}auto after=memory();if(after.rss>peak.load())peak=after.rss;
  const auto* ctx=llm->getContext();const auto m=ResponseElision::metrics();bool ok=ctx->status==LlmStatus::NORMAL_FINISHED||ctx->status==LlmStatus::MAX_TOKENS_FINISHED;
  std::printf("{\"event\":\"request_end\",\"request_index\":%d,\"instance\":%s,\"name\":%s,\"mode\":%s,\"phase\":%s,\"last_step_exclusive\":%d,\"cache_hit\":%s,\"ok\":%s,\"status\":%s,\"status_code\":%d,\"all_seq_len\":%d,\"prompt_len\":%d,\"requested\":%s,\"eligible\":%s,\"prepared\":%s,\"trunk_count\":%d,\"full_count\":%d,\"decode_count\":%d,\"route_check_us\":%lld,\"prepare_us\":%lld,\"release_us\":%lld,\"request_wall_us\":%lld,\"native_prefill_us\":%lld,\"native_decode_us\":%lld,\"native_sample_us\":%lld,\"rss_before_kb\":%ld,\"rss_after_kb\":%ld,\"hwm_after_kb\":%ld,\"prepared_rss_kb\":%ld,\"prepared_hwm_kb\":%ld,\"sample_peak_rss_kb\":%ld,\"sample_count\":%d,\"text\":%s,\"stream_text\":%s,\"output_ids\":",index,ResponseElision::quote(instance).c_str(),ResponseElision::quote(c["name"].GetString()).c_str(),ResponseElision::quote(mode).c_str(),ResponseElision::quote(c["phase"].GetString()).c_str(),step(),m.cache_hit?"true":"false",ok?"true":"false",ResponseElision::quote(status(ctx->status)).c_str(),int(ctx->status),ctx->all_seq_len,ctx->prompt_len,m.requested?"true":"false",m.eligible?"true":"false",m.prepared?"true":"false",m.trunk,m.full,m.decode,(long long)m.route_check_us,(long long)m.prepare_us,(long long)m.release_us,(long long)wall,(long long)ctx->prefill_us,(long long)ctx->decode_us,(long long)ctx->sample_us,before.rss,after.rss,after.hwm,preparedMemory.rss,preparedMemory.hwm,peak.load(),samples.load(),ResponseElision::quote(ctx->generate_str).c_str(),ResponseElision::quote(stream.str()).c_str());ids(ctx->output_tokens);std::printf("}\n");std::fflush(stdout);all=all&&ok;++index;
 }
 owners.clear();std::printf("{\"event\":\"worker_complete\",\"requests\":%d,\"ok\":%s}\n",index,all?"true":"false");return all?0:11;
}
