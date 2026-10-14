# 分块边界阶段简结

这是本地独立审核通过的派生简结，不是完整原始证据包。
31/32/33/64/65及固定ID连续KV共40次手机forward；14对完整词表输出均有限且逐字节一致。
候选head20→14，28层KV/position连续；正式response、文本聊天复用、自由采样及性能尚未验证。
本轮完整设备日志、28份f32及身份原件只保留本地，不在此目录；summary本身不能独立复算这些设备结论。
仓库已有的 `evidence/verify_logits.py` 仍只复算上一阶段公开的三对样例。
新boundary reader在有完整本地fixture/raw目录时可逐case审核，并依赖上一阶段固定fixture的身份。
构建脚本沿用本地固定MNN/NDK/archive目录，并非独立从零构建入口。
