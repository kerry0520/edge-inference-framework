#ifndef PREFILL_TRUNK_FORWARD_TRACE_HPP
#define PREFILL_TRUNK_FORWARD_TRACE_HPP
#include "prefill_trunk_bridge_trace.hpp"
namespace PrefillTrunkForward {
struct Event {int step; std::string route,boundary,name; uintptr_t execution,meta,manager; int kvLength; size_t previous,add,remove; int reserve,fileFlag,errorCode;};
struct State {bool active=false;int step=-1;std::string route;std::map<uintptr_t,std::string> names;std::vector<Event> events;std::mutex mutex;};
inline State& state(){static State s;return s;}
inline void name(const void* execution,const char* name){auto& s=state();std::lock_guard<std::mutex> lock(s.mutex);s.names[reinterpret_cast<uintptr_t>(execution)]=name?name:"";}
inline void begin(int step,const char* route){auto& s=state();s.step=step;s.route=route;s.events.clear();s.active=true;}
inline void end(){state().active=false;std::fflush(stdout);}
inline void execute(const char* boundary,const void* execution,const void* meta,const void* manager,int kvLength,size_t previous,size_t add,size_t remove,int reserve,int fileFlag,int errorCode){
 auto& s=state();if(!s.active)return;std::lock_guard<std::mutex> lock(s.mutex);auto address=reinterpret_cast<uintptr_t>(execution);auto i=s.names.find(address);std::string n=i==s.names.end()?"":i->second;
 s.events.push_back({s.step,s.route,boundary,n,address,reinterpret_cast<uintptr_t>(meta),reinterpret_cast<uintptr_t>(manager),kvLength,previous,add,remove,reserve,fileFlag,errorCode});
 std::printf("{\"event\":\"attention_execute\",\"step\":%d,\"route\":%s,\"boundary\":%s,\"name\":%s,\"execution\":%llu,\"meta\":%llu,\"manager\":%llu,\"kv_length\":%d,\"meta_previous\":%zu,\"meta_add\":%zu,\"meta_remove\":%zu,\"meta_n_reserve\":%d,\"meta_file_flag\":%d,\"error_code\":%d}\n",s.step,PrefillTrunkBridge::quote(s.route).c_str(),PrefillTrunkBridge::quote(boundary).c_str(),PrefillTrunkBridge::quote(n).c_str(),(unsigned long long)address,(unsigned long long)reinterpret_cast<uintptr_t>(meta),(unsigned long long)reinterpret_cast<uintptr_t>(manager),kvLength,previous,add,remove,reserve,fileFlag,errorCode);
}
inline bool validate(const void* meta,const std::map<std::string,uintptr_t>& managers,int prior,int tokens){
 auto& s=state();if(s.events.size()!=56)return false;std::map<std::string,std::vector<Event>> grouped;for(const auto& e:s.events)grouped[e.name].push_back(e);if(grouped.size()!=28)return false;
 for(const auto& pair:managers){auto i=grouped.find(pair.first);if(i==grouped.end()||i->second.size()!=2)return false;const auto& before=i->second[0];const auto& after=i->second[1];if(before.boundary!="before"||after.boundary!="after"||before.execution!=after.execution||before.errorCode!=-1||after.errorCode!=0||before.kvLength!=prior||after.kvLength!=prior+tokens)return false;
  for(const auto& e:i->second)if(!e.execution||e.meta!=reinterpret_cast<uintptr_t>(meta)||e.manager!=pair.second||e.previous!=static_cast<size_t>(prior)||e.add!=static_cast<size_t>(tokens)||e.remove||e.reserve||e.fileFlag!=0)return false;
 }return true;
}
}
#endif
