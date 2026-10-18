# 独立托管Windows完整构建

公开构建源码提交为`613c3f3a58d5aafe3487324cbdf30776fc9ff353`，与后来仅更新文档的提交分开。[实际手动工作流](https://github.com/kerry0520/edge-inference-framework/actions/runs/39135379364)在独立GitHub Windows虚拟机成功，未使用本机旧对象或库；公开依赖准备后调用原prepare/build/candidate/audit链。13个子命令及configure/compile/link/archive-members四阶段实际退出均为0。

日志中唯一`HOSTED_BUILD_SUMMARY`报告565实际对象、565归档成员、101个Kleidi对象及4次原生审查工具调用；ELF需要liblog/libandroid/libm/libdl/libc。新的官方KitwareCMake4.1.1组合与本机旧4.1.1-msvc1分开记录，原recipe/lock/补丁保持。摘要和工具身份见[派生简结](summary.json)。

这里独立核对了公开日志、工作流/步骤成功状态和摘要一致性。完整中间记录随临时runner回收，没有公开下载原生制品来重新执行产物审查。云端新worker为`00fae79e`，仍`candidate_pending`、runtime pending、production false；没有手机命令、模型请求或程序/库/模型上传。它不能继承本机`94deb958`的手机验证或旧产物数值/性能结论。

同一613c3f3提交的Windows/Linux Python离线CI分别399次执行、394个唯一测试ID、0跳过，其中15次为新宿主入口测试。这与上述实际native编译是两条不同验证链；后续文档提交的CI另行核对，不借用该次结果。
