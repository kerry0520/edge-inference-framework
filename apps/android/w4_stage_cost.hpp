// Isolated four-boundary wall-clock observer. No MNN class-layout changes.
#ifndef MNN_W4_STAGE_COST_HPP
#define MNN_W4_STAGE_COST_HPP
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <mutex>
#include <map>
#include <string>
#include <sstream>
#include <time.h>
#include <vector>
#include <algorithm>
namespace MNNW4StageCost {
struct Registration {const char* op;unsigned refs;};
struct State {
 std::mutex mutex;std::map<const void*,Registration> targets;std::string request;bool enabled=false;uint64_t ordinal=0,errors=0,clocks=0,failures=0;
 ~State(){std::fprintf(stderr,"MNN_W4_STAGE_COST {\"event\":\"totals\",\"enabled\":%s,\"errors\":%llu,\"hot_clock_calls\":%llu,\"clock_failures\":%llu,\"registrations_remaining\":%llu,\"request_open\":%s}\n",enabled?"true":"false",(unsigned long long)errors,(unsigned long long)clocks,(unsigned long long)failures,(unsigned long long)targets.size(),request.empty()?"false":"true");}
};
inline State& state(){static State s;return s;}
inline std::string hex(const std::string& x){std::string out;for(unsigned char c:x){out+="0123456789abcdef"[c>>4];out+="0123456789abcdef"[c&15];}return out;}
inline void configure(bool enabled){state().enabled=enabled;}
inline void begin(const std::string& id){auto& s=state();std::lock_guard<std::mutex> lock(s.mutex);if(!s.request.empty())++s.errors;s.request=hex(id);s.ordinal=0;s.clocks=0;s.failures=0;std::fprintf(stderr,"MNN_W4_STAGE_COST {\"event\":\"request_begin\",\"id_hex\":\"%s\",\"enabled\":%s}\n",s.request.c_str(),s.enabled?"true":"false");}
inline void end(){auto& s=state();std::lock_guard<std::mutex> lock(s.mutex);std::fprintf(stderr,"MNN_W4_STAGE_COST {\"event\":\"request_end\",\"id_hex\":\"%s\",\"enabled\":%s,\"executions\":%llu,\"hot_clock_calls\":%llu,\"clock_failures\":%llu}\n",s.request.c_str(),s.enabled?"true":"false",(unsigned long long)s.ordinal,(unsigned long long)s.clocks,(unsigned long long)s.failures);s.request.clear();}
inline void attach(const void* common,const char* name){const char* target=nullptr;for(auto t:{"/layers.0/self_attn/q_proj/Linear","/lm/lm_head/Linear"})if(name&&std::strcmp(name,t)==0)target=t;if(!target)return;auto& s=state();std::lock_guard<std::mutex> lock(s.mutex);auto it=s.targets.find(common);if(it==s.targets.end())s.targets.emplace(common,Registration{target,1});else if(std::strcmp(it->second.op,target)==0)++it->second.refs;else ++s.errors;}
inline void detach(const void* common){auto& s=state();std::lock_guard<std::mutex> lock(s.mutex);auto it=s.targets.find(common);if(it!=s.targets.end()&&--it->second.refs==0)s.targets.erase(it);}
struct Frame {
 const char* op=nullptr;std::string id;uint64_t ordinal=0,t[4]={},calls=0,failures=0;int next=0,plane=0,bits=0,threads=0;bool split=false,finished=false;
 explicit Frame(const void* common){auto& s=state();if(!s.enabled)return;{std::lock_guard<std::mutex> lock(s.mutex);auto it=s.targets.find(common);if(it==s.targets.end())return;op=it->second.op;id=s.request;ordinal=++s.ordinal;if(id.empty())++s.errors;}mark(0);}
 void mark(int index){if(!op)return;if(index!=next){++state().errors;return;}timespec stamp{};++calls;++state().clocks;if(clock_gettime(CLOCK_MONOTONIC,&stamp)!=0||stamp.tv_sec<0||stamp.tv_nsec<0||stamp.tv_nsec>=1000000000){++failures;++state().failures;}else t[index]=uint64_t(stamp.tv_sec)*1000000000ULL+uint64_t(stamp.tv_nsec);++next;}
 void set(int p,int b,int n,bool oc){if(!op)return;plane=p;bits=b;threads=n;split=oc;}
 void computeBegin(){mark(1);}
 void computeEnd(){mark(2);}
 void finish(){if(!op)return;if(split)mark(3);else {next=3;mark(3);}finished=true;}
 ~Frame(){if(!op)return;bool totalValid=finished&&failures==0&&t[3]>=t[0];bool supported=split;bool valid=totalValid&&supported&&next==4&&calls==4&&t[0]<=t[1]&&t[1]<=t[2]&&t[2]<=t[3];if(!totalValid||(supported&&!valid))++state().errors;std::ostringstream o;o<<"MNN_W4_STAGE_COST {\"event\":\"execution_return\",\"id_hex\":\""<<id<<"\",\"ordinal\":"<<ordinal<<",\"op\":\""<<op<<"\",\"phase\":\"unknown\",\"plane\":"<<plane<<",\"weight_bits\":"<<bits<<",\"threads\":"<<threads<<",\"split_by_oc\":"<<(split?"true":"false")<<",\"supported\":"<<(supported?"true":"false")<<",\"valid\":"<<(valid?"true":"false")<<",\"total_valid\":"<<(totalValid?"true":"false")<<",\"finished\":"<<(finished?"true":"false")<<",\"clock_calls\":"<<calls<<",\"clock_failures\":"<<failures<<",\"timestamps_ns\":["<<t[0]<<','<<t[1]<<','<<t[2]<<','<<t[3]<<"],\"total_ns\":";if(totalValid)o<<t[3]-t[0];else o<<"null";o<<",\"preparation_ns\":";if(valid)o<<t[1]-t[0];else o<<"null";o<<",\"compute_wall_ns\":";if(valid)o<<t[2]-t[1];else o<<"null";o<<",\"tail_ns\":";if(valid)o<<t[3]-t[2];else o<<"null";o<<"}\n";std::fputs(o.str().c_str(),stderr);std::fflush(stderr);}
};
inline int probe(){std::vector<uint64_t> delta;uint64_t fails=0;for(int i=0;i<10000;++i){timespec a{},b{};int ra=clock_gettime(CLOCK_MONOTONIC,&a),rb=clock_gettime(CLOCK_MONOTONIC,&b);if(ra||rb){++fails;continue;}uint64_t x=uint64_t(a.tv_sec)*1000000000ULL+a.tv_nsec,y=uint64_t(b.tv_sec)*1000000000ULL+b.tv_nsec;if(y<x){++fails;continue;}delta.push_back(y-x);}std::sort(delta.begin(),delta.end());std::printf("{\"probe\":\"consecutive_monotonic_calls\",\"model_requests\":0,\"clock_calls\":20000,\"attempts\":10000,\"accepted_pairs\":%llu,\"failures\":%llu,\"minimum_ns\":%llu,\"median_ns\":%llu,\"p95_ns\":%llu,\"maximum_ns\":%llu,\"subtract_from_timings\":false}\n",(unsigned long long)delta.size(),(unsigned long long)fails,(unsigned long long)(delta.empty()?0:delta.front()),(unsigned long long)(delta.empty()?0:delta[delta.size()/2]),(unsigned long long)(delta.empty()?0:delta[(delta.size()*95)/100]),(unsigned long long)(delta.empty()?0:delta.back()));return fails?1:0;}
}
#endif
