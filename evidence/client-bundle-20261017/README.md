# 完整客户端包：派生验收摘要

此处只公开派生结论，不分发手机原始记录、运行profile、ELF、模型或私有构建回执。原始记录保存在作者本地 `results/android/client-bundle-delivery-20261017/`，本摘要不是第三方可直接重放的原件。

实现及测试文件的SHA见summary.json；19项测试为fixture/mock，不调用ADB或模型。fresh已验资产另有真实导出、原包与中文空格路径检查；两条负例退出1且内容未变。独审直接核清单、文件字节及实际退出/计数。

固定Mi11/Qwen3-0.6B上的一次真实CLI请求使用搬移包自身代码，无serial；19输入/4输出、token/文字/状态/流字节SHA完整匹配参考。输出“2 + 1”因4-token预算截断，不证明算术答对。13模块+6runtime+README及清单使用前后相同；模型5文件未变、worker/launcher/CLI退出0、11项清理实际0且worker/forward缺席，使用后inspect0。39小型原始会话文件与原件副本一致，由独立agent只读核验。

短prompt full5/trunk0，未触发主干计划命中，不能据此新增优化或速度结论。只有同电脑已配置环境的搬移，没有新电脑从零准备或新手机证据。清单外部SHA固定字节，不是作者签名或解释器安全保证。
