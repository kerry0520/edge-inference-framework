#ifndef PREFILL_RESPONSE_ELISION_HPP
#define PREFILL_RESPONSE_ELISION_HPP
#include <MNN/MNNForwardType.h>
#include <cstdint>
#include <MNN/expr/Module.hpp>
#include <memory>
#include <functional>
#ifndef PREFILL_REUSE_DIAGNOSTIC
#define PREFILL_REUSE_DIAGNOSTIC 0
#endif
#include <chrono>
#include <cstdio>
#include <fstream>
#include <map>
#include <mutex>
#include <set>
#include <string>
namespace ResponseElision {
inline std::string quote(const std::string& v) { std::string r="\""; for (unsigned char c:v) { if(c=='\\'||c=='\"'){r+='\\';r+=c;} else if(c<32){char b[7];std::snprintf(b,7,"\\u%04x",c);r+=b;}else r+=c;} return r+'\"'; }
inline uint64_t fingerprint(const std::string& path) {std::ifstream f(path,std::ios::binary); if(!f)return 0;uint64_t h=1469598103934665603ULL;char b[8192];while(f){f.read(b,sizeof(b));for(std::streamsize i=0;i<f.gcount();++i){h^=static_cast<unsigned char>(b[i]);h*=1099511628211ULL;}}return f.bad()?0:h;}
struct Metrics {bool requested=false,eligible=false,prepared=false,cache_hit=false;int trunk=0,full=0,decode=0;int64_t route_check_us=0,prepare_us=0,release_us=0;void(*prepared_callback)()=nullptr;};
inline Metrics& metrics(){static thread_local Metrics m;return m;}
inline int64_t now(){return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
inline void resetMetrics(bool requested){metrics()=Metrics();metrics().requested=requested;}

struct Provenance {const void* base=nullptr;const void* runtime=nullptr;const void* meta=nullptr;std::string model,weight,key;uint64_t graph=0,epoch=0;std::shared_ptr<void> executorStrong,metaStrong,runtimeStrong,baseStrong;std::shared_ptr<MNN::Express::Module> trunk;};
inline std::map<const void*,Provenance>& owners(){static std::map<const void*,Provenance> m;return m;}
inline std::mutex& mutex(){static std::mutex m;return m;}
inline bool& lifecycle(){static thread_local bool gate=false;return gate;}
inline void event(const char* kind,const void* owner,const Provenance& p,const char* reason){
#if PREFILL_REUSE_DIAGNOSTIC
 if(lifecycle())std::printf("{\"event\":\"reuse_event\",\"kind\":%s,\"reason\":%s,\"owner\":%llu,\"base\":%llu,\"runtime\":%llu,\"meta\":%llu,\"trunk\":%llu,\"epoch\":%llu}\n",quote(kind).c_str(),quote(reason).c_str(),(unsigned long long)reinterpret_cast<uintptr_t>(owner),(unsigned long long)reinterpret_cast<uintptr_t>(p.base),(unsigned long long)reinterpret_cast<uintptr_t>(p.runtime),(unsigned long long)reinterpret_cast<uintptr_t>(p.meta),(unsigned long long)reinterpret_cast<uintptr_t>(p.trunk.get()),(unsigned long long)p.epoch);
#endif
}
inline void release(const void* owner,Provenance& old,const char* reason){if(old.trunk){event("evict",owner,old,reason);old.trunk.reset();event("released",owner,old,reason);}old.baseStrong.reset();old.runtimeStrong.reset();old.metaStrong.reset();old.executorStrong.reset();}
inline void evict(const void* owner,const char* reason){const auto t=now();Provenance old;{std::lock_guard<std::mutex> l(mutex());auto i=owners().find(owner);if(i!=owners().end()){old.base=i->second.base;old.runtime=i->second.runtime;old.meta=i->second.meta;old.epoch=i->second.epoch;old.trunk=std::move(i->second.trunk);old.baseStrong=std::move(i->second.baseStrong);old.runtimeStrong=std::move(i->second.runtimeStrong);old.metaStrong=std::move(i->second.metaStrong);old.executorStrong=std::move(i->second.executorStrong);}}release(owner,old,reason);metrics().release_us+=now()-t;}
inline void erase(const void* owner){const auto t=now();Provenance old;{std::lock_guard<std::mutex> l(mutex());auto i=owners().find(owner);if(i!=owners().end()){old=std::move(i->second);owners().erase(i);}}release(owner,old,"owner_release");metrics().release_us+=now()-t;}
inline void bind(const void* owner,const void* base,const void* runtime,const void* meta,const std::string& model,const std::string& weight,const std::string& key){erase(owner);Provenance p;p.base=base;p.runtime=runtime;p.meta=meta;p.model=model;p.weight=weight;p.key=key;p.graph=fingerprint(model);{std::lock_guard<std::mutex> l(mutex());static uint64_t epoch=0;p.epoch=++epoch;owners()[owner]=p;}event("bind",owner,p,"loaded");}
inline bool bound(const void* owner,const void* base,const void* runtime,const void* meta,const std::string& model,const std::string& weight,const std::string& key){Provenance p;{std::lock_guard<std::mutex> l(mutex());auto i=owners().find(owner);if(i==owners().end())return false;p=i->second;}return p.base==base&&p.runtime==runtime&&p.meta==meta&&p.model==model&&p.weight==weight&&p.key==key&&p.graph&&p.graph==fingerprint(model);}
inline std::shared_ptr<MNN::Express::Module> cached(const void* owner){Provenance p;{std::lock_guard<std::mutex> l(mutex());auto i=owners().find(owner);if(i==owners().end())return {};p=i->second;}if(p.trunk)event("hit",owner,p,"binding_validated");return p.trunk;}
inline void install(const void* owner,std::shared_ptr<MNN::Express::Module> module,std::shared_ptr<void> bs,std::shared_ptr<void> rs,std::shared_ptr<void> ms,std::shared_ptr<void> es){Provenance p;{std::lock_guard<std::mutex> l(mutex());auto i=owners().find(owner);if(i==owners().end()||i->second.trunk)return;i->second.trunk=std::move(module);i->second.baseStrong=std::move(bs);i->second.runtimeStrong=std::move(rs);i->second.metaStrong=std::move(ms);i->second.executorStrong=std::move(es);p=i->second;}event("prepare",owner,p,"scope_28_and_meta_stable");}
struct RequestGuard {const void* owner;std::function<bool()> ok;RequestGuard(const void* p,std::function<bool()> f):owner(p),ok(std::move(f)){}~RequestGuard(){if(!ok())evict(owner,"request_not_successful");}};
#if PREFILL_REUSE_DIAGNOSTIC
inline std::map<uintptr_t,std::string>& names(){static std::map<uintptr_t,std::string> n;return n;}
#endif
struct Scope {const void* base;const void* meta;Scope* previous;bool valid=true;std::map<std::string,uintptr_t> managers;std::map<std::string,int> attempts;Scope(const void* b,const void* m);~Scope();bool passed()const {std::set<uintptr_t> ids;for(auto& p:managers)ids.insert(p.second);return valid&&managers.size()==28&&attempts.size()==28&&ids.size()==28;} };
inline Scope*& current(){static thread_local Scope* s=nullptr;return s;}
inline Scope::Scope(const void* b,const void* m):base(b),meta(m),previous(current()){if(previous)valid=false;current()=this;}
inline Scope::~Scope(){current()=previous;}
inline bool cpuType(int type){return type==MNN_FORWARD_CPU||type==MNN_FORWARD_CPU_EXTENSION;}
inline bool authorized(const void* base,int type){auto s=current();return s&&s->valid&&s->base==base&&cpuType(type);}
inline bool cloneAllowed(const void* sourceMeta,const void* targetMeta,int sourceType,int targetType){auto s=current();return !s||(s->valid&&s->meta==sourceMeta&&s->meta==targetMeta&&cpuType(sourceType)&&cpuType(targetType)&&sourceType==targetType);}
inline void attempt(const char* name,bool ok){auto s=current();if(!s)return;auto n=std::string(name?name:"");++s->attempts[n];s->valid=s->valid&&ok&&s->attempts[n]==1;}
inline void clone(const char* n,const void* source,const void* target,const void* sm,const void* tm,const void* sk,const void* tk,int sl,int tl,int shared){

#if PREFILL_REUSE_DIAGNOSTIC
 {std::lock_guard<std::mutex> l(mutex());names()[reinterpret_cast<uintptr_t>(source)]=n?n:"";names()[reinterpret_cast<uintptr_t>(target)]=n?n:"";}
#endif
 auto s=current();if(!s)return;std::string name=n?n:"";s->valid=s->valid&&sm==s->meta&&tm==s->meta&&sk==tk&&sl==tl&&shared<0;
 auto i=s->managers.find(name);if(i!=s->managers.end()&&i->second!=reinterpret_cast<uintptr_t>(tk))s->valid=false;s->managers[name]=reinterpret_cast<uintptr_t>(tk);
#if PREFILL_REUSE_DIAGNOSTIC
 if(lifecycle())std::printf("{\"event\":\"reuse_clone\",\"name\":%s,\"source_manager\":%llu,\"target_manager\":%llu,\"source_meta\":%llu,\"target_meta\":%llu,\"source_kv_length\":%d,\"target_kv_length\":%d}\n",quote(name).c_str(),(unsigned long long)reinterpret_cast<uintptr_t>(sk),(unsigned long long)reinterpret_cast<uintptr_t>(tk),(unsigned long long)reinterpret_cast<uintptr_t>(sm),(unsigned long long)reinterpret_cast<uintptr_t>(tm),sl,tl);
#endif
}

#if PREFILL_REUSE_DIAGNOSTIC
struct Trace {int step=0;bool active=false;std::string role,dir;};
inline Trace& trace(){static thread_local Trace t;return t;}
inline void begin(const char* role,int seq,int context,size_t previous,size_t remove,const void* meta){auto& t=trace();if(t.dir.empty())return;t.active=true;t.role=role;std::printf("{\"event\":\"response_forward_begin\",\"step\":%d,\"role\":%s,\"seq\":%d,\"context\":%d,\"meta_previous\":%zu,\"meta_remove\":%zu,\"meta\":%llu}\n",t.step,quote(t.role).c_str(),seq,context,previous,remove,(unsigned long long)reinterpret_cast<uintptr_t>(meta));}
inline void end(bool ok,size_t previous){auto& t=trace();if(!t.active)return;std::printf("{\"event\":\"response_forward_end\",\"step\":%d,\"role\":%s,\"ok\":%s,\"meta_previous\":%zu}\n",t.step,quote(t.role).c_str(),ok?"true":"false",previous);t.active=false;++t.step;std::fflush(stdout);}
inline void execute(const char* boundary,const void* exe,const void* meta,const void* manager,int length,size_t previous,size_t add,size_t remove,int reserve,int file,int error){auto& t=trace();if(!t.active)return;std::string n;{std::lock_guard<std::mutex> l(mutex());n=names()[reinterpret_cast<uintptr_t>(exe)];}std::printf("{\"event\":\"response_attention\",\"step\":%d,\"boundary\":%s,\"name\":%s,\"execution\":%llu,\"meta\":%llu,\"manager\":%llu,\"kv_length\":%d,\"meta_previous\":%zu,\"meta_add\":%zu,\"meta_remove\":%zu,\"meta_n_reserve\":%d,\"meta_file_flag\":%d,\"error_code\":%d}\n",t.step,quote(boundary).c_str(),quote(n).c_str(),(unsigned long long)reinterpret_cast<uintptr_t>(exe),(unsigned long long)reinterpret_cast<uintptr_t>(meta),(unsigned long long)reinterpret_cast<uintptr_t>(manager),length,previous,add,remove,reserve,file,error);}
inline void positions(const int* p,size_t count){auto& t=trace();if(!t.active)return;std::printf("{\"event\":\"response_positions\",\"step\":%d,\"values\":[",t.step);for(size_t i=0;i<count;++i)std::printf("%s%d",i?",":"",p[i]);std::printf("]}\n");}
inline bool logits(const float* p,size_t count){auto& t=trace();if(t.dir.empty())return true;auto file="output-step"+std::to_string(t.step)+".f32";auto path=t.dir+"/"+file;std::ofstream f(path,std::ios::binary);f.write(reinterpret_cast<const char*>(p),count*sizeof(float));std::printf("{\"event\":\"response_logits\",\"step\":%d,\"count\":%zu,\"file\":%s,\"path\":%s}\n",t.step,count,quote(file).c_str(),quote(path).c_str());return bool(f);}
#endif
}
#endif
