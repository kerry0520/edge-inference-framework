// Isolated diagnostic hook only. Does not alter any MNN class layout.
#ifndef MNN_RUNTIME_DISPATCH_TRACE_HPP
#define MNN_RUNTIME_DISPATCH_TRACE_HPP
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <vector>
#include <utility>
#include <dlfcn.h>
namespace MNNRuntimeDispatch {
struct Context { const char* op; const char* execution; int plane; };
struct Registration { const char* name; unsigned refs; };
struct Record { Context context; std::string event,site; uintptr_t offset; bool dladdrOk,sameElf; uint64_t count; };
inline void write(const Record& r, const char* suffix) {
    std::fprintf(stderr,"MNN_RUNTIME_DISPATCH {\"event\":\"%s_%s\",\"op\":\"%s\",\"execution\":\"%s\",\"plane\":%d,\"site\":\"%s\",\"function_offset\":%llu,\"dladdr_ok\":%s,\"same_elf\":%s,\"count\":%llu}\n",
        r.event.c_str(),suffix,r.context.op,r.context.execution,r.context.plane,r.site.c_str(),
        (unsigned long long)r.offset,r.dladdrOk?"true":"false",r.sameElf?"true":"false",(unsigned long long)r.count);
}
struct State {
    std::mutex mutex;
    std::map<const void*,Registration> targets;
    std::vector<Record> records;
    uint64_t dropped=0;
    ~State() {
        for (const auto& r:records) write(r,"summary");
        std::fprintf(stderr,"MNN_RUNTIME_DISPATCH {\"event\":\"trace_totals\",\"unique_records\":%llu,\"dropped\":%llu,\"registrations_remaining\":%llu}\n",(unsigned long long)records.size(),(unsigned long long)dropped,(unsigned long long)targets.size());
        std::fflush(stderr);
    }
};
inline State& state() { static State value; return value; }
inline const char* target(const char* name) {
    const char* q="/layers.0/self_attn/q_proj/Linear";
    const char* h="/lm/lm_head/Linear";
    if(name && std::strcmp(name,q)==0) return q;
    if(name && std::strcmp(name,h)==0) return h;
    return nullptr;
}
inline void attach(const void* common,const char* name) {
    const char* t=target(name); if(!t) return;
    auto& s=state(); std::lock_guard<std::mutex> guard(s.mutex);
    auto it=s.targets.find(common);
    if(it==s.targets.end()) s.targets.emplace(common,Registration{t,1});
    else if(std::strcmp(it->second.name,t)==0) ++it->second.refs;
    else ++s.dropped; // Unexpected pointer alias is observable and rejects acceptance.
}
inline void detach(const void* common) {
    auto& s=state(); std::lock_guard<std::mutex> guard(s.mutex);
    auto it=s.targets.find(common); if(it==s.targets.end()) return;
    if(--it->second.refs==0) s.targets.erase(it);
}
inline Context context(const void* common,const char* execution,int plane) {
    auto& s=state(); std::lock_guard<std::mutex> guard(s.mutex);
    auto it=s.targets.find(common);
    return Context{it==s.targets.end()?nullptr:it->second.name,execution,plane};
}
inline Context& tls() { static thread_local Context value{nullptr,nullptr,0}; return value; }
struct Scope { Context old; explicit Scope(Context now):old(tls()) {tls()=now;} ~Scope(){tls()=old;} };
inline void record(Context c,const char* event,const char* site,const void* fn) {
    if(!c.op) return;
    Dl_info info{}; bool resolved=fn && dladdr(fn,&info)!=0 && info.dli_fbase;
    Dl_info anchor{}; bool same=resolved && dladdr(reinterpret_cast<const void*>(&write),&anchor)!=0 && info.dli_fbase==anchor.dli_fbase;
    uintptr_t offset=resolved?reinterpret_cast<uintptr_t>(fn)-reinterpret_cast<uintptr_t>(info.dli_fbase):0;
    auto& s=state(); std::lock_guard<std::mutex> guard(s.mutex);
    for(auto& r:s.records) {
        if(r.context.op==c.op && std::strcmp(r.context.execution,c.execution)==0 && r.context.plane==c.plane && r.event==event && r.site==site && r.offset==offset && r.dladdrOk==resolved && r.sameElf==same) {++r.count;return;}
    }
    if(s.records.size()>=128) {++s.dropped;return;}
    s.records.push_back(Record{c,event,site,offset,resolved,same,1});
    write(s.records.back(),"first");
}
template<class Fn> struct Kernel {
    Context c; Fn fn; const char* site;
    template<class... Args> void operator()(Args&&... args) const {
        fn(std::forward<Args>(args)...);
        record(c,"kernel_return",site,reinterpret_cast<const void*>(fn));
    }
};
template<class Fn> Kernel<Fn> kernel(Context c,Fn fn,const char* site) {return Kernel<Fn>{c,fn,site};}
}
#endif
