# Android项目快速开始

这个项目现在能让你从电脑向手机上的Qwen3-0.6B提问、流式看回答，并连续聊天。模型运算在手机ARM64 CPU上执行；电脑负责输入、显示和记录。交付形式是Python聊天入口、C++手机worker、固定MNN构建与可复核实机证据，目前没有Android图形App。

## 在已配置的电脑上玩

在项目目录打开PowerShell。手机打开无线调试，与电脑处于可互通的网络，且已经完成ADB配对，然后运行：

```powershell
python scripts/android/phone_chat.py --stats --mode kv
```

看到“模型已加载”和“问题>”后，输入一行问题并回车。首次启动包含连接、部署检查与模型加载；显示的每轮请求统计不包含模型加载时间。入口会自动寻找已记录、已配对的手机；必要时也可通过`--serial IP:连接端口`指定当前ADB连接地址，连接端口不是配对端口。

| 输入 | 作用 |
|---|---|
| 普通一行文字 | 发送新问题，接收流式回答，成功确认后提交聊天历史 |
| `/history` | 查看本地已确认的消息历史 |
| `/stats` | 重显最近成功请求的统计，不再次推理 |
| `/new` | 等待手机确认清空当前对话；最近成功请求统计仍是历史快照 |
| `/exit` | 正常结束当前模型会话并保存记录 |

预算拒绝不会提交新历史或新成功统计；可以换短问题继续。发生连接中断或请求结果未确认时，当前会话结束并保存未知记录，不自动重发。下次启动是新会话，不会自动恢复旧聊天。

只想问一次，也可以：

```powershell
python scripts/android/phone_chat.py --stats --mode kv --prompt "2+10等于几？"
```

默认每题最多生成64个token；想看长回答可加`--max-new-tokens 128`。`--max-context-tokens`默认2048，是本入口的输入加生成预算，不代表模型原生最大上下文。提高预算并不代表已经完成相应长上下文验收。

## full与kv怎样选

`--mode kv`使用固定32-token分块的跨轮KV复用；只有与当前模板重新编码的输入一致、满足保留规则的完整块才复用。首轮或前缀不足32时，复用数可以是0。

省略`--mode kv`就是默认full：每轮完整历史重算。它使用原有chunk0配置，**不是**性能实验中同chunk32的完整重算对照。当前的“7组结果一致”与续轮省时结论来自实验用Full32StatsSession对照KV32，不能直接改写成默认full与kv在任何问题上都等价。

`--stats`显示输入如何分成prefill和复用位置、生成token数、MNN原生阶段时间，以及电脑接收首段文字的等待。后者包含传输和接收处理，是用户开始收到文字的指标，不是模型内部首token时间，也不是屏幕刷新时间。客户端请求区间包含接收阶段记录和final校验，在最终提交记录落盘之前结束。`/new`后看到旧统计不表示缓存没有清空，它明确属于最近成功请求。

`query`是以前的候选实验入口，不属于本次full/kv交付主线，也不能使用`--stats`。

## 新电脑需要哪些东西

源码审阅包可以解压、读代码、核对已有证据，**不能直接独立运行手机模型**。运行入口仍依赖以下已匹配资源：

| 依赖 | 当前项目要求与入口 |
|---|---|
| Python | 本机使用Python 3.11；聊天与公开证据reader使用标准库 |
| ADB与配对 | `tools/android/platform-tools/adb.exe`；当前验收设备Mi11（M2011K2C）ARM64，自动发现有固定身份检查 |
| 手机模型 | `/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope`中的`llm.mnn`、`llm.mnn.weight`、`llm_config.json`、`tokenizer.txt`、`tokenizer-android-lf.txt`；文件身份保存于正式session的model_before/model_after |
| 统计worker | `build/chat-metrics-20261012/worker-build-v2/{full_worker,kv_worker,full32_worker}`及`results/android/chat-metrics-20261012/worker-build-v2/*-build.json`；日常只用full或kv，full32是实验对照 |
| 固定框架 | MNN 3.6.1提交`d407447ed56c4121a11ccbd266dc184ca1ead0c2`，匹配已验收base/guard静态库；不能仅换任意新版MNN库 |
| 重建工具 | Android NDK r30的ARM64/API24编译器、固定源码checkout、匹配静态库、先前构建manifest和本机ASCII路径别名 |

`scripts/android/build_chat_metrics.py`能根据`results/android/chat-session-20261007/build.json`的已有编译参数，重新构建三个统计worker，并核对base/guard库和固定源码身份。应使用新的`--record-tag`保留新构建证据。它**不会重建框架静态库、安装工具链或下载模型**，也不是一条适用于任意电脑的安装命令；不传构建选择参数仍读取worker-build-v2；新构建可用`--stats-build-tag TAG`显式选择，无需编辑Python路径。选择器核实际worker与构建记录，并保存本次profile；构建TAG应匹配新record-tag并重新验收。

现有构建和部署脚本用于本项目已配置环境，尚未交付新机器的一键安装、通用Android设备适配或APK。使用其他设备/模型/后端时，需要单独准备与验证。

## 学习只看三个入口

1. [连续复习](项目演示与主线讲解-20261016.md)：正文讲希望你理解的职责与原理，从真实手机请求接到MNN执行链。
2. [学习台账](推理框架学习台账.md)：区分工程已完成、已经讲过和理解待反馈。
3. [交付与复现指南](端侧推理框架交付与复现指南.md)：定位模型请求如何经过LLM、Module、Session/Pipeline和Execution；需要查工程证据时再沿链接深入。

你当前要抓住的关系是：消息先编码成token，LLM决定这轮哪些位置要算，MNN执行相应张量运算，再生成和返回文字。跨轮KV减少的是要重新输入的计算位置，并没有绕过MNN模型执行链。

2026-10-13新增[统计worker构建选择](Android统计worker构建选择-20261013.md)：例如`python scripts/android/phone_chat.py --stats --mode kv --stats-build-tag worker-choice-20261013-v1`。只改变所选worker构建，不更换模型；仅支持stats full/kv。旧V3ZIP仍冻结在上一版本，新代码与4新实机证据另存Git。

## 可选：完整历史重算＋主干执行计划复用（2026-10-15）

在本项目已配置的手机与构建环境中：

```powershell
python scripts/android/phone_chat.py --mode full --stats --prefill-plan-cache --max-new-tokens 64
```

消息历史仍保留，每轮KV按完整历史重算；优化复用的是主干Module计划。`/stats`会另外显示“首次准备”“已复用”或“短输入直接完整执行”。上方复用token计数指KV，本模式为0不代表计划没命中。

这个可选入口默认部署总预算512token，包含模板输入和生成预算；超过会拒绝并保留历史，可`/new`重开。仅支持full＋stats，不能与kv/query或stats-build-tag同时选择。不开开关时，默认入口和预算仍按原配置运行。

当前仅本机构建、固定MNN/Qwen3-0.6B/CPU配置验收，公开仓库不包含手机模型与编译库。详细职责与验收范围见[从聊天开关到手机执行计划](Android可选执行计划缓存聊天-20261015.md)。

## 可选：把已验程序导出为独立运行包

当前新增运行包入口，用于已有固定构建的导出与目录搬移。导出阶段需要原构建证据；指定profile运行时改从包内校验程序、辅助源码和身份记录，不再读取历史静态库。

```powershell
python scripts/android/phone_plan_profile.py export --output build/phone-runtime-v1
python scripts/android/phone_chat.py --mode full --stats --prefill-plan-cache --plan-profile "build/phone-runtime-v1/profile.json"
```

导出目录必须尚不存在，包包含约87MiB程序，不含手机模型或ADB；可用`--adb-path`显式指定ADB文件。当前仍限定同一已验手机与模型，不代表任意新设备适配。固定手机上的实机搬移对照和独立客户端提问已通过；尚未验收新电脑或其他手机。详见[运行包与构建依赖](Android运行包与构建依赖-20261015.md)。

## 可选：使用完整源码重建后的新运行包（2026-10-16）

本机已有已验新包，可直接启动：

```powershell
python scripts/android/phone_chat.py --mode full --stats --prefill-plan-cache --plan-profile "build/source-runtime-bundle-20261016/手机运行包 空格/profile.json"
```

固定手机三轮正式CLI验证通过，旧包仍受支持。新包不随公开仓库分发，export-source当前只支持本项目明确纳入的一组已验构建及对应证据。详见[源码构建运行包](Android源码构建运行包-20261016.md)。

已有受验构建记录和产物换目录后，可以给`export-source`显式提供`--records-root`和`--artifact-map`，保留原记录字节与固定身份检查。该路径已完成本机独立客户端的真实输入解析核对；不会把刚编出的新构建自动加入已验profile，也不代表新电脑全流程已验。格式和边界见[构建身份与文件位置](Android构建身份与文件位置-20261016.md)。

## 启动前快速定位问题

使用现有运行包先做本地检查；加`--device`才检查手机身份与必要文件，不会加载模型：

```powershell
python scripts/android/phone_runtime_check.py --plan-profile "build/source-runtime-bundle-20261016/手机运行包 空格/profile.json" --device --output readiness-phone.json
```

每次输出使用新的文件名，避免覆盖记录。检查通过后，再按上面的聊天命令启动。准备检查通过不等于模型已经回答；层次与已有会话的复核方法见[启动前检查与推理验收](Android启动前检查与推理验收-20261016.md)。

## 统一验收场景（固定手机已验）

```powershell
python scripts/android/phone_acceptance.py --plan-profile "build/source-runtime-bundle-20261016/手机运行包 空格/profile.json" --output results/local-acceptance-plan
```

默认仅保存离线计划。加`--run`并改用新输出目录，才会在手机执行长输入、续轮、预算拒绝后继续、new和短/长路径切换；新源码运行包已完成这整组实机场景及独立原件复核。该驱动每次生成8token，用于生命周期验收；日常聊天按上面的64token命令。详见[请求拒绝与状态提交](Android请求拒绝与状态提交-20261016.md)。演示讲解见[项目演示与主线](项目演示与主线讲解-20261016.md)，数值与性能范围见[证据索引](Android正式验收范围与证据索引-20261016.md)。
