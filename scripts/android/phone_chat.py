"""Unified mobile chat entry; full recomputation is the default."""
import argparse
import importlib
import sys


def main():
    parser = argparse.ArgumentParser(description="手机多轮流式聊天：默认完整历史重算，可选择KV复用模式。")
    parser.add_argument("--mode", choices=("full", "kv", "query"), default="full",
                        help="full为默认；kv为固定32分块；query为本机候选实验入口，仅限固定Mi11/Qwen3-0.6B配置，待核对原版输出差异")
    parser.add_argument("--stats", action="store_true", help="启用逐轮token与原生prefill/decode统计（full或kv）")
    parser.add_argument("--stats-build-tag", help="选择build_chat_metrics.py的record-tag；仅--stats full/kv，省略保持默认构建")
    parser.add_argument("--prefill-plan-cache", action="store_true", help="完整重算历史并复用主干执行计划；需要--mode full --stats")
    parser.add_argument("--plan-profile", help="已导出的本地运行包profile.json；需要--prefill-plan-cache")
    parser.add_argument("--adb-path", help="运行包模式使用的本机ADB路径；需要--plan-profile")
    parser.add_argument("--serial", help="可选ADB连接地址；省略时自动发现已配对手机")
    parser.add_argument("--prompt", help="一次提问后正常退出；省略时进入交互聊天")
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--max-context-tokens", type=int, default=None,
                        help="部署输入+生成token预算，不代表模型最大上下文")
    args = parser.parse_args()
    if args.plan_profile is not None and not args.prefill_plan_cache:
        parser.error("--plan-profile需要--prefill-plan-cache --mode full --stats")
    if args.adb_path is not None and args.plan_profile is None:
        parser.error("--adb-path需要--plan-profile")
    if args.prefill_plan_cache and (args.mode != "full" or not args.stats or args.stats_build_tag is not None):
        parser.error("--prefill-plan-cache需要--mode full --stats，且不能同时选择--stats-build-tag")
    if args.max_context_tokens is None:
        args.max_context_tokens = 512 if args.prefill_plan_cache else 2048
    if not 1 <= args.max_new_tokens <= 512:
        parser.error("max-new-tokens为1..512")
    if not 1 <= args.max_context_tokens <= 8192:
        parser.error("max-context-tokens为1..8192")
    if args.prefill_plan_cache and args.max_context_tokens > 512:
        parser.error("主干计划复用入口的部署token预算为1..512")
    modules = {"full": "phone_chat_session", "kv": "phone_chat_kv_session",
               "query": "phone_chat_query_session"}
    if args.stats and args.mode == "query":
        parser.error("--stats仅支持full和kv")
    if args.stats_build_tag is not None:
        if not args.stats or args.mode == "query":
            parser.error("--stats-build-tag需要--stats且仅支持full或kv")
        from phone_chat_build import validate_tag
        try:
            validate_tag(args.stats_build_tag)
        except ValueError as error:
            parser.error(str(error))
    module = importlib.import_module("phone_chat_plan" if args.prefill_plan_cache else
                                     "phone_chat_metrics" if args.stats else modules[args.mode])
    forwarded = [module.__file__, "--max-new-tokens", str(args.max_new_tokens),
                 "--max-context-tokens", str(args.max_context_tokens)]
    if args.serial is not None:
        forwarded += ["--serial", args.serial]
    if args.prompt is not None:
        forwarded += ["--prompt", args.prompt]
    if args.plan_profile is not None:
        forwarded += ["--plan-profile", args.plan_profile]
    if args.adb_path is not None:
        forwarded += ["--adb-path", args.adb_path]
    if args.stats and not args.prefill_plan_cache:
        forwarded += ["--mode", args.mode]
        if args.stats_build_tag is not None:
            forwarded += ["--stats-build-tag", args.stats_build_tag]
    original = sys.argv
    try:
        sys.argv = forwarded
        return module.main()
    finally:
        sys.argv = original


if __name__ == "__main__":
    raise SystemExit(main())
