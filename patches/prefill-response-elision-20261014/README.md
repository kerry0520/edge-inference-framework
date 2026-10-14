# 隔离实现补丁与最小复建约束

当前 implementation.patch 绑定 build-v5 冻结快照（编译全6步骤实际0，run-v3正式response独立正确性验收通过）。v4实机 run-v2 回答与数值一致，但候选所有长题 prepared=false，实际没有 trunk；不得把这轮fallback当作优化启用。CPU FP16 的内部 Backend 类型为 MNN_FORWARD_CPU_EXTENSION=13，v4仅允许0导致授权跳过。v5仅允许具名CPU0或CPU_EXTENSION13，并要求sourceType==targetType，仍精确base/meta绑定，增加授权观测。v4补丁和receipt已分别保留implementation-v4.patch、implementation-patch-check-v4.json。不得宣称加速。

补丁含3个修改来源和2个新增文件：transformers/llm/engine/src/llm.cpp、express/module/StaticModule.cpp、source/backend/cpu/CPUAttention.cpp，以及 apps/android/prefill_response_elision.hpp/prefill_response_worker.cpp。CPU/StaticModule来自固定 alibaba/MNN 3.6.1 commit d407447ed56c4121a11ccbd266dc184ca1ead0c2 的本地公开源码；llm.cpp前置版本是本项目guard SHA256 f9ba43aa7ed1ae183fbf31b40ca14570ed55493cbd4a99e88946863148e30347，包含先前项目修复，不能直接应用到未经修改的upstream llm.cpp。源文件原有版权文字保留，上游 Apache-2.0 许可证复制为 MNN-LICENSE.txt；新增 helper/worker 的项目发布许可尚需项目明确，不能仅从上游许可自动推断；公开分发前应由项目整体许可说明覆盖。

最小复建依赖：固定guard archive SHA256 5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf；固定源码/guard llm.cpp及同布局头文件；NDK r30 arm64工具链；与原库一致的build.ninja编译flags（尤其-fno-exceptions/-fno-rtti、FP16和低内存选项）；既有chat-session build.json链接参数；项目scripts/android/build_runtime_dispatch.py中的archive解析器；新build_prefill_response_elision.py。现有builder还使用本机ASCII junction和tools/build目录，不是新机器一键运行入口。运行设备还需Qwen3-0.6B固定模型、权重、tokenizer、CPU配置和fixture，模型不包含在patch中。

复建流程是先核对前置文件SHA，再在隔离副本应用补丁，按既有ninja flags编3对象并替换guard库；其余562对象必须字节相同，worker按新库链接。不得用改过layout的头文件与旧对象混链。原生产入口/库/default不自动改变。

源复现校验在 build/prefill-response-elision-20261014/patch-review-v4 的独立临时Git树完成。git apply --check和apply均实际0，5文件重建SHA与build-v5/source冻结文件逐字节一致，receipt implementation-patch-check.json。使用core.autocrlf=false保留原字节。两个早期校验失败原件保留：v1 nested忽略目录导致apply退出0却不改文件，v2新文件因autocrlf转换导致SHA不符；真正通过判据是5个非零匹配+SHA一致，不能只看apply退出码。

本交付只是源码备份、review及复建约束说明，没有新编译、部署或手机forward。正式请求优化启用已有run-v3独立验收：14请求、59对完整logits字节一致、5trunk/head64→59；两份离线自洽坏副本被拒绝。长期生命周期、reload/故障、不支持配置实机和性能仍未测，默认phone_chat未切库。
