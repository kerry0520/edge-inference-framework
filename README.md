# Edge Inference Framework

基于 **MNN 3.6.1 + Android ARM64 CPU** 的端侧 LLM 推理工程：C++ 手机推理 worker、Python 流式聊天客户端、KV 复用、请求统计，以及模型执行计划优化原型。

验证设备为小米 11，模型为 Qwen3-0.6B 的 MNN 量化版本。模型计算在手机进行，电脑负责请求、显示和记录。本项目使用 MNN 作为底层推理引擎，并非从零重写 MNN。

## 当前能力

- 手机驻留模型，电脑连续提问、接收流式回答。
- 完整历史重算与固定 32-token 分块的跨轮 KV 复用。
- 请求预算、完整响应确认、未知结果保护与资源收尾。
- 分开记录 MNN prefill/decode 时间与客户端接收文字时间。
- 实际算子、W4/W8 计算路径和执行准备/并行区域观察。
- 实验原型：非末 prefill 块以 `hidden_states` 为输出，保留全部 Transformer/Attention，省掉词表投影。

**执行计划优化状态（2026-10-15）**：限定CPU配置的主干计划生命周期、完整logits与成本验收完成后，现已接入显式可选聊天入口 `--mode full --stats --prefill-plan-cache`。自然六轮开关配对的输入/回答一致，/new、短输入与预算拒绝保留计划，真实CLI自动发现/流式输出/正常退出通过；默认入口不启用。新入口完整重算消息历史的KV，执行计划另行复用，总部署预算512token。之前四进程组合性能对照33/128/512均值改善6.14%/1.88%/4.33%，128范围重叠且不能归因缓存单项，采样峰值RSS冷/暖增加8.48/9.02MiB；本次入口回归不新增速度结论。

## 主要入口

|目录/入口|职责|
|---|---|
|`apps/android/phone_chat*_worker.cpp`|手机侧 C++ 请求协议、模型调用与流式输出|
|`scripts/android/phone_chat.py`|电脑端统一聊天入口|
|`scripts/android/phone_chat_session.py`、`phone_chat_kv_session.py`|完整重算与 KV 会话|
|`apps/android/prefill_trunk_forward_probe.cpp`|固定输入的主干/完整计划正确性原型|
|`scripts/android/build_prefill_trunk_forward.py`|隔离构建原型，保留原库|
|`scripts/android/recheck_prefill_trunk_forward.py`|离线核对 KV、调用次数与完整 logits|
|`apps/android/prefill_response_worker.cpp`|真实response的隔离候选正确性入口|
|`patches/prefill-response-elision-20261014/`|冻结实现补丁与前置约束|
|`tests/android/`|协议、状态与请求统计测试|
|`docs/`|执行链、量化与计时范围说明|

推理主线：消息 → token IDs → prefill/decode → MNN Module/Session/Pipeline → Execution → CPU kernel → logits/采样 → 流式回答。

## 运行与复现范围

本仓库提供代码和阶段说明，**不包含模型权重、NDK/ADB、MNN 编译产物或手机缓存**。

手机运行需要 Python 3.11、已配对 ADB、Android ARM64 设备、Qwen3-0.6B MNN 模型，以及与当前源码匹配的固定 MNN 构建。已配置环境的入口：

```powershell
python scripts/android/phone_chat.py --stats --mode kv
```

当前脚本保留了已验收设备与资产身份约束，并有历史构建目录依赖；不是任意新电脑/任意手机 clone 后即可运行的一键安装包。通用配置与从零构建入口仍在整理。不要用未匹配库替换固定构建后沿用已有验收结论。

不需要模型或手机的协议/统计测试：

```powershell
python -m unittest discover -s tests/android -p "test_phone_chat_metrics.py"
```

更多运行约束见 `docs/Android项目快速开始.md`。`query` 和观测/原型工具属于实验路径，不是正式聊天默认行为。

## 如何读实验结论

自然回答一致、完整词表 logits 一致、KV 状态一致、调用次数减少和性能改善是不同的验收条件。探针会改变运行开销；并行区域 wall 时间也包含调度和等待，不能称为纯 kernel 时间。

非末 prefill 省略 head 的关键是保留所有层的 KV 更新，同时保证主干与完整计划共享同一份状态；不能简单跳过整个 forward 或只取消读取 logits。31/32/33/64/65及固定ID连续KV边界已通过完整logits/状态审核；正式response的隔离生成流程已验证；跨轮模板复用、默认入口分发和更多配置仍需验证；限定同步CPU下的持久计划生命周期和去逐层观测成本已回归。

## 第三方来源

底层引擎：[alibaba/MNN](https://github.com/alibaba/MNN)，固定 commit `d407447ed56c4121a11ccbd266dc184ca1ead0c2`。MNN 相关适配及补丁保留其来源约束，许可证副本见 `third_party_notices/MNN-LICENSE.txt`。模型需另行获取并遵循其原许可；本仓库不分发模型。

公开数值样例：`python evidence/verify_logits.py`。它只复查完整 logits 的字节与有限性，不替代设备执行/KV 状态审核；说明见 `evidence/README.md`。

部分历史文档链接指向本机原始工程记录；本次公开快照未复制这些个人路径和完整历史日志。

本次执行计划原型的学习说明：[非末 prefill 词表投影省略](docs/Android非末prefill词表投影省略-20261014.md)。

边界阶段：[为什么满块不等于中间块](docs/Android分块prefill边界与KV连续性-20261014.md)，[派生简结与范围](evidence/prefill-boundaries-20261014/README.md)。

正式response阶段：[生成流程怎样传递输出需求](docs/Android生成流程的输出需求-20261014.md)，[简结及未验证范围](evidence/prefill-response-20261014/README.md)。

请求成本阶段：[优化成本与请求用时](docs/Android优化成本与请求用时-20261014.md)，[48条测量及离线复算](evidence/prefill-cost-20261014/README.md)。

执行计划复用阶段：[计划与每题KV的寿命](docs/Android执行计划复用与请求状态-20261015.md)，[56条请求记录及标准库复算](evidence/prefill-trunk-reuse-20261015/README.md)，[冻结补丁](patches/prefill-trunk-reuse-20261015/README.md)。

## 可选主干计划复用聊天

```powershell
python scripts/android/phone_chat.py --mode full --stats --prefill-plan-cache --max-new-tokens 64
```

以上用于已配置设备与固定构建；预算超限可`/new`，消息历史不会自动截断。`/stats`中复用token指KV，计划状态单独显示。详见[使用与原理](docs/Android可选执行计划缓存聊天-20261015.md)及[入口验收简结](evidence/plan-cache-chat-20261015/README.md)。

## 已验运行包的目录搬移

可以在已有验收构建上导出固定运行包，再用`--plan-profile`从新目录启动。固定手机上的六次新旧入口对照一致，独立客户端另一次真实提问正常退出；默认入口不变。不分发模型或原生程序，导出仍需要已验本机构建，尚未完成新电脑的全量源码构建。见[运行包课程](docs/Android运行包与构建依赖-20261015.md)与[验收范围及精简证据](evidence/portable-plan-profile-20261015/README.md)。

## 固定源码完整构建

新增独立配方：固定MNN提交与项目补丁、本地NDK/KleidiAI、全新对象目录，完整重建CPU静态库并链接聊天worker。565实际对象与565归档成员对应，固定手机三组新旧生成回归一致，旧运行入口保持已验版本。新构建目前是显式实验候选；尚未验收新电脑或其他设备，也无新性能声明。

见[构建步骤](configs/android/mnn-source-build/README.md)、[职责讲解](docs/Android从源码到手机程序-20261015.md)和[精简验收](evidence/clean-source-build-20261015/README.md)。源码仓库不分发模型、工具链、静态库或原生程序。

## 源码构建产物接入运行包

此前固定源码构建已接入显式可选source profile，并完成独立正式客户端的三轮手机生成对照。旧运行包保持兼容；默认入口不变。见[使用与职责](docs/Android源码构建运行包-20261016.md)及[精简验收范围](evidence/source-runtime-bundle-20261016/README.md)。仍不声称其他设备或新电脑的全量验收。

## 启动前检查与已有会话验收

默认离线检查运行包和客户端；显式--device才检查固定手机及必要模型文件。另提供已有full-plan会话的离线一致性复核，准备成功与实际请求成功分别报告。见[使用与原理](docs/Android启动前检查与推理验收-20261016.md)、[阶段范围](evidence/runtime-readiness-20261016/README.md)及[项目总览](docs/项目当前成果与下一步.md)。

## 统一场景、证据范围与演示

新增离线计划及显式运行的生命周期验收驱动，覆盖预算拒绝后继续、new与短长路径切换；**新源码运行包已完成固定手机的五成功、一预算拒绝及new/短长切换，独立原件复核通过**。已有真实旧程序拒绝记录复核通过，不当作新source程序实测。见[请求状态](docs/Android请求拒绝与状态提交-20261016.md)、[演示主线](docs/项目演示与主线讲解-20261016.md)、[九场景范围](docs/Android正式验收范围与证据索引-20261016.md)和[阶段边界](evidence/acceptance-integration-20261016/README.md)。

本轮生命周期实际驱动/native/launcher退出0，53forward，详见[新运行包实机场景](evidence/acceptance-integration-20261016/README.md)。没有新增完整分数、速度或RSS结论。

当前生产程序已补[同版本开关对照](docs/Android同版本执行计划对照-20261016.md)：24请求生成与路由回归通过，16暖样本仅作限定计时观测，短负控漂移，不承诺稳定提速。见[派生证据](evidence/source-plan-abba-20261016/README.md)。

已验源构建输入可通过显式记录根与资产映射搬移，固定身份门槛保留；同机隔离解析通过，不代表新电脑构建或新程序已验。见[用法与职责](docs/Android构建身份与文件位置-20261016.md)及[限定简结](evidence/accepted-input-relocation-20261016/README.md)。

新增[待验候选交接](docs/Android新构建的待验候选-20261016.md)：仅绑定文件，不认证编译或运行，不可当正式profile。见[范围与派生简结](evidence/source-candidate-handoff-20261016/README.md)。

新增[候选怎样进入手机验证](docs/Android待验候选怎样进入手机-20261016.md)，见[有限验证范围](evidence/candidate-device-validation-20261016/README.md)。

新增[新程序与源码的对应关系](docs/Android新程序如何证明来自源码-20261016.md)，见[新构建到手机的范围](evidence/fresh-candidate-20261016/README.md)。
