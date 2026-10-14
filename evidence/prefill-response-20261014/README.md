# 正式 response 隔离候选的正确性简结

本目录仅派生简结，完整原始手机日志和123个f32留在本地，不可只凭此summary独立复算设备结论。
v5真实response两进程14请求通过：31/32/33/64/65边界、短文字EOS、长文字max结束、同实例显式reset。
候选5次trunk，head64→59；59对完整词表分数逐字节一致，greedy/token/文字/状态/KV/位置独审通过。
此前v4曾错误拒绝CPU_EXTENSION13，实际全走full；该轮保留为fallback证据，不计作优化成功。
默认phone_chat未更换库，不是性能验收。每请求重新准备trunk的代价、额外内存和故障/reload等仍未测。
补丁见 `patches/prefill-response-elision-20261014/`，有固定guard前置要求；不是任意upstream版本直接可用。
新增离线reader只有获得完整本地证据/固定前置资产才可执行完整审核；仓库已有三对公开logits样例属于此前固定forward阶段。
