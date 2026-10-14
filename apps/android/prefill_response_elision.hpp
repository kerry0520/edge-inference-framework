#ifndef PREFILL_RESPONSE_ELISION_HPP
#define PREFILL_RESPONSE_ELISION_HPP
#include <MNN/MNNForwardType.h>
#include <cstdint>
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
inline bool authorized(const void* base,int backendType){auto s=current();bool ok=s&&s->valid&&s->base==base&&cpuType(backendType);if(s)std::printf("{\"event\":\"response_authorization\",\"expected_base\":%llu,\"actual_base\":%llu,\"backend_type\":%d,\"passed\":%s,\"reason\":%s}\n",(unsigned long long)reinterpret_cast<uintptr_t>(s->base),(unsigned long long)reinterpret_cast<uintptr_t>(base),backendType,ok?"true":"false",quote(!s->valid?"invalid_scope":s->base!=base?"base_mismatch":!cpuType(backendType)?"non_cpu_backend":"authorized").c_str());return ok;}
inline bool cloneAllowed(const void* sourceMeta,const void* targetMeta,int sourceType,int targetType){auto s=current();return !s||(s->valid&&s->meta==sourceMeta&&s->meta==targetMeta&&cpuType(sourceType)&&cpuType(targetType)&&sourceType==targetType);}
inline void attempt(const char* name,bool ok){auto s=current();if(!s)return;auto n=std::string(name?name:"");++s->attempts[n];s->valid=s->valid&&ok&&s->attempts[n]==1;}
inline std::map<uintptr_t,std::string>& names(){static std::map<uintptr_t,std::string> n;return n;}
inline void clone(const char* n,const void* source,const void* target,const void* sm,const void* tm,const void* sk,const void* tk,int sl,int tl,int shared){
 {std::lock_guard<std::mutex> l(mutex());names()[reinterpret_cast<uintptr_t>(source)]=n?n:"";names()[reinterpret_cast<uintptr_t>(target)]=n?n:"";}
 auto s=current();if(!s)return;std::string name=n?n:"";s->valid=s->valid&&sm==s->meta&&tm==s->meta&&sk==tk&&sl==tl&&shared<0;
 auto i=s->managers.find(name);if(i!=s->managers.end()&&i->second!=reinterpret_cast<uintptr_t>(tk))s->valid=false;s->managers[name]=reinterpret_cast<uintptr_t>(tk);
 std::printf("{\"event\":\"response_clone\",\"name\":%s,\"source_execution\":%llu,\"target_execution\":%llu,\"meta\":%llu,\"source_manager\":%llu,\"target_manager\":%llu,\"source_kv_length\":%d,\"target_kv_length\":%d,\"shared_layer_index\":%d}\n",quote(name).c_str(),(unsigned long long)reinterpret_cast<uintptr_t>(source),(unsigned long long)reinterpret_cast<uintptr_t>(target),(unsigned long long)reinterpret_cast<uintptr_t>(sm),(unsigned long long)reinterpret_cast<uintptr_t>(sk),(unsigned long long)reinterpret_cast<uintptr_t>(tk),sl,tl,shared);
}
struct Trace {int step=0;bool active=false;std::string role,dir;};
inline Trace& trace(){static thread_local Trace t;return t;}
inline void begin(const char* role,int seq,int context,size_t previous,size_t remove,const void* meta){auto& t=trace();if(t.dir.empty())return;t.active=true;t.role=role;std::printf("{\"event\":\"response_forward_begin\",\"step\":%d,\"role\":%s,\"seq\":%d,\"context\":%d,\"meta_previous\":%zu,\"meta_remove\":%zu,\"meta\":%llu}\n",t.step,quote(t.role).c_str(),seq,context,previous,remove,(unsigned long long)reinterpret_cast<uintptr_t>(meta));}
inline void end(bool ok,size_t previous){auto& t=trace();if(!t.active)return;std::printf("{\"event\":\"response_forward_end\",\"step\":%d,\"role\":%s,\"ok\":%s,\"meta_previous\":%zu}\n",t.step,quote(t.role).c_str(),ok?"true":"false",previous);t.active=false;++t.step;std::fflush(stdout);}
inline void execute(const char* boundary,const void* exe,const void* meta,const void* manager,int length,size_t previous,size_t add,size_t remove,int reserve,int file,int error){auto& t=trace();if(!t.active)return;std::string n;{std::lock_guard<std::mutex> l(mutex());n=names()[reinterpret_cast<uintptr_t>(exe)];}std::printf("{\"event\":\"response_attention\",\"step\":%d,\"boundary\":%s,\"name\":%s,\"execution\":%llu,\"meta\":%llu,\"manager\":%llu,\"kv_length\":%d,\"meta_previous\":%zu,\"meta_add\":%zu,\"meta_remove\":%zu,\"meta_n_reserve\":%d,\"meta_file_flag\":%d,\"error_code\":%d}\n",t.step,quote(boundary).c_str(),quote(n).c_str(),(unsigned long long)reinterpret_cast<uintptr_t>(exe),(unsigned long long)reinterpret_cast<uintptr_t>(meta),(unsigned long long)reinterpret_cast<uintptr_t>(manager),length,previous,add,remove,reserve,file,error);}
inline void positions(const int* p,size_t count){auto& t=trace();if(!t.active)return;std::printf("{\"event\":\"response_positions\",\"step\":%d,\"values\":[",t.step);for(size_t i=0;i<count;++i)std::printf("%s%d",i?",":"",p[i]);std::printf("]}\n");}
inline bool logits(const float* p,size_t count){auto& t=trace();if(t.dir.empty())return true;auto file="output-step"+std::to_string(t.step)+".f32";auto path=t.dir+"/"+file;std::ofstream f(path,std::ios::binary);f.write(reinterpret_cast<const char*>(p),count*sizeof(float));std::printf("{\"event\":\"response_logits\",\"step\":%d,\"count\":%zu,\"file\":%s,\"path\":%s}\n",t.step,count,quote(file).c_str(),quote(path).c_str());return bool(f);}
}
#endif
