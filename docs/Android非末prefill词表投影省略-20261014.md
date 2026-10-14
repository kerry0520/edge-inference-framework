# 非末prefill块为什么可以省词表投影

这课希望你理解一个执行计划优化：模型不是每次都必须计算所有输出；但有状态的Attention仍必须正确推进。

一次普通自回归prefill要把整段输入经过Transformer，写入各层KV。首次采样使用最后输入位置的词表分数。如果输入分块，非最后块仍要计算主干和KV，却不必为本次采样提供词表输出。

本轮固定输入是93个token，分成32、32、29，再追加两个共同的decode token。两条路线如下：

|步骤|基线|候选|两侧每层KV长度|
|---|---|---|---|
|第一个32-token块|完整模型，head调用1次|主干计划，head调用0次|32|
|第二个32-token块|完整模型，head调用1次|主干计划，head调用0次|64|
|最后29-token块|完整模型，head调用1次|完整模型，head调用1次|93|
|第一个固定decode输入|完整模型|完整模型|94|
|第二个固定decode输入|完整模型|完整模型|95|

主干输出选真实图的hidden_states，它依赖全部28个Attention，却不依赖后面的词表投影。主干产生真实hidden值并等待执行完成；没有用假logits占位，也没有跳过Transformer。

MNN中两套Module的KV并不会自动共享。隔离适配在普通Attention的base clone路径显式复用原manager，并检查meta指针；完整Module必须比主干和它的clones活得久，因为cache manager仍使用原backend。运行时逐层核对缓存身份、长度、position与meta，而不是只看clone返回true。

本轮完整输入步骤里，KV保持连续；候选第一次不用head，后续完整计划仍能读取正确历史。这就是为什么图输出选择与状态所有权必须一起验证。

最终结果：最后prefill以及两次decode，各比较完整151936个float32词表分数；三对共455808个对应值全部有限、逐字节一致，误差为零。候选从五次head调用减到三次。独立原始记录审核和root复核通过，错误KV/数值记录被离线检查器拒绝。

这证明当前固定forward原型在这组输入、CPU配置和两个共同decode输入下正确。尚未接入正式response，没有自由生成、多轮、64整除末块、其他padding/设备或速度收益结论。原型使用相同的无mmap加载配置做数值对照，不能拿它与原正式mmap运行的耗时直接比较。减少两次head调用也不等于完整模型提速40%。

下一步应先补最后完整块与多轮边界，再让正式聊天准确识别非末prefill块，随后在无诊断模式下测整个请求。你已有nano-vLLM基础，prefill/KV原理可以快速衔接；这里新增的是MNN Module计划、Attention执行资源与实际调用验证。

工程原件与续做范围见[本阶段续做](../results/android/prefill-head-elision-20261013/阶段续做.md)。本课已讲解，理解待反馈。代码与阶段说明已发布到[项目GitHub](https://github.com/kerry0520/edge-inference-framework)，不包含模型权重或本地编译产物。
