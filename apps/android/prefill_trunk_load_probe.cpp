#include <MNN/expr/Module.hpp>
#include <MNN/expr/Executor.hpp>
#include <MNN/expr/ExecutorScope.hpp>
#include <MNN/Interpreter.hpp>
#include "core/KVMeta.hpp"
#include "prefill_trunk_bridge_trace.hpp"
#include <memory>
#include <string>
int main(int argc,char** argv){
 if(argc!=3)return 2;
 MNN::BackendConfig backend;backend.precision=MNN::BackendConfig::Precision_Low;backend.memory=MNN::BackendConfig::Memory_Low;
 MNN::ScheduleConfig schedule;schedule.type=MNN_FORWARD_CPU;schedule.numThread=4;schedule.backendConfig=&backend;
 auto executor=MNN::Express::Executor::newExecutor(MNN_FORWARD_CPU,backend,4);MNN::Express::ExecutorScope scope(executor);
 auto runtime=std::shared_ptr<MNN::Express::Executor::RuntimeManager>(MNN::Express::Executor::RuntimeManager::createRuntimeManager(schedule),MNN::Express::Executor::RuntimeManager::destroy);
 MNN::KVMeta meta;meta.layer_nums=28;runtime->setHintPtr(MNN::Interpreter::KVCACHE_INFO,&meta);
 runtime->setHint(MNN::Interpreter::INIT_THREAD_NUMBER,4);runtime->setHint(MNN::Interpreter::MEM_ALLOCATOR_TYPE,0);runtime->setHint(MNN::Interpreter::ATTENTION_OPTION,8);runtime->setHint(MNN::Interpreter::DYNAMIC_QUANT_OPTIONS,0);
 runtime->setExternalFile(argv[2]);
 MNN::Express::Module::Config config;config.shapeMutable=true;config.rearrange=true;
 const std::vector<std::string> inputs={"input_ids","attention_mask","position_ids","logits_index"};
 PrefillTrunkBridge::phase("full-load",false);
 std::unique_ptr<MNN::Express::Module> full(MNN::Express::Module::load(inputs,{"logits"},argv[1],runtime,&config));
 if(!full)return PrefillTrunkBridge::report(&meta,false,false);
 config.base=full.get();PrefillTrunkBridge::phase("trunk-load",true);
 std::unique_ptr<MNN::Express::Module> trunk(MNN::Express::Module::load(inputs,{"hidden_states"},argv[1],runtime,&config));
 PrefillTrunkBridge::phase("",false);int status=PrefillTrunkBridge::report(&meta,true,bool(trunk));
 trunk.reset();full.reset();return status;
}
