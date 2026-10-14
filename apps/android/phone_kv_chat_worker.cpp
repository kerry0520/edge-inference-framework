#include "llm/llm.hpp"
#include "rapidjson/document.h"
#include "rapidjson/writer.h"
#include "rapidjson/stringbuffer.h"
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>
#include <cerrno>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <memory>
#include <sstream>
#include <streambuf>
#include <string>
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
static bool sendJson(int fd,const Document& doc) { std::string data=encode(doc);uint32_t n=htonl(data.size());return io(fd,&n,4,true)&&io(fd,&data[0],data.size(),true); }
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
static Document infer(Llm* llm,const Document& request,int fd=-1) {
    Document out;out.SetObject();auto& a=out.GetAllocator();out.AddMember("pid",getpid(),a);out.AddMember("load_count",1,a);
    std::ostringstream pointer;pointer<<llm;str(out,"llm_identity",pointer.str());str(out,"id",request["id"].GetString());
    auto c=llm->getContext();out.AddMember("before_history",static_cast<int>(c->history_tokens.size()),a);out.AddMember("before_all_seq",c->all_seq_len,a);
    bool reset_applied=c->all_seq_len==0; if(reset_applied)llm->reset();out.AddMember("reset_applied",reset_applied,a);out.AddMember("reset_history",static_cast<int>(c->history_tokens.size()),a);out.AddMember("reset_output",static_cast<int>(c->output_tokens.size()),a);out.AddMember("reset_all_seq",c->all_seq_len,a);out.AddMember("reset_gen_seq",c->gen_seq_len,a);
    ChunkBuffer buffer(fd, std::string(request["id"].GetString(), request["id"].GetStringLength()));
    std::ostream stream(&buffer);
    ChatMessages messages;
    for (auto& message : request["messages"].GetArray())
        messages.emplace_back(std::string(message["role"].GetString(), message["role"].GetStringLength()), std::string(message["content"].GetString(), message["content"].GetStringLength()));
    auto full=llm->tokenizer_encode(llm->apply_chat_template(messages));
    out.AddMember("input_tokens",static_cast<unsigned>(full.size()),a);
    llm->response(messages,&stream,"",request["max_new_tokens"].GetInt());
    out.AddMember("message_count", static_cast<unsigned>(messages.size()), a);
    str(out,"history_mode","kv_reuse");out.AddMember("full_template_tokens",static_cast<unsigned>(full.size()),a);
    str(out,"event","final");out.AddMember("chunk_count",static_cast<unsigned>(buffer.count()),a);
    out.AddMember("stream_bytes_match",buffer.bytes()==c->generate_str,a);
    out.AddMember("stream_transport_ok",buffer.good(),a);
    str(out,"text",c->generate_str);str(out,"status",statusName(c->status));out.AddMember("status_code",static_cast<int>(c->status),a);out.AddMember("after_all_seq",c->all_seq_len,a);out.AddMember("prompt_tokens",c->prompt_len,a);
    Value tokens(rapidjson::kArrayType);for(int token:c->output_tokens)tokens.PushBack(token,a);out.AddMember("tokens",tokens,a);
    bool ok=(c->status==LlmStatus::NORMAL_FINISHED||c->status==LlmStatus::MAX_TOKENS_FINISHED)&&!c->generate_str.empty()&&buffer.good()&&buffer.bytes()==c->generate_str;out.AddMember("ok",ok,a);return out;
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
    if(argc!=4)return 2;std::setvbuf(stdout,nullptr,_IONBF,0);
    std::unique_ptr<Llm> llm(Llm::createLLM(argv[1]));if(!llm||!llm->load())return 3;
    llm->set_config(R"({"reuse_kv":true,"prompt_cache":true,"async":false,"jinja":{"context":{"enable_thinking":false}}})");
    if(std::string(argv[2])=="--cold") { std::ifstream f(argv[3]);std::ostringstream s;s<<f.rdbuf();Document request;request.Parse<rapidjson::kParseValidateEncodingFlag>(s.str().c_str());if(request.HasParseError()||!valid(request))return 2;Document rejected;if(!budget(llm.get(),request,rejected)){std::printf("%s\n",encode(rejected).c_str());return 8;}Document out=infer(llm.get(),request);std::printf("%s\n",encode(out).c_str());return out["ok"].GetBool()?0:4; }
    if(std::string(argv[2])!="--serve")return 2;std::string token=argv[3];if(token.size()!=32)return 2;
    int listener=socket(AF_INET,SOCK_STREAM,0);sockaddr_in addr={};addr.sin_family=AF_INET;addr.sin_addr.s_addr=htonl(INADDR_LOOPBACK);addr.sin_port=0;
    if(listener<0||bind(listener,reinterpret_cast<sockaddr*>(&addr),sizeof(addr))||listen(listener,1))return 5;
    socklen_t length=sizeof(addr);if(getsockname(listener,reinterpret_cast<sockaddr*>(&addr),&length))return 5;
    Document ready;ready.SetObject();auto& a=ready.GetAllocator();str(ready,"event","ready");str(ready,"bind","127.0.0.1");ready.AddMember("pid",getpid(),a);ready.AddMember("port",ntohs(addr.sin_port),a);ready.AddMember("load_count",1,a);std::printf("%s\n",encode(ready).c_str());
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
