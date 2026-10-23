# MNN prompt_cache 条件 token 序列复现材料

本目录用于讨论：同一完整 `ChatMessages` 下，`prompt_cache` 是否应保留与完整 Jinja 渲染相同的条件 token；thinking 被模板省略时允许怎样的差异。**不声称已证明答案错误或 KV 张量损坏，也不是生产修复。**

实际运行版本是官方 `97715f88fbd7c82ab86d240d27c403930e739799`，Windows x64 / MSVC / monolithic shared / CPU high precision / 普通同步 AR。Qwen3-0.6B 的两轮用例覆盖非 EOS 预算截断和自然 EOS；公开状态结合固定源码作受限重建，没有直接读取 KV。2026-10-24 核对当前 master `959640b4a168667bc2e0e43ba7d334a2041aaa50` 的六个关联文件与该版本相同；**没有把它作为新 head 的运行结果**。

## 使用

下载本目录的 `upstream-repro-materials.zip` 并解压到新目录，先读包内 `README.md`。包内路径均相对解压目录；不要在本仓库根目录直接执行示例。自行准备同身份的现有模型资源及官方 MNN 构建；先在解压目录执行 `python prepare.py --model <MODEL> --out <NEW_WORKDIR>`，它核资源哈希、只生成配置，不下载、不复制模型、不推理。随后按包内 README 编译公开 API 探针、分别采集两进程真实退出状态，再运行分析器。请按自己环境填绝对路径，不能把示例退出码当实测值。

ZIP 内仅有14项白名单：公开源码/辅助脚本、说明、完整模板、资源身份、实际公开状态和脱敏文本日志。**不包含模型、权重、DLL、EXE或设备文件**。配置构造器限定当前资源与模式；这不是任意模型通用测试器。

ZIP 大小：246501 bytes。SHA256：

```text
a2759411ef06e0c1c2464728b910678d6c5d3efa1d7a05442925df11d1d7b262
```

包内 `issue-body.zh.md` 是保存证据时的草稿，引用“附件”表示本 ZIP 内材料，不代表已经存在某个上游 Issue。正式公开讨论应引用最终固定链接。参考功能来源：[MNN #4330](https://github.com/alibaba/MNN/pull/4330)。EOS 未 forward、非 EOS 最后 token 已 forward、多轮 history gap 与 thinking 契约的边界见包内正文。
