"""Package only fixed accepted mobile clients and an already verified runtime.

No build, download, ADB, pairing or model process is started by this module.
The externally retained manifest digest fixes bytes; it is not an author signature.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.dont_write_bytecode = True
import phone_plan_profile as profile

MODULES = (
    "phone_chat.py", "phone_chat_build.py", "phone_chat_cleanup.py",
    "phone_chat_kv_session.py", "phone_chat_metrics.py", "phone_chat_plan.py",
    "phone_chat_query_session.py", "phone_chat_session.py", "phone_chat_transfer.py",
    "phone_infer.py", "phone_plan_profile.py", "phone_runtime_check.py",
    "phone_client_bundle.py",
)
RUNTIME = ("profile.json", *profile.FILES.values())
SCHEMA = "fixed-mobile-client-bundle-v1"
README = """# 本地手机聊天客户端包

适用范围：既有固定 Mi11 / Qwen3-0.6B 运行包。模型须已在手机上，手机须已配对；
电脑另行安装 Python 3.11+ 和 ADB。本包不含模型、ADB、工具链或静态库，不会替你配对。

先在包根目录离线检查（SHA 来自导出回执，须在包外保管）：

    python -B scripts/android/phone_client_bundle.py inspect --package . --expected-manifest-sha256 <导出回执SHA>

再在包根目录运行，替换 ADB 的实际路径：

    python -B scripts/android/phone_chat.py --mode full --stats --prefill-plan-cache --plan-profile runtime/profile.json --adb-path "C:/实际路径/adb.exe"

省略 --serial 时自动发现已配对目标手机；也可添加 --serial 当前IP:端口。
聊天中 /history 查看历史，/new 清空对话，/exit 退出。默认生成上限64 token。
不要使用其它 KV/query 模式当作这个独立包的启动入口。

manifest 的外部 SHA 只固定选中的字节，不是作者签名或新设备适配证明。
使用后允许 results 会话记录和 scripts/android/__pycache__ 普通缓存；检查不读取缓存。
此工具固定源文件，不保证解释器或已执行字节码的完整性，推荐始终用 python -B 启动。
"""


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def no_links(path):
    path = Path(os.path.abspath(path))
    for current in (path, *path.parents):
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("链接或reparse路径不允许: " + str(current))
    return path


def plain_file(path):
    path = no_links(path)
    if not path.is_file():
        raise ValueError("缺少普通文件: " + str(path))
    return path


def record(path):
    plain_file(path)
    return {"sha256": digest(path), "size": Path(path).stat().st_size}


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复JSON key: " + key)
        result[key] = value
    return result


def read_json(path):
    return json.loads(plain_file(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique)


def equal(left, right, label):
    if isinstance(left, set) and isinstance(right, set):
        left, right = sorted(left), sorted(right)
    if json.dumps(left, sort_keys=True) != json.dumps(right, sort_keys=True):
        raise ValueError("不匹配: " + label)


def validate_runtime(base):
    for name in RUNTIME:
        path = plain_file(base / name)
        if path.suffix == ".json":
            read_json(path)
    return profile.resolve_profile(base / "profile.json")


def expected_paths():
    return {"README.md": "readme", **{"scripts/android/" + n: "client" for n in MODULES},
            **{"runtime/" + n: "runtime" for n in RUNTIME}}


def file_inventory(base):
    result = set()
    for path in base.rglob("*"):
        no_links(path)
        if path.is_file():
            rel = path.relative_to(base).as_posix()
            # Python-generated cache is never consumed by verification.
            if ((path.parent == base / "scripts/android/__pycache__" and path.suffix == ".pyc")
                    or rel.startswith("results/")):
                continue
            result.add(rel)
        elif not path.is_dir():
            raise ValueError("包内有特殊文件")
    return result


def manifest_document(base, selected):
    return {"schema": SCHEMA, "profile_id": selected, "modules": list(MODULES),
            "files": {rel: {"role": role, **record(base / rel)}
                      for rel, role in expected_paths().items()}}


def export_bundle(profile_path, output):
    source = plain_file(profile_path)
    base = source.parent
    if source.name != "profile.json":
        raise ValueError("输入文件名必须为profile.json")
    identity = validate_runtime(base)
    modules_base = Path(__file__).absolute().parent
    inputs = {"runtime/" + n: base / n for n in RUNTIME}
    inputs.update({"scripts/android/" + n: modules_base / n for n in MODULES})
    before = {rel: record(path) for rel, path in inputs.items()}
    out = no_links(output)
    if out.exists():
        raise ValueError("输出必须是新目录")
    if not out.parent.is_dir():
        raise ValueError("输出父目录必须存在")
    # Reject writing into source trees and any ancestor of a consumed input.
    protected = (modules_base.parent.parent, base)
    for protected_path in protected:
        protected_path = no_links(protected_path)
        if out == protected_path or out.is_relative_to(protected_path) or protected_path.is_relative_to(out):
            # Workspace build/ exports are legitimate; source scripts itself is protected below.
            if protected_path == modules_base.parent.parent and out.is_relative_to(protected_path):
                if any(out.is_relative_to(protected_path / n) for n in ("scripts", "tests", "source", "docs")):
                    raise ValueError("输出位于受保护源码")
            else:
                raise ValueError("输出与输入范围重叠")
    out.mkdir()
    for rel, path in inputs.items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(path.read_bytes())
    with (out / "README.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(README)
    equal(validate_runtime(out / "runtime")["profile_id"], identity["profile_id"], "profile")
    for rel, path in inputs.items():
        equal(record(path), before[rel], "输入变化 " + rel)
        equal(record(out / rel), before[rel], "复制变化 " + rel)
    equal(file_inventory(out), set(expected_paths()), "输出集合")
    document = manifest_document(out, identity["profile_id"])
    # No acceptable manifest exists before all post-copy checks have passed.
    with (out / "manifest.json").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return {"status": "exported", "manifest_sha256": digest(out / "manifest.json"),
            "profile_id": identity["profile_id"], "client_modules": len(MODULES),
            "runtime_files": len(RUNTIME), "device_commands": 0, "model_requests": 0}


def inspect_bundle(package, expected_sha):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("需要外部64位小写SHA256")
    base = no_links(package)
    if not base.is_dir():
        raise ValueError("包目录不存在")
    manifest = plain_file(base / "manifest.json")
    equal(digest(manifest), expected_sha, "外部manifest SHA")
    data = read_json(manifest)
    equal(file_inventory(base), {*expected_paths(), "manifest.json"}, "完整文件集合")
    before = {rel: record(base / rel) for rel in expected_paths()}
    runtime = validate_runtime(base / "runtime")
    equal(data, manifest_document(base, runtime["profile_id"]), "manifest完整规范")
    for rel, binding in before.items():
        equal(record(base / rel), binding, "检查期间文件变化 " + rel)
    equal(file_inventory(base), {*expected_paths(), "manifest.json"}, "末尾文件集合")
    equal(digest(manifest), expected_sha, "manifest末尾SHA")
    return {"status": "verified", "manifest_sha256": expected_sha,
            "profile_id": runtime["profile_id"], "client_modules": len(MODULES),
            "runtime_files": len(RUNTIME), "device_commands": 0, "model_requests": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--plan-profile", required=True)
    exp.add_argument("--output", required=True)
    check = sub.add_parser("inspect")
    check.add_argument("--package", required=True)
    check.add_argument("--expected-manifest-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_bundle(args.plan_profile, args.output)
        else:
            result = inspect_bundle(args.package, args.expected_manifest_sha256)
    except (OSError, ValueError, TypeError) as error:
        print("客户端包失败: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
