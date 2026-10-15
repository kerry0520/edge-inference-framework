"""Export and verify the single accepted, local-only mobile runtime bundle."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
PROFILE_ID = "mi11-qwen3-06b-plan-v1"
IDENTITY = {
    "source_commit": "d407447ed56c4121a11ccbd266dc184ca1ead0c2",
    "binary_sha256": "ee5ed8d620e00550ada713d3ffce25354c763bda5d34a23897c40315dc2894b5",
    "source_sha256": "bbec551047e4d58690e03d4d48b525371e7a6a06dc6d49cd314fa88c80788b1a",
    "helper_sha256": "f53b15b844e6eb20d3c309ac2b7fa953016b311b8453aa06b3877b5e68d13e04",
    "library_sha256": "7d6147a6c41af78572ba4704c83a4c719aba01cc43c5661721e252c9213291fa",
    "performance_manifest_sha256": "9ec358d77206aaa83022922481b2814bd165743d200e840ed975b3842d8d174c",
}
FILES = {"binary": "phone_chat_plan_worker", "worker": "phone_chat_plan_worker.cpp",
         "helper": "prefill_trunk_reuse.hpp", "runtime_build": "runtime-build.json",
         "provenance": "provenance.json"}
LIMITS = {"device_model": "M2011K2C", "hardware_serial": "1e7c759e",
          "model_directory": "/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope",
          "backend": "cpu", "chunk": 32, "max_context_tokens": 512}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    with Path(path).open("x", encoding="utf-8") as output:
        output.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _equal(actual, expected, label):
    # JSON equality alone accepts True for 1; a canonical serialization does not.
    if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("运行包身份不匹配: " + label)


def profile_document():
    return {"schema_version": 1, "profile_id": PROFILE_ID, "identity": dict(IDENTITY),
            "files": dict(FILES), "limits": dict(LIMITS)}


def runtime_document():
    return {"schema_version": 1, "profile_id": PROFILE_ID, **IDENTITY,
            "variant": "full_plan", "diagnostic": False, "plan_cache_version": 1,
            "metrics_version": 1, "chunk": 32}


def provenance_document():
    return {"schema_version": 1, "profile_id": PROFILE_ID, "identity": dict(IDENTITY),
            "accepted_stage": "plan-cache-chat-20261015/build-v2",
            "library_stage": "prefill-trunk-reuse-20261014/performance-build-v3",
            "scope": "local runtime export; not a source rebuild or model distribution",
            "export_requires": "original strict resolve_plan_build verification"}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def resolve_profile(profile_path):
    """Verify only bundle assets and current client; no historical build dependency."""
    try:
        supplied = Path(profile_path).expanduser().absolute()
        if supplied.is_symlink():
            raise ValueError("运行包profile不能是符号链接")
        path = supplied.resolve(strict=True)
        if not path.is_file():
            raise ValueError("运行包profile必须是文件")
        base = path.parent
        _equal(_read(path), profile_document(), "profile")
        paths = {}
        for role, relative in FILES.items():
            entry = base / relative
            resolved = entry.resolve(strict=True)
            if entry.is_symlink() or not resolved.is_relative_to(base) or not resolved.is_file():
                raise ValueError("运行包文件越界或不是普通文件: " + role)
            paths[role] = resolved
        for role, key in (("binary", "binary_sha256"), ("worker", "source_sha256"),
                          ("helper", "helper_sha256")):
            if sha(paths[role]) != IDENTITY[key]:
                raise ValueError("运行包文件身份不匹配: " + role)
        _equal(_read(paths["runtime_build"]), runtime_document(), "runtime-build")
        _equal(_read(paths["provenance"]), provenance_document(), "provenance")
        return {**IDENTITY, "manifest_sha256": sha(paths["runtime_build"]),
                "binary_path": str(paths["binary"]), "manifest_path": str(paths["runtime_build"]),
                "worker_source_path": str(paths["worker"]), "helper_path": str(paths["helper"]),
                "profile_path": str(path), "profile_sha256": sha(path), "bundle_root": str(base),
                "provenance_sha256": sha(paths["provenance"]), "profile_id": PROFILE_ID,
                "client_sha256": sha(__file__)}
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("运行包缺失或非法: " + str(error)) from error


def resolve_adb_path(explicit=None):
    """Choose one local executable without connecting or executing it."""
    if explicit is not None:
        candidate = Path(explicit).expanduser().resolve()
    else:
        candidate = ROOT / "tools/android/platform-tools/adb.exe"
        if not candidate.is_file():
            found = shutil.which("adb")
            if not found:
                raise ValueError("未找到ADB；请提供--adb-path")
            candidate = Path(found).resolve()
    if not candidate.is_file():
        raise ValueError("ADB路径不是文件: " + str(candidate))
    return candidate


def export_bundle(output, root=None):
    """Export once after strict archive/source/build verification; retain failures."""
    from phone_chat_plan import resolve_plan_build
    root = Path(ROOT if root is None else root).resolve()
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("导出目录已存在，保留原件: " + str(output))
    before = resolve_plan_build(root)
    for key, value in IDENTITY.items():
        if key != "source_commit" and before.get(key) != value:
            raise ValueError("导出输入不是已验收身份: " + key)
    inputs = {"binary": root / "build/plan-cache-chat-20261015/build-v2/phone_chat_plan_worker",
              "worker": root / "apps/android/phone_chat_plan_worker.cpp",
              "helper": root / "apps/android/prefill_trunk_reuse.hpp"}
    hashes = {role: sha(path) for role, path in inputs.items()}
    output.mkdir(parents=True, exist_ok=False)
    for role, source in inputs.items():
        with (output / FILES[role]).open("xb") as target:
            target.write(source.read_bytes())
    save(output / FILES["runtime_build"], runtime_document())
    save(output / FILES["provenance"], provenance_document())
    save(output / "profile.json", profile_document())
    if hashes != {role: sha(path) for role, path in inputs.items()} or before != resolve_plan_build(root):
        raise ValueError("导出期间输入变化；保留目录，不得运行")
    result = resolve_profile(output / "profile.json")
    save(output / "export-receipt.json", {"source_before": hashes, "source_after": hashes,
         "strict_build": before, "output_profile_sha256": result["profile_sha256"],
         "deployment_performed": False, "model_requests": 0})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="从本机已验收构建导出；不包含权重")
    export.add_argument("--output", required=True)
    resolve = commands.add_parser("resolve", help="只读验证运行包，不连接手机")
    resolve.add_argument("--profile", required=True)
    resolve.add_argument("--adb-path")
    args = parser.parse_args(argv)
    try:
        result = export_bundle(args.output) if args.command == "export" else resolve_profile(args.profile)
        if args.command == "resolve":
            result["adb_path"] = str(resolve_adb_path(args.adb_path))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
