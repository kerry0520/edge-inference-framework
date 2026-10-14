"""Opt-in native phase counters; client elapsed time is a separate clock."""
import argparse
import codecs
import math
from pathlib import Path
import sys
from phone_infer import ROOT
import phone_chat_session as full
import phone_chat_kv_session as kv


def derive_client_text_timing(committed):
    """Replay UTF-8 frames on the client clock; this is not native token TTFT.

    The clock begins at the base client's request start before serialization.
    A frame timestamp follows full receipt and JSON decoding, before display.
    Raises ValueError for unusable evidence; callers must not undo a committed
    conversation just because supplementary timing evidence is unavailable.
    """
    def seconds(value):
        if type(value) not in (int, float) or value < 0:
            raise ValueError("client frame timing must be finite and nonnegative")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("client frame timing must be finite and nonnegative")
        return value

    elapsed = seconds(committed["elapsed_seconds"])
    frames = committed.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("client frame timing requires a final frame")
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    first = None
    previous = 0
    sequence = 0
    raw_output = bytearray()
    for index, entry in enumerate(frames):
        at = seconds(entry["elapsed_seconds"])
        if at < previous or at > elapsed:
            raise ValueError("client frame times are not monotonic within request elapsed")
        previous = at
        message = entry["message"]
        if message.get("id") != committed["response"]["id"]:
            raise ValueError("client frame belongs to another request")
        if message.get("event") == "chunk":
            if type(message.get("seq")) is not int or message["seq"] != sequence:
                raise ValueError("client frame sequence mismatch")
            encoded = message.get("bytes_hex")
            if (not isinstance(encoded, str) or len(encoded) % 2 or
                    any(c not in "0123456789abcdef" for c in encoded)):
                raise ValueError("client chunk bytes are not canonical hex")
            raw = bytes.fromhex(encoded)
            raw_output.extend(raw)
            text = decoder.decode(raw, final=False)
            if text and first is None:
                first = at
            sequence += 1
        elif message.get("event") == "final" and index == len(frames) - 1:
            tail = decoder.decode(b"", final=True)
            if tail and first is None:
                first = at
            if (message != committed["response"] or type(message.get("chunk_count")) is not int or
                    message["chunk_count"] != sequence or
                    bytes(raw_output).decode("utf-8") != message.get("text")):
                raise ValueError("client final frame does not match confirmed output")
            return {"client_first_text_seconds": first,
                    "client_final_frame_seconds": at,
                    "client_tail_seconds": None if first is None else at - first}
        else:
            raise ValueError("client trace contains an unexpected frame")
    raise ValueError("client trace has no final frame")


def validate_metrics(response, full_mode=False):
    metrics = response.get("metrics")
    keys = ("version", "input_tokens", "prefill_tokens", "reused_tokens",
            "output_tokens", "decode_steps", "prefill_us", "decode_us", "response_wall_us")
    if not isinstance(metrics, dict) or any(type(metrics.get(k)) is not int or metrics[k] < 0 for k in keys):
        raise RuntimeError("metrics必须是非负整数（不接受bool）")
    if metrics["version"] != 1:
        raise RuntimeError("metrics协议版本不匹配")
    ids = response.get("full_input_ids")
    if (not isinstance(ids, list) or not ids or len(ids) != response["input_tokens"] or
            any(type(value) is not int or value < 0 for value in ids)):
        raise RuntimeError("metrics完整输入token ID元数据非法")
    if (metrics["input_tokens"] != response["input_tokens"] or
            metrics["prefill_tokens"] != response["prompt_tokens"] or
            metrics["input_tokens"] != metrics["prefill_tokens"] + metrics["reused_tokens"] or
            metrics["output_tokens"] != len(response["tokens"]) or
            metrics["input_tokens"] < 1 or metrics["prefill_tokens"] < 1):
        raise RuntimeError("metrics token计数与已确认响应不匹配")
    if full_mode:
        if metrics["reused_tokens"] != 0:
            raise RuntimeError("完整重算不得报告跨轮KV复用")
    elif metrics["reused_tokens"] != response["reused_tokens"]:
        raise RuntimeError("metrics KV复用计数不一致")
    expected_steps = metrics["output_tokens"] - (response["status"] == "normal_finished")
    if metrics["decode_steps"] != expected_steps:
        raise RuntimeError("metrics decode步骤与终止条件不匹配")
    if metrics["prefill_us"] + metrics["decode_us"] > metrics["response_wall_us"]:
        raise RuntimeError("metrics原生阶段耗时超过原生response耗时")
    return dict(metrics)


class MetricsMixin:
    full_mode = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_stats = None
        self.record["metrics_client_sha256"] = full.sha(__file__)
        self.record["base_client_sha256"] = full.sha(Path(__file__).with_name(
            "phone_chat_session.py" if self.full_mode else "phone_chat_kv_session.py"))
        self.record["router_sha256"] = full.sha(Path(__file__).with_name("phone_chat.py"))

    def validate_response(self, response):
        super().validate_response(response)
        validate_metrics(response, self.full_mode)

    def ask(self, *args, **kwargs):
        response = super().ask(*args, **kwargs)
        # Parent validates all fields and commits before this display snapshot.
        committed = self.record["requests"][-1]
        summary = {**response["metrics"], "request_id": response["id"],
                   "status": response["status"],
                   "client_elapsed_seconds": committed["elapsed_seconds"]}
        try:
            summary.update(derive_client_text_timing(committed))
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
            # Supplementary evidence cannot turn an already committed answer
            # into a failed/unknown request or replace its confirmed history.
            summary.update(client_first_text_seconds=None,
                           client_final_frame_seconds=None, client_tail_seconds=None,
                           client_text_timing_error=str(error))
        committed["metrics_summary"] = summary
        self.last_stats = summary
        self.record["last_stats"] = summary
        self.persist()
        full.save(self.directory/(response["id"]+".json"), committed)
        return response

    def format_stats(self):
        m = self.last_stats
        if m is None:
            return "[统计] 尚无已确认成功请求。"
        first = m.get("client_first_text_seconds")
        wait = "无可用非空文字计时" if first is None else "首段非空文字等待 %.3f ms" % (first * 1000)
        tail = m.get("client_tail_seconds")
        tail_text = "" if tail is None else "；首段到final帧 %.3f ms" % (tail * 1000)
        return ("[统计: 最近已确认请求] 输入 %d token = prefill %d + 复用 %d；输出 %d token（遇EOS计入）；"
                "decode %d 步\n"
                "[原生MNN计时] prefill %.3f ms；decode %.3f ms；response %.3f ms\n"
                "[电脑客户端计时] 请求区间 %.3f ms（含传输及接收阶段记录；final校验后、最终落盘前结束；不是TTFT）" % (
                    m["input_tokens"], m["prefill_tokens"], m["reused_tokens"],
                    m["output_tokens"], m["decode_steps"], m["prefill_us"]/1000,
                    m["decode_us"]/1000, m["response_wall_us"]/1000,
                    m["client_elapsed_seconds"]*1000)) + (
                        "\n[电脑接收帧计时] " + wait + tail_text +
                        "（请求开始→完整帧接收/解析；含传输，不含模型加载；不是原生TTFT或屏幕呈现时间）")


BUILD = ROOT/"build/chat-metrics-20261012/worker-build-v2"
MANIFEST = ROOT/"results/android/chat-metrics-20261012/worker-build-v2"


class FullStatsSession(MetricsMixin, full.Session):
    full_mode = True
    release = "/data/local/tmp/MNN/chat-metrics-full-20261012"
    record_root = ROOT/"results/android/chat-metrics-20261012/full-sessions"
    binary_path = BUILD/"full_worker"
    manifest_path = MANIFEST/"full-build.json"
    worker_source = ROOT/"apps/android/phone_chat_metrics_worker.cpp"


class KvStatsSession(MetricsMixin, kv.Session):
    release = "/data/local/tmp/MNN/chat-metrics-kv-20261012"
    record_root = ROOT/"results/android/chat-metrics-20261012/kv-sessions"
    binary_path = BUILD/"kv_worker"
    manifest_path = MANIFEST/"kv-build.json"
    worker_source = ROOT/"apps/android/phone_chat_kv_metrics_worker.cpp"


class Full32StatsSession(FullStatsSession):
    """Same guarded library/chunk as KV; experimental control, not CLI mode."""
    release = "/data/local/tmp/MNN/chat-metrics-full32-20261012"
    record_root = ROOT/"results/android/chat-metrics-20261012/full32-sessions"
    binary_path = BUILD/"full32_worker"
    manifest_path = MANIFEST/"full32-build.json"
    extra_config = {"chunk": 32}


def selected_stats_session(tag, mode):
    """Return an isolated profile; never mutate default session class attributes."""
    from phone_chat_build import resolve_stats_build
    profile = resolve_stats_build(tag, mode)
    base = FullStatsSession if mode == "full" else KvStatsSession

    class SelectedStatsSession(base):
        binary_path = Path(profile["binary_path"])
        manifest_path = Path(profile["manifest_path"])
        worker_source = Path(profile["worker_source_path"])
        record_root = ROOT / "results/android/chat-build-choice-20261013/selected-sessions" / tag / mode
        release = "/data/local/tmp/MNN/chat-build-choice-20261013/" + tag + "/" + mode

        def __init__(self, *args, **kwargs):
            # Constructor validation occurs before even constructing SessionAdb.
            checked = resolve_stats_build(tag, mode)
            if checked != profile:
                raise ValueError("stats构建在选择后发生变化；未创建手机会话")
            super().__init__(*args, **kwargs)
            self.record["selected_stats_build"] = dict(profile)
            self.record["build_resolver_sha256"] = full.sha(Path(__file__).with_name("phone_chat_build.py"))
            self.persist()

        def start(self):
            if resolve_stats_build(tag, mode) != profile:
                raise ValueError("stats构建在启动前发生变化；未连接手机")
            return super().start()

    return SelectedStatsSession


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", choices=("full", "kv"), default="full")
    parser.add_argument("--stats-build-tag")
    args, forwarded = parser.parse_known_args()
    try:
        cls = (selected_stats_session(args.stats_build_tag, args.mode) if args.stats_build_tag is not None
               else FullStatsSession if args.mode == "full" else KvStatsSession)
    except ValueError as error:
        parser.error(str(error))
    original = sys.argv
    try:
        sys.argv = [original[0]] + forwarded
        entry = full if args.mode == "full" else kv
        return entry.main(session_type=cls, loaded_message=(
            "模型已加载；%s，/new新对话，/history历史，/stats最近成功请求统计，/exit退出。" %
            ("完整重算＋原生统计" if args.mode == "full" else "固定32块KV复用＋原生统计")))
    finally:
        sys.argv = original


if __name__ == "__main__":
    raise SystemExit(main())
