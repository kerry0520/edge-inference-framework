// Isolated metadata observer. No tensor data values and no MNN class-layout changes.
#ifndef MNN_W4_EXECUTION_CONTRACT_HPP
#define MNN_W4_EXECUTION_CONTRACT_HPP
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <mutex>
#include <map>
#include <string>
#include <vector>
#include <sstream>
#include <atomic>
#include <dlfcn.h>
namespace MNNW4Contract {
struct Registration { const char* op; unsigned refs; };
struct State { std::mutex mutex; std::map<const void*,Registration> targets; std::string request; uint64_t ordinal=0,errors=0; ~State(){std::fprintf(stderr,"MNN_W4_CONTRACT {\"event\":\"totals\",\"errors\":%llu,\"registrations_remaining\":%llu,\"request_open\":%s}\n",(unsigned long long)errors,(unsigned long long)targets.size(),request.empty()?"false":"true");} };
inline State& state(){static State s;return s;}
inline std::string hex(const std::string& s){std::string out;for(unsigned char c:s){out+="0123456789abcdef"[c>>4];out+="0123456789abcdef"[c&15];}return out;}
inline void begin(const std::string& id){auto& s=state();std::lock_guard<std::mutex> lock(s.mutex);if(!s.request.empty())++s.errors;s.request=hex(id);s.ordinal=0;std::fprintf(stderr,"MNN_W4_CONTRACT {\"event\":\"request_begin\",\"id_hex\":\"%s\"}\n",s.request.c_str());}
inline void end(){auto& s=state();std::lock_guard<std::mutex> lock(s.mutex);std::fprintf(stderr,"MNN_W4_CONTRACT {\"event\":\"request_end\",\"id_hex\":\"%s\",\"executions\":%llu}\n",s.request.c_str(),(unsigned long long)s.ordinal);s.request.clear();}
inline void attach(const void* common,const char* name){const char* target=nullptr;for(auto t:{"/layers.0/self_attn/q_proj/Linear","/lm/lm_head/Linear"})if(name&&std::strcmp(name,t)==0)target=t;if(!target)return;auto& s=state();std::lock_guard<std::mutex> lock(s.mutex);auto it=s.targets.find(common);if(it==s.targets.end())s.targets.emplace(common,Registration{target,1});else if(std::strcmp(it->second.op,target)==0)++it->second.refs;else ++s.errors;}
inline void detach(const void* common){auto& s=state();std::lock_guard<std::mutex> lock(s.mutex);auto it=s.targets.find(common);if(it!=s.targets.end()&&--it->second.refs==0)s.targets.erase(it);}
struct Call { const char* kind; std::vector<int64_t> args; uint64_t count; };
struct Slot {std::vector<Call> calls;uint64_t dropped=0;};
struct Frame {
 const char* op=nullptr;std::string id;uint64_t ordinal=0;std::vector<Slot> slots;std::string metadata;std::atomic<uint64_t> invalid{0};
 explicit Frame(const void* common,int threads){auto& s=state();std::lock_guard<std::mutex> lock(s.mutex);auto it=s.targets.find(common);if(it==s.targets.end())return;op=it->second.op;id=s.request;ordinal=++s.ordinal;if(id.empty())++s.errors;if(threads<1||threads>64){++s.errors;op=nullptr;return;}slots.resize(threads);}
 void add(int tid,const char* kind,std::initializer_list<int64_t> args){if(!op)return;if(tid<0||size_t(tid)>=slots.size()){++invalid;return;}auto& slot=slots[tid];std::vector<int64_t> key(args);for(auto& c:slot.calls)if(std::strcmp(c.kind,kind)==0&&c.args==key){++c.count;return;}if(slot.calls.size()>=256){++slot.dropped;return;}slot.calls.push_back(Call{kind,std::move(key),1});}
 template<class T> void shape(std::ostringstream& out,T* t){out<<'[';for(int i=0;i<t->dimensions();++i){if(i)out<<',';out<<t->length(i);}out<<']';}
 template<class T> void set(T* in,T* out,std::initializer_list<int64_t> contract,const std::vector<int>& divides){if(!op)return;std::ostringstream o;o<<"\"input_shape\":";shape(o,in);o<<",\"output_shape\":";shape(o,out);o<<",\"contract\":[";bool first=true;for(auto n:contract){if(!first)o<<',';first=false;o<<n;}o<<"],\"divides\":[";first=true;for(auto n:divides){if(!first)o<<',';first=false;o<<n;}o<<']';metadata=o.str();}
 template<class F> void gemm(int tid,const char* kind,F fn,std::initializer_list<int64_t> args){if(!op)return;Dl_info info{},anchor{};bool ok=dladdr(reinterpret_cast<const void*>(fn),&info)!=0&&info.dli_fbase;bool same=ok&&dladdr(reinterpret_cast<const void*>(&state),&anchor)!=0&&anchor.dli_fbase==info.dli_fbase;std::vector<int64_t> a(args);a.push_back(ok?reinterpret_cast<uintptr_t>(fn)-reinterpret_cast<uintptr_t>(info.dli_fbase):0);a.push_back(ok);a.push_back(same);if(tid<0||size_t(tid)>=slots.size()){++invalid;return;}auto& slot=slots[tid];for(auto& c:slot.calls)if(std::strcmp(c.kind,kind)==0&&c.args==a){++c.count;return;}if(slot.calls.size()>=256){++slot.dropped;return;}slot.calls.push_back(Call{kind,std::move(a),1});}
 ~Frame(){if(!op)return;std::ostringstream o;o<<"MNN_W4_CONTRACT {\"event\":\"execution_return\",\"id_hex\":\""<<id<<"\",\"ordinal\":"<<ordinal<<",\"op\":\""<<op<<"\",\"phase\":\"unknown\","<<metadata<<",\"invalid_slots\":"<<invalid.load()<<",\"threads\":[";for(size_t t=0;t<slots.size();++t){if(t)o<<',';auto& slot=slots[t];o<<"{\"tid\":"<<t<<",\"dropped\":"<<slot.dropped<<",\"calls\":[";for(size_t j=0;j<slot.calls.size();++j){if(j)o<<',';auto& c=slot.calls[j];o<<"{\"kind\":\""<<c.kind<<"\",\"count\":"<<c.count<<",\"args\":[";for(size_t k=0;k<c.args.size();++k){if(k)o<<',';o<<c.args[k];}o<<"]}";}o<<"]}";}o<<"]}\n";std::fputs(o.str().c_str(),stderr);std::fflush(stderr);}
};
}
#endif
