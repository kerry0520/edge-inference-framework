#include "llm/llm.hpp"
#include "rapidjson/document.h"
#include "rapidjson/writer.h"
#include "rapidjson/stringbuffer.h"
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <memory>
#include <sstream>
#include <string>
using MNN::Transformer::Llm;
using MNN::Transformer::LlmStatus;
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
static Document infer(Llm* llm,const Document& request) {
    Document out;out.SetObject();auto& a=out.GetAllocator();out.AddMember("pid",getpid(),a);out.AddMember("load_count",1,a);
    std::ostringstream pointer;pointer<<llm;str(out,"llm_identity",pointer.str());str(out,"id",request["id"].GetString());
    auto c=llm->getContext();out.AddMember("before_history",static_cast<int>(c->history_tokens.size()),a);out.AddMember("before_all_seq",c->all_seq_len,a);
    llm->reset();out.AddMember("reset_history",static_cast<int>(c->history_tokens.size()),a);out.AddMember("reset_output",static_cast<int>(c->output_tokens.size()),a);out.AddMember("reset_all_seq",c->all_seq_len,a);out.AddMember("reset_gen_seq",c->gen_seq_len,a);
    llm->response(request["prompt"].GetString(),nullptr,"",request["max_new_tokens"].GetInt());
    str(out,"text",c->generate_str);str(out,"status",statusName(c->status));out.AddMember("status_code",static_cast<int>(c->status),a);out.AddMember("after_all_seq",c->all_seq_len,a);out.AddMember("prompt_tokens",c->prompt_len,a);
    Value tokens(rapidjson::kArrayType);for(int token:c->output_tokens)tokens.PushBack(token,a);out.AddMember("tokens",tokens,a);
    bool ok=(c->status==LlmStatus::NORMAL_FINISHED||c->status==LlmStatus::MAX_TOKENS_FINISHED)&&!c->generate_str.empty();out.AddMember("ok",ok,a);return out;
}
static bool valid(const Document& d) {
    if(!d.IsObject()||!d.HasMember("id")||!d["id"].IsString()||!d.HasMember("prompt")||!d["prompt"].IsString()||!d.HasMember("max_new_tokens")||!d["max_new_tokens"].IsInt())return false;
    auto idLength=d["id"].GetStringLength(), promptLength=d["prompt"].GetStringLength();
    return idLength>0&&idLength<=128&&promptLength>0&&promptLength<=65536&&!memchr(d["id"].GetString(),0,idLength)&&!memchr(d["prompt"].GetString(),0,promptLength)&&d["max_new_tokens"].GetInt()>=1&&d["max_new_tokens"].GetInt()<=512;
}
int main(int argc,char** argv) {
    if(argc!=4)return 2;std::setvbuf(stdout,nullptr,_IONBF,0);
    std::unique_ptr<Llm> llm(Llm::createLLM(argv[1]));if(!llm||!llm->load())return 3;
    llm->set_config(R"({"reuse_kv":false,"async":false,"jinja":{"context":{"enable_thinking":false}}})");
    if(std::string(argv[2])=="--cold") { std::ifstream f(argv[3]);std::ostringstream s;s<<f.rdbuf();Document request;request.Parse<rapidjson::kParseValidateEncodingFlag>(s.str().c_str());if(request.HasParseError()||!valid(request))return 2;Document out=infer(llm.get(),request);std::printf("%s\n",encode(out).c_str());return out["ok"].GetBool()?0:4; }
    if(std::string(argv[2])!="--serve")return 2;std::string token=argv[3];if(token.size()!=32)return 2;
    int listener=socket(AF_INET,SOCK_STREAM,0);sockaddr_in addr={};addr.sin_family=AF_INET;addr.sin_addr.s_addr=htonl(INADDR_LOOPBACK);addr.sin_port=0;
    if(listener<0||bind(listener,reinterpret_cast<sockaddr*>(&addr),sizeof(addr))||listen(listener,1))return 5;
    socklen_t length=sizeof(addr);if(getsockname(listener,reinterpret_cast<sockaddr*>(&addr),&length))return 5;
    Document ready;ready.SetObject();auto& a=ready.GetAllocator();str(ready,"event","ready");str(ready,"bind","127.0.0.1");ready.AddMember("pid",getpid(),a);ready.AddMember("port",ntohs(addr.sin_port),a);ready.AddMember("load_count",1,a);std::printf("%s\n",encode(ready).c_str());
    int client=accept(listener,nullptr,nullptr);if(client<0){close(listener);return 5;}int result=0;
    for(;;) { Document request;if(!receive(client,request)){result=6;break;}
        if(!request.HasMember("session_token")||!request["session_token"].IsString()||(request["session_token"].GetStringLength()!=32||memcmp(token.data(),request["session_token"].GetString(),32)!=0)){result=7;break;}
        if(request.HasMember("command")&&request["command"].IsString()&&std::string(request["command"].GetString())=="shutdown") { Document done;done.SetObject();done.AddMember("ok",true,done.GetAllocator());done.AddMember("pid",getpid(),done.GetAllocator());str(done,"event","shutdown");sendJson(client,done);break; }
        if(!valid(request)){result=2;break;}Document out=infer(llm.get(),request);if(!sendJson(client,out)){result=6;break;}if(!out["ok"].GetBool()){result=4;break;}
    }
    close(client);close(listener);std::printf("{\"event\":\"stopped\",\"pid\":%d,\"load_count\":1,\"exit\":%d}\n",getpid(),result);return result;
}
