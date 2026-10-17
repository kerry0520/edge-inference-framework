"""Read-only readiness checks for a fixed accepted mobile runtime bundle.

Offline success means the local client, bundle and ADB file are present. It is
not a device, model correctness, execution or performance acceptance result.
"""
import argparse
import importlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

from phone_plan_profile import LIMITS, resolve_adb_path, resolve_profile
from phone_infer import Adb

CLIENT_MODULES = (
    "phone_chat", "phone_chat_plan", "phone_plan_profile", "phone_chat_session",
    "phone_chat_metrics", "phone_chat_kv_session", "phone_chat_transfer",
    "phone_chat_cleanup", "phone_infer",
)
ASSET_NAMES = ("llm.mnn", "llm.mnn.weight", "llm_config.json", "tokenizer.txt",
               "tokenizer-android-lf.txt")
TIMEOUT = 15


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""


def _run(report, adb, *args, timeout=TIMEOUT):
    argv = [str(adb), *args]
    record = {"argv": argv, "actual_exit": None, "timed_out": False,
              "stdout": "", "stderr": ""}
    report["commands"].append(record)
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout)
        record.update(actual_exit=result.returncode, stdout=_text(result.stdout),
                      stderr=_text(result.stderr))
    except subprocess.TimeoutExpired as error:
        record.update(timed_out=True, stdout=_text(error.stdout), stderr=_text(error.stderr))
        raise RuntimeError("ADB检查超时；没有启动模型") from error
    except OSError as error:
        record["stderr"] = str(error)
        raise RuntimeError("无法执行ADB: " + str(error)) from error
    if result.returncode != 0:
        raise RuntimeError("ADB检查失败: " + record["stderr"].strip())
    return record["stdout"].replace("\r\n", "\n")


def _select_device(report, adb, serial):
    if serial is not None:
        devices = _run(report, adb, "devices", "-l")
        online = {fields[0] for line in devices.splitlines()
                  if len(fields := line.split()) >= 2 and fields[1] == "device"}
        if serial in online:
            return serial
        # An explicitly supplied current endpoint need not be advertised by mDNS.
        # Identity is still checked before any model-file checks.
        _run(report, adb, "connect", serial)
        return serial

    class RecordedSelection(Adb):
        def call(self, *args, targeted=True, check=True, timeout=TIMEOUT):
            arguments = (("-s", self.serial) if targeted and self.serial else ()) + args
            # Selection only issues checked reads/connects. Keep its raw exits in
            # the readiness report rather than silently switching transports.
            if not check:
                raise ValueError("readiness selection requires checked commands")
            return _run(report, adb, *arguments, timeout=timeout).encode("utf-8")

    identity = RecordedSelection().select()
    report["device"]["identity"] = identity
    return identity["serial"]


def check_runtime(profile_path, adb_path=None, serial=None, device=False):
    report = {"schema_version": 1, "accepted": False,
              "scope": "fixed bundle readiness only; no model execution or correctness acceptance",
              "python": {"version": sys.version, "executable": sys.executable},
              "offline": {"status": "failed", "checks": {}},
              "device": {"status": "not_run"}, "commands": [], "errors": []}
    checks = report["offline"]["checks"]
    try:
        profile = resolve_profile(profile_path)
        checks["profile"] = {"status": "passed", "profile_id": profile["profile_id"],
                             "profile_sha256": profile["profile_sha256"],
                             "binary_sha256": profile["binary_sha256"]}
        modules = [importlib.import_module(name).__name__ for name in CLIENT_MODULES]
        checks["client_imports"] = {"status": "passed", "modules": modules,
                                    "third_party_packages_required": False}
        adb = resolve_adb_path(adb_path)
        checks["adb_path"] = {"status": "passed", "path": str(adb),
                              "executable_test": "not_run"}
        report["offline"]["status"] = "passed"
    except Exception as error:
        report["errors"].append({"stage": "offline", "type": type(error).__name__,
                                 "message": str(error)})
        return report
    if device:
        report["device"] = {"status": "failed"}
        try:
            selected = _select_device(report, adb, serial)
            identity = report["device"].get("identity")
            if identity is None:
                model = _run(report, adb, "-s", selected, "shell", "getprop ro.product.model").strip()
                hardware = _run(report, adb, "-s", selected, "shell", "getprop ro.serialno").strip()
                if not hardware:
                    hardware = _run(report, adb, "-s", selected, "shell", "getprop ro.boot.serialno").strip()
                identity = {"serial": selected, "model": model, "hardware_serial": hardware}
                report["device"]["identity"] = identity
            model, hardware = identity["model"], identity["hardware_serial"]
            if model != LIMITS["device_model"] or hardware != LIMITS["hardware_serial"]:
                raise ValueError("手机身份不匹配固定运行包；未读取模型资产")
            assets = []
            for name in ASSET_NAMES:
                path = LIMITS["model_directory"] + "/" + name
                quoted = shlex.quote(path)
                _run(report, adb, "-s", selected, "shell",
                     "test -f " + quoted + " && test -s " + quoted + " && test -r " + quoted)
                assets.append({"name": name, "path": path, "status": "present_nonempty_readable"})
            report["device"].update(status="passed", assets=assets,
                model_contents_verified=False, native_run="not_run",
                note="只检查固定目录的五个文件可读非空，不校验其内容或模型加载")
            checks["adb_path"]["executable_test"] = "passed"
        except Exception as error:
            report["errors"].append({"stage": "device", "type": type(error).__name__,
                                     "message": str(error)})
            return report
    report["accepted"] = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读检查固定手机运行包；默认不连接手机、不启动ADB。")
    parser.add_argument("--plan-profile", required=True)
    parser.add_argument("--adb-path")
    parser.add_argument("--serial", help="仅--device时使用的已配对ADB连接标识")
    parser.add_argument("--device", action="store_true", help="显式允许ADB发现和只读手机检查；不运行模型")
    parser.add_argument("--output", help="新建JSON记录；已有文件拒绝覆盖")
    args = parser.parse_args(argv)
    if args.serial is not None and not args.device:
        parser.error("--serial需要--device；默认离线检查不会连接手机")
    output = None
    try:
        if args.output:
            output = Path(args.output).expanduser().open("x", encoding="utf-8")
    except OSError as error:
        print("无法新建检查记录，未进行检查: " + str(error), file=sys.stderr)
        return 1
    try:
        report = check_runtime(args.plan_profile, args.adb_path, args.serial, args.device)
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if output is not None:
            output.write(encoded)
            output.flush()
        print(encoded, end="")
        return 0 if report["accepted"] else 1
    finally:
        if output is not None:
            output.close()


if __name__ == "__main__":
    raise SystemExit(main())
