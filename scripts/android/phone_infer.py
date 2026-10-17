"""电脑单行问题 → 已验收官方 MNN demo；每次新进程，仅复用权重缓存。"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build/framework-integration-20261006"
RECORDS = ROOT / "results/android/phone-entry-20261006/requests"
RELEASE = "/data/local/tmp/MNN/framework-release-20261006"
MODEL = "/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope"
KNOWN_IP = "192.168.5.30"
EXPECTED_HARDWARE_SERIAL = "1e7c759e"
EXPECTED_MODEL = "M2011K2C"
SOURCE = "d407447ed56c4121a11ccbd266dc184ca1ead0c2"
MANIFEST_SHA = "21dedf6c803a8aa7fa4d48d52f545123ab64c6e23eb114e723faf184477ebfec"
BINARY_SHA = "dfa063cc7a4429c1f187cc150223a4fcbe24a72e01764d5217b75457b1cfb669"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_prompt(args):
    if args.prompt_file:
        data = Path(args.prompt_file).read_bytes()
        text = data.decode("utf-8")
        # One file-line terminator is framing, not part of getline's question.
        question = text[:-2] if text.endswith("\r\n") else text[:-1] if text.endswith("\n") else text
    else:
        question = args.prompt
        data = (question + "\n").encode("utf-8")
    if not question.strip() or "\n" in question or "\r" in question:
        raise ValueError("v1只接受一行非空问题；官方demo逐行当作独立请求，多行文件不代表多轮会话。")
    if "\x00" in question:
        raise ValueError("问题不能包含NUL字符。")
    if question.startswith("#") or question == "id,question,A,B,C,D,answer":
        raise ValueError("官方demo将这类行解释为注释或测评入口；v1拒绝它，保持问题原文不改写。")
    return question, data


def verify_binary():
    manifest_path = BUILD / "build.json"
    if sha(manifest_path) != MANIFEST_SHA:
        raise RuntimeError("集成构建manifest身份不匹配；请先核对已验收构建。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest["source_commit"] != SOURCE:
        raise RuntimeError("MNN源码版本不匹配。")
    binary = BUILD / "demo_candidate"
    if sha(binary) != BINARY_SHA:
        raise RuntimeError("官方demo候选二进制身份不匹配。")
    return binary


def _private_adb_endpoint(value):
    """Only canonical RFC1918 IPv4 endpoints advertised by ADB are eligible."""
    import ipaddress
    match = re.fullmatch(r"([0-9.]+):([0-9]{1,5})", value)
    if not match or not 1 <= int(match[2]) <= 65535:
        return False
    try:
        address = ipaddress.IPv4Address(match[1])
    except ipaddress.AddressValueError:
        return False
    return any(address in ipaddress.IPv4Network(network) for network in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _adb_serial_rank(serial):
    if serial == EXPECTED_HARDWARE_SERIAL:
        return 0
    if re.fullmatch(r"adb-" + re.escape(EXPECTED_HARDWARE_SERIAL) + r"-[A-Za-z0-9_-]+\._adb-tls-connect\._tcp\.?", serial):
        return 1
    if _private_adb_endpoint(serial):
        return 3
    # USB serials are opaque, but exclude addresses, service aliases, and command-like tokens.
    if re.fullmatch(r"[A-Za-z0-9_-]+", serial):
        return 2
    return None


def _online_adb_candidates(text):
    candidates = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            rank = _adb_serial_rank(fields[0])
            if rank is not None:
                candidates.add((rank, fields[0], False))
    return candidates


def _mdns_adb_candidates(text):
    candidates = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[1].rstrip(".") == "_adb-tls-connect._tcp" and _private_adb_endpoint(fields[2]):
            # Service labels never grant trust; every connected endpoint must pass getprop identity checks.
            candidates.add((3, fields[2], True))
    return candidates


class Adb:
    def __init__(self, serial=None):
        self.exe = ROOT / "tools/android/platform-tools/adb.exe"
        self.serial = serial
        self.commands = []

    def call(self, *args, targeted=True, check=True, timeout=30):
        argv = [str(self.exe)]
        if targeted and self.serial:
            argv += ["-s", self.serial]
        argv += list(args)
        self.commands.append(list(args))
        result = subprocess.run(argv, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError("ADB失败: " + result.stderr.decode("utf-8", "replace").strip())
        return result.stdout

    def shell(self, command, check=True, timeout=30):
        return self.call("shell", command, check=check, timeout=timeout)

    def _identity(self):
        model = self.shell("getprop ro.product.model", timeout=5).decode("utf-8", "replace").strip()
        if model != EXPECTED_MODEL:
            raise RuntimeError("设备型号核对失败: " + model)
        hardware = self.shell("getprop ro.serialno", timeout=5).decode("utf-8", "replace").strip()
        if not hardware:
            hardware = self.shell("getprop ro.boot.serialno", timeout=5).decode("utf-8", "replace").strip()
        if not hardware:
            raise RuntimeError("无法取得稳定设备序列号，不能建立安装身份。")
        return {"serial": self.serial, "hardware_serial": hardware, "model": model, "online": True}

    def select(self):
        # Explicit selection keeps the historical identity contract; callers may impose stricter policy.
        if self.serial:
            return self._identity()
        attempted = set()
        failures = []

        def probe(candidates):
            verified = []
            for rank, serial, connect in sorted(candidates):
                if verified and rank > verified[0][0]:
                    break
                if serial in attempted or len(attempted) >= 8:
                    continue
                attempted.add(serial)
                self.serial = serial
                try:
                    if connect:
                        self.call("connect", serial, targeted=False, timeout=5)
                    identity = self._identity()
                    if identity["hardware_serial"] == EXPECTED_HARDWARE_SERIAL:
                        verified.append((rank, serial, identity))
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    failures.append(type(exc).__name__ + ": " + serial)
                finally:
                    self.serial = None
            if not verified:
                return None
            best_rank = min(item[0] for item in verified)
            best = [item for item in verified if item[0] == best_rank]
            if len(best) != 1:
                raise RuntimeError("多个同优先级端点报告目标身份，请显式 --serial，避免自动选择歧义。")
            self.serial = best[0][1]
            return best[0][2]

        devices = self.call("devices", targeted=False, timeout=5).decode("utf-8", "replace")
        result = probe(_online_adb_candidates(devices))
        if result:
            return result
        try:
            mdns = self.call("mdns", "services", targeted=False, timeout=5).decode("utf-8", "replace")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            failures.append(type(exc).__name__ + ": mdns")
            mdns = ""
        result = probe(_mdns_adb_candidates(mdns))
        if result:
            return result
        raise RuntimeError("未发现通过稳定硬件身份核验的目标手机；请打开无线调试，必要时重新配对或显式 --serial。"
                           "没有扫描局域网。探测端点数: " + str(len(attempted)) + "; " + ", ".join(failures))

    def push_verified(self, file, remote):
        self.call("push", str(file), remote)
        digest = self.shell("sha256sum " + remote).decode().split()[0]
        if digest != sha(file):
            raise RuntimeError("部署SHA核对失败: " + remote)
        return {"remote": remote, "sha256": digest}


@contextlib.contextmanager
def request_lock():
    path = RECORDS.parent / "active-request.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("同一电脑已有入口请求。若前次被强制终止，请确认进程结束后移除active-request.lock。")
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def model_identity(adb):
    command = "sha256sum " + " ".join(MODEL + "/" + name for name in
        ("llm.mnn", "llm.mnn.weight", "llm_config.json", "tokenizer.txt", "tokenizer-android-lf.txt"))
    data = adb.shell(command)
    return {line.split()[1]: line.split()[0] for line in data.decode().splitlines()}


def install(adb, binary, record):
    adb.shell("mkdir -p " + RELEASE + "/requests")
    remote_binary = RELEASE + "/llm_demo"
    existing = adb.shell("sha256sum " + remote_binary, check=False).decode().split()
    reused = bool(existing and existing[0] == BINARY_SHA)
    if not reused:
        adb.push_verified(binary, remote_binary)
        adb.shell("chmod 755 " + remote_binary)
    record["deployment"] = {"binary": remote_binary, "sha256": BINARY_SHA, "reused_verified_binary": reused}
    identity_file = RECORDS.parent / "installation-model-identity-v2.json"
    if identity_file.exists():
        identity = json.loads(identity_file.read_text(encoding="utf-8"))
        if identity["hardware_serial"] != record["device"]["hardware_serial"] or identity["model"] != EXPECTED_MODEL or identity["binary_sha256"] != BINARY_SHA:
            raise RuntimeError("安装设备身份改变；需重新核对模型。")
        record["model_identity"] = {"verification": "recorded_at_installation", **identity}
    else:
        identity = {"initial_connection_serial": adb.serial, "hardware_serial": record["device"]["hardware_serial"], "model": EXPECTED_MODEL, "binary_sha256": BINARY_SHA,
                    "verified_at": datetime.now(timezone.utc).isoformat(), "files": model_identity(adb)}
        write_json(identity_file, identity)
        record["model_identity"] = {"verification": "full_five_files_now", **identity}


def extract_answer(stdout, config, prompt):
    text = stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    header = "config path is " + config + "\n" + "prompt file is " + prompt + "\n"
    if not text.startswith(header):
        raise RuntimeError("官方demo未到达已核对的eval入口，或启动输出格式改变；保留raw日志供核对。")
    answer = text[len(header):]
    if not answer.strip():
        raise RuntimeError("官方demo没有返回有效回答；exit0不等于加载成功。")
    return answer


def execute(adb, binary, directory, record, args):
    record["device"] = adb.select()
    install(adb, binary, record)
    remote_request = RELEASE + "/requests/" + record["request_id"]
    adb.shell("mkdir " + remote_request)
    config = RELEASE + "/config.json"
    prompt = remote_request + "/prompt.txt"
    record["config_deployment"] = adb.push_verified(directory / "config.json", config)
    record["prompt_deployment"] = adb.push_verified(directory / "prompt.txt", prompt)
    nonce = record["request_id"]
    pid_marker, exit_marker = "PHONE_PID_" + nonce, "PHONE_EXIT_" + nonce
    # The user's text is only in prompt.txt. This shell contains safe owned paths and validated integers.
    command = ("cd " + RELEASE + "; ./llm_demo " + config + " " + prompt + " " + str(args.max_new_tokens)
               + " no_think > " + remote_request + "/stdout.log 2>&1 & child=$!; echo " + pid_marker
               + "=$child; wait $child; code=$?; echo " + exit_marker + "=$code")
    record["execution_submitted"] = True
    record["phone_run_attempted"] = True
    record["phone_run"] = None  # Unknown until the PID/exit acknowledgement arrives.
    wrapper = adb.shell(command, timeout=900)
    (directory / "wrapper.log").write_bytes(wrapper)
    framing = wrapper.decode().replace("\r\n", "\n")
    match = re.fullmatch(re.escape(pid_marker) + r"=(\d+)\n" + re.escape(exit_marker) + r"=(\d+)\n", framing)
    if not match:
        raise RuntimeError("进程PID/exit帧不完整，不能宣称运行成功。")
    pid, code = map(int, match.groups())
    record.update(pid=pid, device_exit=code, phone_run=True)
    adb.call("pull", remote_request + "/stdout.log", str(directory / "stdout.log"))
    logcat = adb.call("logcat", "-d", "-v", "threadtime", "--pid=" + str(pid))
    (directory / "pid-logcat.log").write_bytes(logcat)
    record["raw_sha256"] = {name: sha(directory / name) for name in ("wrapper.log", "stdout.log", "pid-logcat.log")}
    errors = re.findall(r"(?im)^.*(?:LLM init error|Failed to load tokenizer|Error: Generation failed|Error: Response generation failed|Can't run session because not resized).*$", logcat.decode("utf-8", "replace"))
    record["detected_runtime_errors"] = errors
    if code != 0 or errors:
        raise RuntimeError("官方入口加载/运行失败；见本请求的stdout和PID logcat。")
    answer = extract_answer((directory / "stdout.log").read_bytes(), config, prompt)
    (directory / "answer.txt").write_text(answer, encoding="utf-8")
    counts = {}
    for key, pattern in (("prompt_tokens", r"prompt tokens num = (\d+)"), ("decode_tokens", r"decode tokens num = (\d+)")):
        found = re.search(pattern, logcat.decode("utf-8", "replace"))
        if found:
            counts[key] = int(found.group(1))
    record.update(status="success", answer_returned=True, answer=answer, answer_sha256=sha(directory / "answer.txt"), observed_token_counts=counts)
    return answer


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog="这是电脑CLI，不是Android APK或常驻聊天服务。每次重新load，无跨请求KV历史。")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt", help="原样发送的一行问题")
    group.add_argument("--prompt-file", type=Path, help="UTF-8单行文件，可有一条行终止符")
    parser.add_argument("--serial", help="显式ADB设备；默认devices/mDNS仅寻找已记录手机IP")
    parser.add_argument("--max-new-tokens", type=int, default=64, help="1..512，默认64")
    parser.add_argument("--cache-mode", choices=("cached", "uncached"), default="cached", help="两者都mmap；cached复用权重文件，uncached使用隔离临时文件")
    parser.add_argument("--prepare-only", action="store_true", help="只在本地准备请求，不连接或运行手机")
    args = parser.parse_args(argv)
    if not 1 <= args.max_new_tokens <= 512:
        parser.error("--max-new-tokens必须为1..512")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        question, data = read_prompt(args)
        binary = verify_binary()
    except (ValueError, OSError, UnicodeError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    request_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    directory = RECORDS / request_id
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "prompt.txt").write_bytes(data)
    config = {"base_dir": MODEL + "/", "tokenizer_file": "tokenizer.txt", "backend_type": "cpu", "thread_num": 4,
              "precision": "low", "memory": "low", "sampler_type": "greedy", "use_mmap": True,
              "use_cached_mmap": args.cache_mode == "cached", "kvcache_mmap": False}
    write_json(directory / "config.json", config)
    record = {"request_id": request_id, "status": "prepared_offline", "phone_run": False, "source_version": SOURCE,
              "manifest_sha256": MANIFEST_SHA, "binary_sha256": BINARY_SHA, "entry_script_sha256": sha(__file__), "question": question,
              "prompt_sha256": sha(directory / "prompt.txt"), "cache_mode": args.cache_mode,
              "max_new_tokens": args.max_new_tokens, "official_demo_unmodified": True, "integrated_fixes": ["mmap-cache-isolation", "tokenizer-crlf-v3"],
              "model_process_reloaded_each_request": True, "persistent_cache_kind": "weights_not_KV"}
    adb = Adb(args.serial)
    try:
        if args.prepare_only:
            print("离线请求已准备，未连接或运行手机。记录: " + str(directory))
        else:
            with request_lock():
                if (RECORDS.parent / "uncertain-request.json").exists():
                    raise RuntimeError("上一请求的远端进程状态未确认；先确认手机进程结束，再移除uncertain-request.json。")
                try:
                    answer = execute(adb, binary, directory, record, args)
                except (OSError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
                    if record.get("phone_run_attempted") and record.get("phone_run") is None:
                        write_json(RECORDS.parent / "uncertain-request.json", {"request_id": request_id, "request_record": str(directory), "remote_status": "unknown", "error": str(error) or "interrupted"})
                    raise
            print(answer, end="" if answer.endswith("\n") else "\n")
            print("请求记录: " + str(directory), file=sys.stderr)
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        record.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error=str(error) or "interrupted")
        print(str(error) + "\n请求记录: " + str(directory), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        record["adb_commands"] = adb.commands
        write_json(directory / "request.json", record)


if __name__ == "__main__":
    raise SystemExit(main())
