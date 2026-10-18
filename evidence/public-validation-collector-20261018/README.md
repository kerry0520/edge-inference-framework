# 公开候选验证CLI采集入口

新增phone_candidate_validation_collect.py：默认检查候选/参考并生成计划，无子进程、ADB或伪命令退出记录；--run才一次调用原validator CLI，stdout/stderr实时写文件，保存原协议所需四原件及真正child实际退出。采集器外层还核源码/输入未变，只有外层0才能继续seal，不把子进程0覆盖或冒充外层成功。启动失败和中断如实保留None/PID/unknown，不自动重发、不强杀其他进程、不宣称资源已清理。

23项fixture通过、0skip，含本机短Python孩子的非零退出和实时日志，未运行模型。真实已有候选的默认CLI在进程启动审计钩子下0尝试、无run/childcommand；钩子不是OS沙箱。原验证器、seal协议、正式profile均保持。

同时开展干净公开本地clone的新输出构建，prepare成功但首次configure对象写入Permission denied；新输出重试尚在进行，未将prepare称编译/手机成功，更不称物理新电脑验证。这里只发布代码、合成测试、学习说明与派生摘要，原路径/日志/回执/模型/程序不公开。
