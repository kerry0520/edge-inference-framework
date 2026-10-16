# 固定 Android CPU 源码构建

本配方把 MNN `d407447ed56c4121a11ccbd266dc184ca1ead0c2` 与项目已有三组修改组合起来，从空编译目录生成完整 `libMNN.a`，再链接 `apps/android/phone_chat_plan_worker.cpp`。不读取历史build.ninja、已编对象、旧静态库或results里的历史链接命令。模型权重不参与构建。

## 明确输入

- Windows x86_64宿主、Python 3.11（脚本使用标准库），以及在PATH中可执行的Git。Git用于读取固定提交、归档源码和应用补丁，不改输入仓库的工作树。
- 一个包含上述提交的本地MNN Git仓库，脚本直接`git archive`该提交，不把工作树中的未提交修改带入。
- Windows x86_64 Android NDK r30，revision `30.0.16248370`。
- CMake、Ninja程序路径。本轮工具为CMake4.1.1-msvc1、Ninja1.12.1；prepare记录实际输入工具SHA，build前重核，不把所有其他工具版本视为已验。
- KleidiAI1.16.0的本地tar.gz，SHA256见lock.json，来源URL也在锁中。脚本不下载；核验后解压到新的依赖目录，显式提供给MNN。
- 当前仓库的worker/helper、这里的三份补丁和lock.json。编译期为`PREFILL_REUSE_DIAGNOSTIC=0`。

三补丁按framework→guard→trunk应用；最后五个改变的框架源码恢复并验证到已验字节。上游代码及这些修改遵循MNN的Apache-2.0许可，补丁保留原代码中的许可信息。

NDK应是完整安装，包含sysroot、编译器资源、链接器、ar/ranlib和Android工具链文件；只有一个编译器入口文件并不代表构建依赖齐全。源码、NDK与KleidiAI由调用者预先准备，本脚本不联网取得它们。工具存在、prepare成功、build成功、运行包已验和手机已运行是不同状态。

## 两步运行

用实际路径替换下面占位值；NDK与输出应使用不含空格的ASCII路径，Windows可用指向原目录的junction。脚本不会删除、清空或续用现有输出目录。

```powershell
python scripts/android/build_mnn_source.py prepare --mnn-source "MNN仓库路径" --ndk "ASCII路径/android-ndk-r30" --cmake "cmake.exe路径" --ninja "ninja.exe路径" --kleidiai-archive "v1.16.0.tar.gz路径" --output "ASCII路径/新构建目录" --records "新记录目录" --jobs 4
```

prepare只导出并修补源码、解包锁定依赖、复制小型worker源码、记录工具与命令，不配置或编译。输出`prepared.json`。审核计划后计算该文件实际SHA256：

```powershell
(Get-FileHash "新记录目录/prepared.json" -Algorithm SHA256).Hash.ToLower()
python scripts/android/build_mnn_source.py build --prepared "新记录目录/prepared.json" --expected-sha256 "实际SHA256"
```

build重核源树、依赖与工具，执行CMake configure并校验关键选项，完整编译MNN后链接worker。编译stdout/stderr实时写入记录；保留actual_exit、成员清单及compile_commands/.ninja_log路径。`build.json`写构建身份，运行验证初始为pending。失败记录不覆盖；失败后的增量续编或旧编译目录重用不在这个专用配方范围内。

## 验收边界

本配方限定CPU/ARM64/API24/静态C++运行库及固定模型项目的构建组合。它不创建APK、不下载模型、不连接手机、不自动把新ELF加入生产profile。新的程序应先作为明确绑定的实验候选，与已验入口做设备回归。

MNN Git归档里的六个symlink（一个文档、五个Android商店媒体）在Windows提取为linkblob普通文本，不创建操作系统链接；这些不参与本CPU目标。不能据此宣称Android应用打包或文档构建已保持原生symlink语义。外部依赖tar仍拒绝符号/硬链接及越界路径。

编译成功表示构建链闭合；固定请求的生成回归表示该场景行为一致。它们不等于全模型完整logits一致、其他手机已适配，或新增性能提升。构建产物和模型不随公开源码仓库分发。

已有已验源构建的记录和产物可以通过显式位置映射搬移，见[文件位置与构建身份](../../../docs/Android构建身份与文件位置-20261016.md)。这不让新生成的`runtime_validation: pending`构建自动进入既有已验profile；新程序仍需自己的审查和设备验证。既有受验build记录本身可能保留当时的pending字段，受验状态由另外固定的构建/手机审查记录共同确定，不能只按单个字段判定。

新产物可以先用独立的`phone_source_candidate.py prepare`生成待验资产清单，再用`inspect`重新绑定记录与四项实际文件。该工具只检查文件摘要与有限容器头，不认证本配方执行过程或设备结果，不输出可供正式聊天使用的profile。使用方式见[待验候选交接](../../../docs/Android新构建的待验候选-20261016.md)；现代recipe已用真实完整构建验证：公开prepare后保留宿主写入失败，再用prepare-retry从新空对象目录完成565对象编译、归档和worker链接；真实receipt经候选入口在固定手机通过三题对照。没有复用旧对象或旧库，也没有单独手工修正链接命令。详见[新程序与源码](../../../docs/Android新程序如何证明来自源码-20261016.md)。这仍不自动纳入正式profile，不代表新电脑或完整logits已验。

## 保留失败结果后再做完整构建

配方为每次编译设置独立ASCII临时目录，并将TEMP/TMP覆盖记录在冻结计划和实际命令中。若一次完整构建失败，可以只复用已经固定的源码/依赖输入，准备另一个完全空的编译目录：

```powershell
python scripts/android/build_mnn_source.py prepare-retry --prepared "原记录目录/prepared.json" --expected-sha256 "原计划实际SHA256" --output "ASCII路径/全新attempt" --records "全新记录目录"
```

审核新prepared后仍用build子命令执行。新的计划明确记录源、依赖、worker源码与新对象/库/程序的位置；会在编译前重新核源树，完整重编所有对象。它不延续失败目录的增量进度，也不复制或修改旧对象。

`prepare-retry` 用于已保存的宿主环境构建失败之后：输入原 prepared.json 与审定 SHA，只复用已经导出并固定的源码、依赖和 worker 源码目录，创建全新的对象、库、程序、临时文件和日志目录。它不能继续原对象构建，也不会删除失败产物；新计划需再次审定 SHA。`build` 仍完整核对源码/依赖树，并从空对象目录开始。显式 TEMP/TMP 指向本次拥有的 ASCII tmp 目录，并保存在命令记录中。
