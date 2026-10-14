#include "llm/llm.hpp"
#include "rapidjson/document.h"
#include "rapidjson/writer.h"
#include "rapidjson/stringbuffer.h"
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <memory>
#include <sstream>
#include <streambuf>
#include <string>
#include <map>
#include <mutex>
#include <thread>
#include <vector>
using MNN::Transformer::Llm;
using MNN::Transformer::LlmStatus;
using MNN::Transformer::ChatMessages;
using rapidjson::Document;
using rapidjson::Value;
static bool io(int fd, void* data, size_t n, bool writing) {
    char* p=static_cast<char*>(data);
    while(n) { ssize_t k=writing?send(fd,p,n,MSG_NOSIGNAL):recv(fd,p,n,0); if(k<0&&errno==EINTR)continue; if(k<=0)return false; p+=k;n-=k; }
    return true;
}
static std::string encode(const Document& doc) { rapidjson::StringBuffer buf;rapidjson::Writer<rapidjson::StringBuffer> writer(buf);doc.Accept(writer);return buf.GetString(); }
static bool sendJson(int fd,const Document& doc) { std::string data=encode(doc);if(data.size()>1024*1024)return false;uint32_t n=htonl(data.size());return io(fd,&n,4,true)&&io(fd,&data[0],data.size(),true); }
static bool receive(int fd,Document& doc) { uint32_t n=0;if(!io(fd,&n,4,false))return false;n=ntohl(n);if(!n||n>1024*1024)return false;std::string data(n,'\0');if(!io(fd,&data[0],n,false))return false;doc.Parse<rapidjson::kParseValidateEncodingFlag>(data.data(),data.size());return !doc.HasParseError()&&doc.IsObject(); }
static void str(Document& d,const char* name,const std::string& text) { auto& a=d.GetAllocator();d.AddMember(Value(name,a),Value(text.data(),text.size(),a),a); }
static const char* statusName(LlmStatus s) { switch(s) {case LlmStatus::NORMAL_FINISHED:return "normal_finished";case LlmStatus::MAX_TOKENS_FINISHED:return "max_tokens_finished";case LlmStatus::INTERNAL_ERROR:return "internal_error";case LlmStatus::TIMEOUT:return "timeout";case LlmStatus::USER_CANCEL:return "user_cancel";case LlmStatus::RUNNING:return "running";default:return "not_loaded";} }
// The tokenizer can emit incomplete UTF-8 bytes. Hex framing preserves bytes;
// only the client incremental decoder decides when a complete character exists.
class ChunkBuffer : public std::streambuf {
public:
    ChunkBuffer(int fd, const std::string& id) : mFd(fd), mId(id), mCount(0), mGood(true) {}
    size_t count() const { return mCount; }
    bool good() const { return mGood; }
    const std::string& bytes() const { return mBytes; }
protected:
    std::streamsize xsputn(const char* data, std::streamsize n) override {
        if (!mGood) return 0;
        if (n <= 0) return 0;
        mBytes.append(data, static_cast<size_t>(n));
        if (mFd < 0) return n;
        static const char digits[] = "0123456789abcdef";
        std::string hex; hex.reserve(static_cast<size_t>(n) * 2);
        for (std::streamsize i = 0; i < n; ++i) {
            unsigned char byte = static_cast<unsigned char>(data[i]);
            hex.push_back(digits[byte >> 4]); hex.push_back(digits[byte & 15]);
        }
        Document chunk; chunk.SetObject(); auto& a = chunk.GetAllocator();
        str(chunk, "event", "chunk"); str(chunk, "id", mId); str(chunk, "bytes_hex", hex);
        chunk.AddMember("pid", getpid(), a); chunk.AddMember("load_count", 1, a);
        chunk.AddMember("seq", static_cast<unsigned>(mCount), a);
        if (!sendJson(mFd, chunk)) { mGood = false; return 0; }
        ++mCount; return n;
    }
    int_type overflow(int_type value) override {
        if (traits_type::eq_int_type(value, traits_type::eof())) return traits_type::not_eof(value);
        char byte = traits_type::to_char_type(value);
        return xsputn(&byte, 1) == 1 ? value : traits_type::eof();
    }
    int sync() override { return mGood ? 0 : -1; }
private:
    int mFd; std::string mId, mBytes; size_t mCount; bool mGood;
};

// Observation only: visible Pipeline callback intervals, not pure kernel time.
struct OpProfile {
    using Clock = std::chrono::steady_clock;
    struct Row { std::string name,type; uint64_t count=0, elapsed=0; };
    struct Active { const MNN::OperatorInfo* info; Clock::time_point start; size_t sample; };
    struct Sample { std::string name,type; std::vector<std::vector<int>> inputs,outputs; };
    std::mutex mutex; bool enabled=false,window=false;
    uint64_t beforeCount=0,afterCount=0,pairs=0,nested=0,unmatched=0,nullInfo=0,mismatch=0,dropped=0,concurrent=0;
    std::map<std::thread::id,std::vector<Active>> active;
    std::map<std::string,Row> rows; std::vector<Sample> samples;
    static std::string bounded(const std::string& s) { if(s.size()<=256)return s;size_t n=256;while(n&&((static_cast<unsigned char>(s[n])&0xc0)==0x80))--n;return s.substr(0,n); }
    static std::vector<std::vector<int>> shapes(const std::vector<MNN::Tensor*>& ts) {
        std::vector<std::vector<int>> out; for(size_t i=0;i<ts.size()&&i<8;++i){std::vector<int> shape;if(!ts[i])shape.push_back(-1);else for(int d=0;d<ts[i]->dimensions()&&d<8;++d)shape.push_back(ts[i]->length(d));out.push_back(shape);}return out;
    }
    void begin() { std::lock_guard<std::mutex> lock(mutex);beforeCount=afterCount=pairs=nested=unmatched=nullInfo=mismatch=dropped=concurrent=0;active.clear();rows.clear();samples.clear();window=true; }
    bool before(const std::vector<MNN::Tensor*>& ts,const MNN::OperatorInfo* info) {
        std::lock_guard<std::mutex> lock(mutex);if(!window)return true;++beforeCount;if(!info){++nullInfo;return true;}
        auto tid=std::this_thread::get_id();auto& stack=active[tid];if(!stack.empty())++nested;for(auto& a:active)if(a.first!=tid&&!a.second.empty()){++concurrent;break;}
        size_t sample=16;if(samples.size()<16){sample=samples.size();Sample s;s.name=bounded(info->name());s.type=bounded(info->type());s.inputs=shapes(ts);samples.push_back(s);}
        stack.push_back({info,Clock::now(),sample});return true;
    }
    bool after(const std::vector<MNN::Tensor*>& ts,const MNN::OperatorInfo* info) {
        auto end=Clock::now();std::lock_guard<std::mutex> lock(mutex);if(!window)return true;++afterCount;if(!info){++nullInfo;return true;}
        auto it=active.find(std::this_thread::get_id());if(it==active.end()||it->second.empty()){++unmatched;return true;}Active start=it->second.back();it->second.pop_back();if(start.info!=info){++mismatch;return true;}++pairs;
        if(start.sample<samples.size())samples[start.sample].outputs=shapes(ts);
        std::string name=bounded(info->name()),type=bounded(info->type()),key=type;auto row=rows.find(key);if(row==rows.end()){if(rows.size()>=512){++dropped;return true;}Row r;r.name=name;r.type=type;row=rows.emplace(key,r).first;}
        ++row->second.count;row->second.elapsed+=std::chrono::duration_cast<std::chrono::nanoseconds>(end-start.start).count();return true;
    }
    static Value shapeJson(const std::vector<std::vector<int>>& shapes,Document::AllocatorType& a){Value out(rapidjson::kArrayType);for(auto& shape:shapes){Value dims(rapidjson::kArrayType);for(int d:shape)dims.PushBack(d,a);out.PushBack(dims,a);}return out;}
    void finish(Document& out) {
        std::lock_guard<std::mutex> lock(mutex);window=false;auto& a=out.GetAllocator();Value v(rapidjson::kObjectType);v.AddMember("version",1,a);v.AddMember("enabled",enabled,a);v.AddMember("scope","whole_response_visible_pipeline_callbacks",a);v.AddMember("aggregate_key","type",a);v.AddMember("aggregate_name_semantics","first_observed_example",a);v.AddMember("before_count",beforeCount,a);v.AddMember("after_count",afterCount,a);v.AddMember("pairs",pairs,a);
        uint64_t pending=0;for(auto& it:active)pending+=it.second.size();v.AddMember("balance",beforeCount==afterCount&&pending==0&&unmatched==0&&mismatch==0&&nullInfo==0,a);
        Value diag(rapidjson::kObjectType);diag.AddMember("nested",nested,a);diag.AddMember("concurrent",concurrent,a);diag.AddMember("unmatched_after",unmatched,a);diag.AddMember("pending_before",pending,a);diag.AddMember("info_null",nullInfo,a);diag.AddMember("info_mismatch",mismatch,a);diag.AddMember("dropped_aggregate_pairs",dropped,a);v.AddMember("diagnostics",diag,a);
        Value aggregate(rapidjson::kArrayType);for(auto& it:rows){auto& r=it.second;Value row(rapidjson::kObjectType);row.AddMember("name",Value(r.name.c_str(),a),a);row.AddMember("type",Value(r.type.c_str(),a),a);row.AddMember("count",r.count,a);row.AddMember("elapsed_ns",r.elapsed,a);aggregate.PushBack(row,a);}v.AddMember("op_aggregate",aggregate,a);
        Value examples(rapidjson::kArrayType);for(auto& s:samples){Value sample(rapidjson::kObjectType);sample.AddMember("name",Value(s.name.c_str(),a),a);sample.AddMember("type",Value(s.type.c_str(),a),a);sample.AddMember("input_shapes",shapeJson(s.inputs,a),a);sample.AddMember("output_shapes",shapeJson(s.outputs,a),a);examples.PushBack(sample,a);}v.AddMember("samples",examples,a);out.AddMember("op_profile",v,a);
    }
};
static OpProfile profile;

static Document infer(Llm* llm,const Document& request,int fd=-1) {
    Document out;out.SetObject();auto& a=out.GetAllocator();out.AddMember("pid",getpid(),a);out.AddMember("load_count",1,a);
    std::ostringstream pointer;pointer<<llm;str(out,"llm_identity",pointer.str());str(out,"id",request["id"].GetString());
    auto c=llm->getContext();out.AddMember("before_history",static_cast<int>(c->history_tokens.size()),a);out.AddMember("before_all_seq",c->all_seq_len,a);
    llm->reset();out.AddMember("reset_history",static_cast<int>(c->history_tokens.size()),a);out.AddMember("reset_output",static_cast<int>(c->output_tokens.size()),a);out.AddMember("reset_all_seq",c->all_seq_len,a);out.AddMember("reset_gen_seq",c->gen_seq_len,a);
    ChunkBuffer buffer(fd, std::string(request["id"].GetString(), request["id"].GetStringLength()));
    std::ostream stream(&buffer);
    ChatMessages messages;
    for (auto& message : request["messages"].GetArray())
        messages.emplace_back(std::string(message["role"].GetString(), message["role"].GetStringLength()), std::string(message["content"].GetString(), message["content"].GetStringLength()));
    auto full=llm->tokenizer_encode(llm->apply_chat_template(messages));
    out.AddMember("input_tokens",static_cast<unsigned>(full.size()),a);
    Value fullInputIds(rapidjson::kArrayType);
    for (int token : full) fullInputIds.PushBack(token, a);
    out.AddMember("full_input_ids", fullInputIds, a);
    profile.begin();
    const auto responseBegin = std::chrono::steady_clock::now();
    llm->response(messages,&stream,"",request["max_new_tokens"].GetInt());
    const int64_t responseWallUs = std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::steady_clock::now() - responseBegin).count();
    profile.finish(out);
    out.AddMember("message_count", static_cast<unsigned>(messages.size()), a);
    str(out,"history_mode","full_recompute");
    str(out,"event","final");out.AddMember("chunk_count",static_cast<unsigned>(buffer.count()),a);
    out.AddMember("stream_bytes_match",buffer.bytes()==c->generate_str,a);
    out.AddMember("stream_transport_ok",buffer.good(),a);
    str(out,"text",c->generate_str);str(out,"status",statusName(c->status));out.AddMember("status_code",static_cast<int>(c->status),a);out.AddMember("after_all_seq",c->all_seq_len,a);out.AddMember("prompt_tokens",c->prompt_len,a);
    Value tokens(rapidjson::kArrayType);for(int token:c->output_tokens)tokens.PushBack(token,a);out.AddMember("tokens",tokens,a);
    bool ok=(c->status==LlmStatus::NORMAL_FINISHED||c->status==LlmStatus::MAX_TOKENS_FINISHED)&&!c->generate_str.empty()&&buffer.good()&&buffer.bytes()==c->generate_str;
    // These fields describe this completed synchronous AR request only.
    // generate_init resets both timers and gen_seq_len even on the KV delta path.
    const int64_t outputCount = static_cast<int64_t>(c->output_tokens.size());
    const int64_t inputCount = static_cast<int64_t>(full.size());
    const int64_t prefillCount = c->prompt_len;
    const bool countersValid = prefillCount > 0 && prefillCount <= inputCount &&
        outputCount > 0 && c->gen_seq_len == outputCount && c->prefill_us >= 0 &&
        c->decode_us >= 0 && responseWallUs >= 0;
    ok = ok && countersValid;
    if (ok) {
        Value metrics(rapidjson::kObjectType);
        metrics.AddMember("version", 1, a);
        metrics.AddMember("input_tokens", inputCount, a);
        metrics.AddMember("prefill_tokens", prefillCount, a);
        metrics.AddMember("reused_tokens", inputCount - prefillCount, a);
        metrics.AddMember("output_tokens", outputCount, a);
        // AR appends EOS before stopping, without forwarding EOS. At MAX it
        // forwards every non-stop token, including the last budget token.
        const int64_t decodeSteps = outputCount - (c->status == LlmStatus::NORMAL_FINISHED ? 1 : 0);
        metrics.AddMember("decode_steps", decodeSteps, a);
        metrics.AddMember("prefill_us", c->prefill_us, a);
        metrics.AddMember("decode_us", c->decode_us, a);
        metrics.AddMember("response_wall_us", responseWallUs, a);
        out.AddMember("metrics", metrics, a);
    }
    out.AddMember("ok",ok,a);return out;
}
static bool valid(const Document& d) {
    if(!d.IsObject()||!d.HasMember("id")||!d["id"].IsString()||!d.HasMember("messages")||!d["messages"].IsArray()||!d.HasMember("max_new_tokens")||!d["max_new_tokens"].IsInt()||!d.HasMember("max_context_tokens")||!d["max_context_tokens"].IsInt())return false;
    auto idLength=d["id"].GetStringLength();
    if(!idLength||idLength>128||memchr(d["id"].GetString(),0,idLength)||d["max_new_tokens"].GetInt()<1||d["max_new_tokens"].GetInt()>512||d["max_context_tokens"].GetInt()<1||d["max_context_tokens"].GetInt()>8192)return false;
    if(d["messages"].Empty()||d["messages"].Size()>31||d["messages"].Size()%2!=1)return false;
    unsigned index=0; size_t bytes=0;
    for(auto& m:d["messages"].GetArray()) {
        if(!m.IsObject()||!m.HasMember("role")||!m["role"].IsString()||!m.HasMember("content")||!m["content"].IsString())return false;
        std::string role(m["role"].GetString(),m["role"].GetStringLength());
        if(role!=(index%2==0?"user":"assistant"))return false;
        auto n=m["content"].GetStringLength();if(!n||n>65536||memchr(m["content"].GetString(),0,n))return false;
        bytes+=n;++index;
    }
    return bytes<=262144;
}
static bool budget(Llm* llm,const Document& request,Document& rejection) {
    ChatMessages messages;
    for(auto& m:request["messages"].GetArray())messages.emplace_back(std::string(m["role"].GetString(),m["role"].GetStringLength()),std::string(m["content"].GetString(),m["content"].GetStringLength()));
    auto input=llm->tokenizer_encode(llm->apply_chat_template(messages));
    if(input.size()+request["max_new_tokens"].GetInt()<=static_cast<unsigned>(request["max_context_tokens"].GetInt()))return true;
    rejection.SetObject();auto& a=rejection.GetAllocator();str(rejection,"id",request["id"].GetString());str(rejection,"event","rejected");str(rejection,"reason","context_budget_exceeded");
    rejection.AddMember("pid",getpid(),a);rejection.AddMember("load_count",1,a);rejection.AddMember("input_tokens",static_cast<unsigned>(input.size()),a);rejection.AddMember("max_new_tokens",request["max_new_tokens"].GetInt(),a);rejection.AddMember("max_context_tokens",request["max_context_tokens"].GetInt(),a);
    return false;
}
int main(int argc,char** argv) {
    if(argc!=4)return 2;
    std::ifstream configFile(argv[1]);std::ostringstream configBytes;configBytes<<configFile.rdbuf();Document profileConfig;profileConfig.Parse(configBytes.str().c_str());
    if(profileConfig.HasParseError()||!profileConfig.IsObject()||!profileConfig.HasMember("op_profile_enabled")||!profileConfig["op_profile_enabled"].IsBool())return 2;
    profile.enabled=profileConfig["op_profile_enabled"].GetBool();std::setvbuf(stdout,nullptr,_IONBF,0);
    std::unique_ptr<Llm> llm(Llm::createLLM(argv[1]));if(!llm)return 3;
    llm->set_config(R"({"enable_debug":true,"chunk":32,"reuse_kv":false,"prompt_cache":false,"async":false,"speculative_type":"","jinja":{"context":{"enable_thinking":false}}})");
    if(profile.enabled)llm->setDebugCallback(
        [](const std::vector<MNN::Tensor*>& t,const MNN::OperatorInfo* i){return profile.before(t,i);},
        [](const std::vector<MNN::Tensor*>& t,const MNN::OperatorInfo* i){return profile.after(t,i);});
    if(!llm->load())return 3;
    if(std::string(argv[2])=="--cold") { std::ifstream f(argv[3]);std::ostringstream s;s<<f.rdbuf();Document request;request.Parse<rapidjson::kParseValidateEncodingFlag>(s.str().c_str());if(request.HasParseError()||!valid(request))return 2;Document rejected;if(!budget(llm.get(),request,rejected)){std::printf("%s\n",encode(rejected).c_str());return 8;}Document out=infer(llm.get(),request);std::printf("%s\n",encode(out).c_str());return out["ok"].GetBool()?0:4; }
    if(std::string(argv[2])!="--serve")return 2;std::string token=argv[3];if(token.size()!=32)return 2;
    int listener=socket(AF_INET,SOCK_STREAM,0);sockaddr_in addr={};addr.sin_family=AF_INET;addr.sin_addr.s_addr=htonl(INADDR_LOOPBACK);addr.sin_port=0;
    if(listener<0||bind(listener,reinterpret_cast<sockaddr*>(&addr),sizeof(addr))||listen(listener,1))return 5;
    socklen_t length=sizeof(addr);if(getsockname(listener,reinterpret_cast<sockaddr*>(&addr),&length))return 5;
    Document ready;ready.SetObject();auto& a=ready.GetAllocator();str(ready,"event","ready");ready.AddMember("op_profile_enabled",profile.enabled,a);str(ready,"bind","127.0.0.1");ready.AddMember("pid",getpid(),a);ready.AddMember("port",ntohs(addr.sin_port),a);ready.AddMember("load_count",1,a);std::printf("%s\n",encode(ready).c_str());
    int client=accept(listener,nullptr,nullptr);if(client<0){close(listener);return 5;}int result=0;ChatMessages committed;
    for(;;) { Document request;if(!receive(client,request)){result=6;break;}
        if(!request.HasMember("session_token")||!request["session_token"].IsString()||(request["session_token"].GetStringLength()!=32||memcmp(token.data(),request["session_token"].GetString(),32)!=0)){result=7;break;}
        if(request.HasMember("command")&&request["command"].IsString()&&std::string(request["command"].GetString())=="shutdown") { Document done;done.SetObject();done.AddMember("ok",true,done.GetAllocator());done.AddMember("pid",getpid(),done.GetAllocator());str(done,"event","shutdown");sendJson(client,done);break; }
        if(request.HasMember("command")&&request["command"].IsString()&&std::string(request["command"].GetString())=="new") {
            committed.clear();llm->reset();Document done;done.SetObject();auto& a=done.GetAllocator();done.AddMember("ok",true,a);done.AddMember("pid",getpid(),a);done.AddMember("committed_message_count",0,a);auto c=llm->getContext();done.AddMember("reset_history",static_cast<unsigned>(c->history_tokens.size()),a);done.AddMember("reset_output",static_cast<unsigned>(c->output_tokens.size()),a);done.AddMember("reset_all_seq",c->all_seq_len,a);done.AddMember("reset_gen_seq",c->gen_seq_len,a);str(done,"event","new");sendJson(client,done);continue;
        }
        if(!valid(request)){result=2;break;}ChatMessages proposed;
        for(auto& m:request["messages"].GetArray())proposed.emplace_back(std::string(m["role"].GetString(),m["role"].GetStringLength()),std::string(m["content"].GetString(),m["content"].GetStringLength()));
        if(proposed.size()!=committed.size()+1||!std::equal(committed.begin(),committed.end(),proposed.begin())){result=2;break;}
        Document rejected;if(!budget(llm.get(),request,rejected)){if(!sendJson(client,rejected)){result=6;break;}continue;}Document out=infer(llm.get(),request,client);
        if(out["ok"].GetBool()){committed=proposed;committed.emplace_back("assistant",std::string(out["text"].GetString(),out["text"].GetStringLength()));}
        out.AddMember("committed_message_count",static_cast<unsigned>(committed.size()),out.GetAllocator());
        out.AddMember("committed_turn_count",static_cast<unsigned>(committed.size()/2),out.GetAllocator());
        if(!sendJson(client,out)){result=6;break;}if(!out["ok"].GetBool()){result=4;break;}
    }
    close(client);close(listener);std::printf("{\"event\":\"stopped\",\"pid\":%d,\"load_count\":1,\"exit\":%d}\n",getpid(),result);return result;
}
