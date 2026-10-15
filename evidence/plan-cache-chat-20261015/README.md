# 可选聊天入口验收

`phone_chat.py --mode full --stats --prefill-plan-cache`在本项目已配置环境中启用，默认full/kv入口保持原样。消息历史仍完整重算，KV复用token为0；主干执行计划另显示首次准备/已复用/短输入完整执行。总部署预算默认及上限512token，包含输入与生成预算，超过明确拒绝而不截历史。

同库开关配对各六轮：input长度13/61/87/109/13/61，输入与输出token哈希、文字和状态在完整本地记录中相同。候选冷建一次、命中三次；两次短请求走full，拒绝和/new保留计划。实际CLI另1请求40输入32输出，自动发现/default512/流式可见，三worker均正常退出、registry空。

13成功＋2预算拒绝，156forward=148full+8trunk，128decode包含在full；95相关本机测试通过。新入口只重新链接已验performance-v3库，无新完整logits观测或性能声称。启动成功命令继承args/digest记录，不能声称每条ADB原始exit均完整导出；cleanup有原始退出证据。

运行 `python evidence/plan-cache-chat-20261015/verify.py` 可复核精简记录的配对哈希、路由和计数，不需要手机。它不独立证明手机身份、执行过程、消息历史事务、清理过程或完整logits；这些依据单独的本地原件审核。模型、编译产物、设备日志不随本包分发。native非法513帧未注入；客户端拒绝及原生源码512限制已检查。未知传输结果的事务检查沿单元测试，未新增手机故障注入。

源码入口与教学见 `docs/Android可选执行计划缓存聊天-20261015.md`。构建脚本仍依赖项目固定MNN/guard/archive及本机环境，并非任意新机器clone后可运行。
