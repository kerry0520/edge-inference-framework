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
    parser.add_argument("--serial", help="可选ADB连接地址；省略时自动发现已配对手机")
    parser.add_argument("--prompt", help="一次提问后正常退出；省略时进入交互聊天")
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--max-context-tokens", type=int, default=2048,
                        help="部署输入+生成token预算，不代表模型最大上下文")
    args = parser.parse_args()
    if not 1 <= args.max_new_tokens <= 512:
        parser.error("max-new-tokens为1..512")
    if not 1 <= args.max_context_tokens <= 8192:
        parser.error("max-context-tokens为1..8192")
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
    module = importlib.import_module("phone_chat_metrics" if args.stats else modules[args.mode])
    forwarded = [module.__file__, "--max-new-tokens", str(args.max_new_tokens),
                 "--max-context-tokens", str(args.max_context_tokens)]
    if args.serial is not None:
        forwarded += ["--serial", args.serial]
    if args.prompt is not None:
        forwarded += ["--prompt", args.prompt]
    if args.stats:
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
