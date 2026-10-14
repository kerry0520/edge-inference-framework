# 从Execution走到真实计算内核

本课希望你理解：一个模型层为什么能由不同底层实现计算，以及怎样区分“源码允许”与“手机实际使用”。

以Q投影为例，你熟悉的含义是把每个位置的1024维输入变成2048维Q。固定图将它表达为1×1 Convolution。这一层接收张量、输出张量；它没有规定必须用哪条CPU指令。

CPUBackend查找这个图类型对应的Creator。Creator结合权重格式、内存模式和设备能力创建Execution。例如公开源码既有KleidiAI优先路径，也有Dense低内存路径。这是选择实现的入口，尚未计算某个问题的答案。

Execution在onResize中准备形状、缓冲区、权重布局与线程方案。在onExecute中拿到本次输入，做需要的量化和数据整理，调用kernel，再把结果写到输出张量。kernel是其中执行一段乘加的函数，不负责整段聊天、tokenizer或完整模型调度。

可以把职责连起来：

文字 → token ID → Llm组织prefill/decode → Module/Session准备执行图 → Pipeline按命令调用Execution → Execution调用kernel → 输出张量。

一次Q投影可以按位置或输出通道分块，多线程也可以分别调用同一个kernel。因此看到十次kernel调用，不能说生成了十个token。要回到输入shape、分块和调用位置理解它。

W4/W8描述本课已经确认的权重存储格式。入口张量是浮点，也可能在Execution内部临时量化成整数，再做乘累加和尺度恢复。最终实际用了哪一种函数，要看运行时调用证据；图元数据和源码条件不能替代它。

本轮工程方法是隔离复制库并插入观察点，记录目标Execution进入及实际kernel调用返回。原库提供相同问题的对照回答。只有真实退出、回答轨迹一致、目标调用非零和符号归因审核都通过，才能报告观察结果。观察有开销，本轮不能据此宣布提速。

当前状态：构建与采集准备中，实际手机内核待验证。工程原件与恢复位置见[阶段续做](../results/android/runtime-dispatch-20261013/阶段续做.md)。学习状态：已讲解，理解待反馈。

## 本轮已经确认的执行链

| 模型层 | 图声明 | 实际Execution | 实际返回的kernel |
|---|---|---|---|
| 第一层Q投影 | 1024→2048，W4 | DenseConvInt8TiledExecutor | MNNGemmInt8AddBiasScale_ARMV82_w4_Unit_FP16 |
| 词表输出层 | 1024→151936，W8 | DenseConvInt8TiledExecutor | MNNGemmInt8AddBiasScale_ARMV82_Unit_FP16 |

这张表的意思是：Backend选择的Execution这一层相同，实际负责计算的函数仍可以不同。前面按图元数据确认了W4/W8，现在又从手机实际call返回和同一ELF的精确符号地址确认对应函数，不再只讨论源码中可能发生什么。

拆开Q函数名就容易读了：Gemm表示矩阵乘加；Int8描述内部整数计算接口；w4区分四位权重路径；FP16表示这里输出半精度的路径。你无需背名字，应能说清这些标签分别描述哪件事。

公开汇编入口有整数乘累加、把累加值转成浮点并处理尺度、最后转成half写出。于是数据在一层内部可以经历“浮点输入→临时量化激活→小块解码W4权重并做整数乘累加→尺度/偏移处理→FP16输出”。这与“先把全部W4权重扩成FP16，再做浮点矩阵乘”不同。权重位宽、临时计算类型、输出类型是三个需要分别判断的东西。

连接源码时只需先看两个地方：`source/backend/cpu/compute/ConvInt8TiledExecutor.cpp`的onExecute负责组织输入和线程、调用GEMM；`source/backend/arm82/asm/arm64/low_memory/MNNGemmInt8AddBiasScale_ARMV82_w4_Unit_FP16.S`定义对应Q函数的计算。onResize中的选择逻辑解释为什么会安排这个函数，实际call证据确认本轮确实调用了它。

本轮还观察到Q的物理plane有32、29、17和1，而词表层是1。它们是传入Execution的物理位置数量，不直接等于有效输入token、输出token或请求次数。本轮按进程聚合，尚未给每条记录标注prefill/decode，不能凭一个plane值补出阶段结论。

工程结果压缩为一段：隔离重编3对象，565个库成员只有这3个改变；4手机请求、2对回答轨迹一致，所有运行正常退出和清理，两个目标的实际callee精确归因。独立审核和root复核通过，三种错证据被拒绝。原生产入口/库保持，诊断开销不用于性能结论；有限回答一致不等于全词表logits或全部设备验证。详情见[原始证据续做入口](../results/android/runtime-dispatch-20261013/阶段续做.md)。本阶段工程完成，已讲解，理解待反馈。
