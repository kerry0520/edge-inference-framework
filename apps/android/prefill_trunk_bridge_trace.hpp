#ifndef PREFILL_TRUNK_BRIDGE_TRACE_HPP
#define PREFILL_TRUNK_BRIDGE_TRACE_HPP
#include <cstdint>
#include <cstdio>
#include <map>
#include <mutex>
#include <string>
#include <vector>
namespace PrefillTrunkBridge {
struct Clone {std::string phase,name; uintptr_t src,dst,srcMeta,dstMeta,srcManager,dstManager; int srcLength,dstLength,sharedIndex;};
struct Attempt {std::string name; bool success;};
struct State {bool enabled=false; std::string phase; std::vector<Clone> clones;std::vector<Attempt> attempts; std::mutex mutex;};
inline State& state(){static State value;return value;}
inline bool enabled(){return state().enabled && state().phase=="trunk-load";}
inline void phase(const char* value,bool enabled){state().phase=value;state().enabled=enabled;}
inline void attempt(const char* name,bool success){auto& s=state();std::lock_guard<std::mutex> lock(s.mutex);s.attempts.push_back({name?name:"",success});}
inline void clone(const char* name,const void* src,const void* dst,const void* sm,const void* dm,const void* sk,const void* dk,int sl,int dl,int shared){auto& s=state();if(s.phase.empty())return;std::lock_guard<std::mutex> lock(s.mutex);s.clones.push_back({s.phase,name?name:"",reinterpret_cast<uintptr_t>(src),reinterpret_cast<uintptr_t>(dst),reinterpret_cast<uintptr_t>(sm),reinterpret_cast<uintptr_t>(dm),reinterpret_cast<uintptr_t>(sk),reinterpret_cast<uintptr_t>(dk),sl,dl,shared});}
inline std::string quote(const std::string& v){std::string r="\"";for(unsigned char c:v){if(c=='\\'||c=='\"'){r+='\\';r+=c;}else if(c<32){char b[7];std::snprintf(b,sizeof(b),"\\u%04x",c);r+=b;}else r+=c;}return r+'\"';}
inline int report(const void* meta,bool fullLoaded,bool trunkLoaded){auto& s=state();uintptr_t expected=reinterpret_cast<uintptr_t>(meta);std::map<std::string,uintptr_t> full,trunk;bool valid=fullLoaded&&trunkLoaded;
for(const auto& c:s.clones){std::printf("{\"event\":\"clone\",\"phase\":%s,\"name\":%s,\"source_execution\":%llu,\"target_execution\":%llu,\"source_meta\":%llu,\"target_meta\":%llu,\"source_manager\":%llu,\"target_manager\":%llu,\"source_kv_length\":%d,\"target_kv_length\":%d,\"shared_layer_index\":%d}\n",quote(c.phase).c_str(),quote(c.name).c_str(),(unsigned long long)c.src,(unsigned long long)c.dst,(unsigned long long)c.srcMeta,(unsigned long long)c.dstMeta,(unsigned long long)c.srcManager,(unsigned long long)c.dstManager,c.srcLength,c.dstLength,c.sharedIndex);
valid=valid&&c.srcMeta==expected&&c.dstMeta==expected&&c.srcManager==c.dstManager&&c.srcLength==0&&c.dstLength==0&&c.sharedIndex<0;
auto& m=c.phase=="full-load"?full:trunk;auto it=m.find(c.name);if(it!=m.end()&&it->second!=c.dstManager)valid=false;m[c.name]=c.dstManager;}
std::map<std::string,int> counts;for(const auto& a:s.attempts){++counts[a.name];valid=valid&&a.success;std::printf("{\"event\":\"bridge_attempt\",\"name\":%s,\"success\":%s}\n",quote(a.name).c_str(),a.success?"true":"false");}
valid=valid&&full.size()==28&&trunk.size()==28&&counts.size()==28&&s.attempts.size()==28;std::map<uintptr_t,int> unique;
for(const auto& c:full){auto t=trunk.find(c.first);valid=valid&&t!=trunk.end()&&t->second==c.second&&counts[c.first]==1;++unique[c.second];}
valid=valid&&unique.size()==28;
std::printf("{\"event\":\"summary\",\"full_loaded\":%s,\"trunk_loaded\":%s,\"full_attention_names\":%zu,\"trunk_attention_names\":%zu,\"bridge_attempts\":%zu,\"unique_managers\":%zu,\"model_forward_count\":0,\"expected_meta\":%llu,\"passed\":%s}\n",fullLoaded?"true":"false",trunkLoaded?"true":"false",full.size(),trunk.size(),s.attempts.size(),unique.size(),(unsigned long long)expected,valid?"true":"false");std::fflush(stdout);return valid?0:9;}
}
#endif
