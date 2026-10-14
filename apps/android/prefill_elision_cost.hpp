#ifndef PREFILL_RESPONSE_ELISION_HPP
#define PREFILL_RESPONSE_ELISION_HPP
#include <MNN/MNNForwardType.h>
#include <cstdint>
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
struct Provenance {const void* base;const void* runtime;const void* meta;std::string model,weight;uint64_t graph;};
inline std::map<const void*,Provenance>& owners(){static std::map<const void*,Provenance> m;return m;}
inline std::mutex& mutex(){static std::mutex m;return m;}
inline void erase(const void* owner){std::lock_guard<std::mutex> l(mutex());owners().erase(owner);}
inline void bind(const void* owner,const void* base,const void* runtime,const void* meta,const std::string& model,const std::string& weight){std::lock_guard<std::mutex> l(mutex());owners()[owner]={base,runtime,meta,model,weight,fingerprint(model)};}
inline bool bound(const void* owner,const void* base,const void* runtime,const void* meta,const std::string& model,const std::string& weight){std::lock_guard<std::mutex> l(mutex());auto i=owners().find(owner);return i!=owners().end()&&i->second.base==base&&i->second.runtime==runtime&&i->second.meta==meta&&i->second.model==model&&i->second.weight==weight&&i->second.graph&&i->second.graph==fingerprint(model);}
struct Scope {const void* base;const void* meta;Scope* previous;bool valid=true;std::map<std::string,uintptr_t> managers;std::map<std::string,int> attempts;Scope(const void* b,const void* m);~Scope();bool passed()const {std::set<uintptr_t> ids;for(auto& p:managers)ids.insert(p.second);return valid&&managers.size()==28&&attempts.size()==28&&ids.size()==28;} };
inline Scope*& current(){static thread_local Scope* s=nullptr;return s;}
inline Scope::Scope(const void* b,const void* m):base(b),meta(m),previous(current()){if(previous)valid=false;current()=this;}
inline Scope::~Scope(){current()=previous;}
inline bool cpuType(int type){return type==MNN_FORWARD_CPU||type==MNN_FORWARD_CPU_EXTENSION;}
inline bool authorized(const void* base,int type){auto s=current();return s&&s->valid&&s->base==base&&cpuType(type);}
inline bool cloneAllowed(const void* sourceMeta,const void* targetMeta,int sourceType,int targetType){auto s=current();return !s||(s->valid&&s->meta==sourceMeta&&s->meta==targetMeta&&cpuType(sourceType)&&cpuType(targetType)&&sourceType==targetType);}
inline void attempt(const char* name,bool ok){auto s=current();if(!s)return;auto n=std::string(name?name:"");++s->attempts[n];s->valid=s->valid&&ok&&s->attempts[n]==1;}
inline void clone(const char* n,const void*,const void*,const void* sm,const void* tm,const void* sk,const void* tk,int sl,int tl,int shared){
 auto s=current();if(!s)return;std::string name=n?n:"";s->valid=s->valid&&sm==s->meta&&tm==s->meta&&sk==tk&&sl==tl&&shared<0;
 auto i=s->managers.find(name);if(i!=s->managers.end()&&i->second!=reinterpret_cast<uintptr_t>(tk))s->valid=false;s->managers[name]=reinterpret_cast<uintptr_t>(tk);
}
struct Metrics {bool requested=false,eligible=false,prepared=false;int trunk=0,full=0,decode=0;int64_t route_check_us=0,prepare_us=0,release_us=0;void(*prepared_callback)()=nullptr;};
inline Metrics& metrics(){static thread_local Metrics m;return m;}
inline int64_t now(){return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
inline void resetMetrics(bool requested){metrics()=Metrics();metrics().requested=requested;}
}
#endif
