// Real ordinary response, isolated opt-in archive; diagnostics are correctness evidence.
#include "llm/llm.hpp"
#include "prefill_response_elision.hpp"
#include "rapidjson/document.h"
#include "rapidjson/stringbuffer.h"
#include "rapidjson/writer.h"
#include <fstream>
#include <iostream>
#include <iterator>
#include <memory>
#include <sstream>
#include <vector>
using namespace MNN;using namespace MNN::Transformer;
static std::string read(const char* path){std::ifstream f(path);return std::string(std::istreambuf_iterator<char>(f),{});}
static void ids(const std::vector<int>& x){std::printf("[");for(size_t i=0;i<x.size();++i)std::printf("%s%d",i?",":"",x[i]);std::printf("]");}
static const char* status(LlmStatus s){switch(s){case LlmStatus::NORMAL_FINISHED:return "normal_finished";case LlmStatus::MAX_TOKENS_FINISHED:return "max_tokens_finished";case LlmStatus::INTERNAL_ERROR:return "internal_error";case LlmStatus::TIMEOUT:return "timeout";case LlmStatus::RUNNING:return "running";case LlmStatus::USER_CANCEL:return "user_cancel";default:return "not_loaded";}}
int main(int argc,char** argv){
 if(argc!=5){std::cerr<<"usage: worker config fixture baseline|candidate output_directory\n";return 2;}
 rapidjson::Document fixture;auto raw=read(argv[2]);fixture.Parse(raw.c_str());
 std::string mode=argv[3];if(fixture.HasParseError()||!fixture.IsObject()||!fixture.HasMember("cases")||!fixture["cases"].IsArray()||(mode!="baseline"&&mode!="candidate"))return 3;
 std::unique_ptr<Llm> llm(Llm::createLLM(argv[1]));if(!llm)return 4;
 llm->set_config(mode=="candidate"?R"({"prefill_head_elision":true,"enable_debug":true,"sampler_type":"greedy","async":false,"reuse_kv":false,"prompt_cache":false,"jinja":{"context":{"enable_thinking":false}}})":R"({"prefill_head_elision":false,"enable_debug":true,"sampler_type":"greedy","async":false,"reuse_kv":false,"prompt_cache":false,"jinja":{"context":{"enable_thinking":false}}})");
 if(!llm->load())return 5;
 ResponseElision::trace().dir=argv[4];
 llm->setDebugCallback([](const std::vector<Tensor*>&,const OperatorInfo* i){if(ResponseElision::trace().active&&i&&i->name()=="/lm/lm_head/Linear")std::printf("{\"event\":\"response_head\",\"step\":%d,\"boundary\":\"before\"}\n",ResponseElision::trace().step);return true;},[](const std::vector<Tensor*>&,const OperatorInfo* i){if(ResponseElision::trace().active&&i&&i->name()=="/lm/lm_head/Linear")std::printf("{\"event\":\"response_head\",\"step\":%d,\"boundary\":\"after\"}\n",ResponseElision::trace().step);return true;});
 int index=0;bool all=true;
 for(const auto& c:fixture["cases"].GetArray()){
  if(!c.IsObject()||!c.HasMember("name")||!c["name"].IsString()||!c.HasMember("max_new_tokens")||!c["max_new_tokens"].IsInt())return 6;
  bool text=c.HasMember("prompt");std::vector<int> input;
  if(text){if(!c["prompt"].IsString())return 7;input=llm->tokenizer_encode(llm->apply_chat_template(c["prompt"].GetString()));}
  else {if(!c.HasMember("input_ids")||!c["input_ids"].IsArray())return 8;for(auto& v:c["input_ids"].GetArray()){if(!v.IsInt())return 9;input.push_back(v.GetInt());}}
  if(input.empty()||input.size()>512||c["max_new_tokens"].GetInt()<1||c["max_new_tokens"].GetInt()>64)return 10;
  std::printf("{\"event\":\"request_begin\",\"request_index\":%d,\"name\":%s,\"mode\":%s,\"source_entry\":%s,\"first_step\":%d,\"max_new_tokens\":%d,\"input_ids\":",index,ResponseElision::quote(c["name"].GetString()).c_str(),ResponseElision::quote(mode).c_str(),text?"\"response(string)\"":"\"response(token IDs)\"",ResponseElision::trace().step,c["max_new_tokens"].GetInt());ids(input);std::printf("}\n");std::fflush(stdout);
  llm->reset();
  std::printf("{\"event\":\"request_reset\",\"request_index\":%d,\"context_all\":%d}\n",index,llm->getContext()->all_seq_len);
  std::ostringstream stream;
  if(text)llm->response(std::string(c["prompt"].GetString()),&stream,"",c["max_new_tokens"].GetInt());else llm->response(input,&stream,"",c["max_new_tokens"].GetInt());
  const auto* context=llm->getContext();bool ok=context->status==LlmStatus::NORMAL_FINISHED||context->status==LlmStatus::MAX_TOKENS_FINISHED;
  std::printf("{\"event\":\"request_end\",\"request_index\":%d,\"name\":%s,\"last_step_exclusive\":%d,\"status\":%s,\"status_code\":%d,\"ok\":%s,\"all_seq_len\":%d,\"prompt_len\":%d,\"text\":%s,\"stream_text\":%s,\"output_ids\":",index,ResponseElision::quote(c["name"].GetString()).c_str(),ResponseElision::trace().step,ResponseElision::quote(status(context->status)).c_str(),int(context->status),ok?"true":"false",context->all_seq_len,context->prompt_len,ResponseElision::quote(context->generate_str).c_str(),ResponseElision::quote(stream.str()).c_str());ids(context->output_tokens);std::printf("}\n");std::fflush(stdout);all=all&&ok;++index;
 }
 ResponseElision::trace().dir.clear();llm.reset();std::printf("{\"event\":\"worker_complete\",\"requests\":%d,\"ok\":%s}\n",index,all?"true":"false");return all?0:11;
}
