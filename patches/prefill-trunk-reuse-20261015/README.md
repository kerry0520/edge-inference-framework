# 同实例主干计划复用：冻结补丁

来源：alibaba/MNN 3.6.1，commit d407447ed56c4121a11ccbd266dc184ca1ead0c2，MNN来源文件遵循Apache-2.0，许可证见仓库third_party_notices/MNN-LICENSE.txt。

本补丁直接从项目guard前置源码重建最终v3，不是在旧response补丁上叠加。llm.cpp前置SHA256为f9ba43aa7ed1ae183fbf31b40ca14570ed55493cbd4a99e88946863148e30347，包含项目已有guard，不能声称适用于任意原生upstream。另两文件前置及五份最终源SHA在source-bindings.json。源重建已在独立Git目录check/apply实际0并逐字节核对，不改变现场源码或重新运行模型。

补丁包含llm.cpp、StaticModule.cpp、CPUAttention.cpp、helper与worker，共五份源；Llm类布局不变。编译时PREFILL_REUSE_DIAGNOSTIC=1启用诊断，0用于去观测性能。两种构建源文件相同，只编译定义不同。源码依赖固定MNN/guard/archive环境，公开仓库不分发模型、NDK或已编译库；历史构建脚本尚有本机路径/资产约束，非通用一键安装。

范围仅ordinary同步CPU FP16 lowmem/Qwen3-0.6B/chunk32/独立问题reset/no mmap/no prompt或reuse缓存。registry按Llm实例保存trunk及其依赖，校验base/runtime/meta/配置/图fingerprint/weight路径，配置失配或load/destructor时失效；默认off只登记raw绑定不强持旧模型。图内容fingerprint不是密码学真实性保证，加载期间权重内容按不变处理，weight仅path检查。

24次诊断请求/99forward，51对151936完整logits有限字节一致，涵盖reset命中/off-on/config失效/reload恢复/双实例隔离/带cache析构与直接成功reload。失败load在配置先清cache后发生，不能称带cache失败load已测。released仅指registry引用释放，非物理Module析构证明；同实例并发、异步、多模态、LoRA、其他后端及任意模型均未验收。

性能为固定四进程B,C,C,B的组合对照，B完整模型 vs C省略非末块输出头＋常驻主干计划；不单独归因缓存。完整提取数据与标准库离线复算见evidence/prefill-trunk-reuse-20261015。默认phone_chat入口尚未切库，不能依据有限百分比无条件默认启用。
