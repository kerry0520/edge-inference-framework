# 待验候选的资产绑定

新增phone_source_candidate.py prepare/inspect：固定外部manifest SHA、读取四项真实资产并核摘要和ELF/ar有限文件头。清单始终candidate_pending/accepted_profile=false/build_proof=not_verified，不执行或复制大型资产，不输出profile、不改已验白名单。

16单元测试全过、10独立异常输入拒绝。真实历史linkfix与既有四资产完成prepare/inspect实际退出0，前后身份相同；现代recipe格式仅fixture正例，没有真实generic新构建完成原件。真实绑定不证明fresh build/链接来源/设备正确性。production profile拒绝candidate，未来设备验收需单独实现并绑定清单。

summary.json只发布范围与计数；不含实际candidate、路径、历史receipt、原生程序、库、工具链或模型，不独立证明真实资产。课程：[新构建待验候选](../../docs/Android新构建的待验候选-20261016.md)。
