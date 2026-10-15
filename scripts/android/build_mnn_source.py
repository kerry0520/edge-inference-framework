"""Build the fixed production Android worker from a fresh MNN source tree.

prepare never configures or compiles. build consumes its frozen local plan;
it never downloads dependencies, resumes a build, or deletes an old directory.
"""
import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "d407447ed56c4121a11ccbd266dc184ca1ead0c2"
KLEIDI_SHA = "57b0a95c559ec6a4b1460bc742af600bfb6cdff8dc39154b376c25347d7b0891"
NDK_REVISION = "30.0.16248370"
PATCHES = {
    "configs/android/mnn-source-build/patches/framework.patch": "c0dbc9cdb2ac0f8f4bf1386bfd19cc81c9b46a21ef9d2f96f7808b847dc5eada",
    "configs/android/mnn-source-build/patches/guard.patch": "81e6785cb790b766e0a902abb73e70aa02803328981d455551c22c7fee7ac3d4",
    "configs/android/mnn-source-build/patches/trunk.patch": "45518a5785e6b395ad6c03320ba6e12cfd56f2a77b2c079a7c673b4accd2456a",
}
SOURCE_SHA = {
    "source/backend/cpu/CPUBackend.cpp": "901273d30293ec85dcab99c39a8342335174703603ee14053fad1053fe75a329",
    "transformers/llm/engine/src/tokenizer/tokenizer.cpp": "57f391f37d4c5e175e8e33710ea4ad9951f13482597476548706d221b77815af",
    "transformers/llm/engine/src/llm.cpp": "0c378976ca895e291bf49edc4c5d2d904d1eecd85dbd07b43c9ed4adf3346a75",
    "express/module/StaticModule.cpp": "9017cb2bbdfd4832a36d4b155fec4dd090802569ec658a1998d70d0232e55a66",
    "source/backend/cpu/CPUAttention.cpp": "cac0a45a8aceef1b0b6a792b2c1d3a70cc707b7f49496089148223f6a7465513",
}
CLIENT_SHA = {
    "apps/android/phone_chat_plan_worker.cpp": "bbec551047e4d58690e03d4d48b525371e7a6a06dc6d49cd314fa88c80788b1a",
    "apps/android/prefill_trunk_reuse.hpp": "f53b15b844e6eb20d3c309ac2b7fa953016b311b8453aa06b3877b5e68d13e04",
}
OPTIONS = {
    "CMAKE_BUILD_TYPE": "Release", "ANDROID_ABI": "arm64-v8a",
    "ANDROID_PLATFORM": "android-24", "ANDROID_STL": "c++_static",
    "MNN_BUILD_LLM": "ON", "MNN_BUILD_FOR_ANDROID_COMMAND": "ON",
    "MNN_BUILD_SHARED_LIBS": "OFF", "MNN_SEP_BUILD": "OFF",
    "MNN_BUILD_TEST": "OFF", "MNN_BUILD_BENCHMARK": "OFF",
    "MNN_OPENCL": "OFF", "MNN_VULKAN": "OFF",
    "LLM_SUPPORT_HTTP_RESOURCE": "OFF", "MNN_KLEIDIAI": "ON",
    "MNN_LOW_MEMORY": "ON", "MNN_ARM82": "ON", "MNN_SME2": "ON",
    "MNN_SUPPORT_QUANT_EXTEND": "ON", "MNN_SUPPORT_TRANSFORMER_FUSE": "ON",
    "MNN_USE_THREAD_POOL": "ON", "MNN_USE_LOGCAT": "ON",
    "CMAKE_EXPORT_COMPILE_COMMANDS": "ON",
}
DEFINES = "LLM_USE_JINJA MNN_BUILD_FOR_ANDROID MNN_BUILD_STATIC_LIBS MNN_KLEIDIAI_ENABLED=1 MNN_LOW_MEMORY MNN_SME2 MNN_SUPPORT_DEPRECATED_OPV2 MNN_SUPPORT_QUANT_EXTEND MNN_SUPPORT_TRANSFORMER_FUSE MNN_USE_LOGCAT MNN_USE_NEON MNN_USE_THREAD_POOL PREFILL_REUSE_DIAGNOSTIC=0".split()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hash(directory):
    rows = []
    for path in sorted(Path(directory).rglob("*")):
        if path.is_symlink():
            raise ValueError("unexpected source symlink")
        if path.is_file():
            rows.append([path.relative_to(directory).as_posix(), sha(path)])
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def extract(data, destination, *, git_archive=False):
    """No filesystem links; fixed Git symlink blobs become ordinary text files."""
    destination = Path(destination)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
        seen = set()
        for member in members:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or ".." in name.parts or "\\" in member.name
                    or ":" in member.name or not (member.isfile() or member.isdir()
                    or (git_archive and member.issym()))):
                raise ValueError("unsafe archive member: " + member.name)
            folded = str(name).casefold()
            if folded in seen:
                raise ValueError("duplicate archive member: " + member.name)
            seen.add(folded)
            target = destination.joinpath(*name.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("archive path escapes destination")
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as out:
                    out.write(member.linkname.encode() if member.issym() else archive.extractfile(member).read())


def run(records, label, args, *, cwd=None, binary=False, environment=None):
    records = Path(records)
    env = dict(os.environ)
    env.update(environment or {})
    if cwd:
        env["GIT_CEILING_DIRECTORIES"] = str(Path(cwd).parent)
    result = subprocess.run(list(map(str, args)), cwd=cwd, capture_output=True, env=env)
    if not binary:
        (records / (label + "-stdout.log")).write_bytes(result.stdout)
    (records / (label + "-stderr.log")).write_bytes(result.stderr)
    save(records / (label + "-command.json"), {
        "arguments": list(map(str, args)), "cwd": str(cwd) if cwd else None,
        "actual_exit": result.returncode, "environment": environment or {}, "stdout_bytes": len(result.stdout),
        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest()})
    if result.returncode:
        raise RuntimeError(label + " failed; see saved stderr and actual exit")
    return result.stdout


def compile_logged(records, args, *, environment=None):
    """Persist compiler output during the build, including interrupted runs."""
    records = Path(records)
    save(records / "compile-started.json", {"arguments": list(map(str, args)), "environment": environment or {}})
    with (records / "compile-stdout.log").open("xb") as out, (records / "compile-stderr.log").open("xb") as err:
        env = dict(os.environ)
        env.update(environment or {})
        process = subprocess.Popen(list(map(str, args)), stdout=out, stderr=err, env=env)
        code = process.wait()
    save(records / "compile-command.json", {"arguments": list(map(str, args)), "actual_exit": code, "environment": environment or {}})
    if code:
        raise RuntimeError("compile failed; see preserved compiler output")


def prepare(output, records, repository, ndk, cmake, ninja, kleidi, *, root=ROOT, jobs=4):
    output, records = Path(output).absolute(), Path(records).absolute()
    if output.exists() or records.exists():
        raise ValueError("output and records must both be new")
    if not str(output).isascii() or " " in str(output):
        raise ValueError("compiler output path must be ASCII without spaces")
    paths = {key: Path(value).absolute() for key, value in {
        "repository": repository, "ndk": ndk, "cmake": cmake,
        "ninja": ninja, "kleidi": kleidi}.items()}
    if not 1 <= jobs <= 16:
        raise ValueError("jobs must be 1..16")
    for key in ("cmake", "ninja", "kleidi"):
        if not paths[key].is_file():
            raise ValueError("missing " + key)
    if sha(paths["kleidi"]) != KLEIDI_SHA:
        raise ValueError("KleidiAI archive differs from fixed lock")
    ndk_props = paths["ndk"] / "source.properties"
    if f"Pkg.Revision = {NDK_REVISION}" not in ndk_props.read_text():
        raise ValueError("NDK revision differs from fixed lock")
    compiler = paths["ndk"] / "toolchains/llvm/prebuilt/windows-x86_64/bin/aarch64-linux-android24-clang++.cmd"
    if not compiler.is_file():
        raise ValueError("missing Windows NDK ARM64 compiler")
    inputs = {}
    for rel, expected in {**PATCHES, **CLIENT_SHA}.items():
        if sha(Path(root) / rel) != expected:
            raise ValueError("locked input changed: " + rel)
        inputs[rel] = expected
    # Once input validation finishes, preserve all later failures in new records.
    records.mkdir(parents=True)
    tree = run(records, "git-tree", ["git", "-C", paths["repository"], "rev-parse", COMMIT + "^{tree}"]).decode().strip()
    output.mkdir(parents=True)
    (output / "tmp").mkdir()
    archive = run(records, "git-archive", ["git", "-C", paths["repository"], "archive", "--format=tar", COMMIT], binary=True)
    source = output / "source"
    source.mkdir()
    extract(archive, source, git_archive=True)
    for rel in SOURCE_SHA:
        path = source / rel
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    # git apply accepts a plain source directory; no nested Git checkout is made.
    for number, rel in enumerate(PATCHES, 1):
        patch = output / f"patch-{number}.patch"
        patch.write_bytes((Path(root) / rel).read_bytes().replace(b"\r\n", b"\n"))
        run(records, f"patch-{number}", ["git", "apply", str(patch)], cwd=source)
    for rel, expected in SOURCE_SHA.items():
        path = source / rel
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        if sha(path) != expected:
            raise ValueError("patched source differs: " + rel)
    deps = output / "deps"
    deps.mkdir()
    extract(paths["kleidi"].read_bytes(), deps)
    kleidi_dir = deps / "kleidiai-1.16.0"
    if not (kleidi_dir / "kai/kai_common.h").is_file():
        raise ValueError("unexpected dependency layout")
    worker = output / "worker"
    worker.mkdir()
    for rel in CLIENT_SHA:
        (worker / Path(rel).name).write_bytes((Path(root) / rel).read_bytes())
    build_dir = output / "cmake-build"
    configure = [str(paths["cmake"]), "-S", str(source), "-B", str(build_dir), "-G", "Ninja",
        "-DCMAKE_MAKE_PROGRAM=" + str(paths["ninja"]),
        "-DCMAKE_TOOLCHAIN_FILE=" + str(paths["ndk"] / "build/cmake/android.toolchain.cmake"),
        "-DKLEIDIAI_SRC_DIR=" + str(kleidi_dir),
        "-DCMAKE_CXX_FLAGS=-DPREFILL_REUSE_DIAGNOSTIC=0 -I" + str(worker)]
    configure += ["-D" + key + "=" + value for key, value in OPTIONS.items()]
    link = [str(compiler)] + ["-D" + item for item in DEFINES]
    link += ["-g", "-O3", "-DNDEBUG", "-std=gnu++11", "-fPIC", "-fno-rtti", "-fno-exceptions", "-fPIE", "-pie", "-static-libstdc++"]
    for rel in ("include", "source", "express", "tools", "codegen", "schema/current", "3rd_party", "3rd_party/flatbuffers/include", "3rd_party/half", "transformers/llm/engine/include", "transformers/llm/engine/src"):
        link += ["-I" + str(source / rel)]
    link += ["-I" + str(worker), str(worker / "phone_chat_plan_worker.cpp"),
        "-Wl,--whole-archive", str(build_dir / "libMNN.a"), "-Wl,--no-whole-archive",
        "-pthread", "-llog", "-landroid", "-lm", "-latomic", "-o", str(worker / "phone_chat_plan_worker")]
    lock = {"schema": 1, "source_commit": COMMIT, "source_tree": tree,
        "source_sha256": SOURCE_SHA, "input_sha256": inputs, "kleidi_sha256": KLEIDI_SHA,
        "ndk_revision": NDK_REVISION, "ndk_properties_sha256": sha(ndk_props),
        "tools": {key: {"path": str(paths[key]), "sha256": sha(paths[key])} for key in ("cmake", "ninja")},
        "compiler": {"path": str(compiler), "sha256": sha(compiler)},
        "output": str(output), "records": str(records), "options": OPTIONS,
        "source_directory": str(source), "dependency_directory": str(deps),
        "worker_directory": str(worker), "build_directory": str(build_dir),
        "binary_path": str(worker / "phone_chat_plan_worker"),
        "environment": {"TEMP": str(output / "tmp"), "TMP": str(output / "tmp")},
        "prepared_source_tree_sha256": tree_hash(source),
        "prepared_dependency_tree_sha256": tree_hash(deps),
        "recipe_sha256": sha(__file__),
        "recipe_lock_sha256": sha(Path(root) / "configs/android/mnn-source-build/lock.json"),
        "recipe_lock_path": str(Path(root) / "configs/android/mnn-source-build/lock.json"),
        "git_symlink_policy": "ordinary text files containing Git link blobs; no OS links created",
        "commands": {"configure": configure, "compile": [str(paths["cmake"]), "--build", str(build_dir), "--target", "MNN", "--parallel", str(jobs)], "link": link},
        "compilation_performed": False, "device_commands": 0}
    for name in ("clang.exe", "ld.lld.exe", "llvm-ar.exe", "llvm-ranlib.exe"):
        tool = compiler.parent / name
        lock["tools"][name] = {"path": str(tool), "sha256": sha(tool)}
    for name, tool in (("ndk_properties", ndk_props),
                       ("ndk_toolchain", paths["ndk"] / "build/cmake/android.toolchain.cmake")):
        lock["tools"][name] = {"path": str(tool), "sha256": sha(tool)}
    save(records / "prepared.json", lock)
    return lock


def prepare_retry(prepared, expected_sha256, output, records):
    """New objects and logs, retaining only frozen, previously exported inputs."""
    prepared = Path(prepared)
    if sha(prepared) != expected_sha256:
        raise ValueError("parent plan SHA differs")
    parent = json.loads(prepared.read_text(encoding="utf-8"))
    output, records = Path(output).absolute(), Path(records).absolute()
    if output.exists() or records.exists() or not str(output).isascii() or " " in str(output):
        raise ValueError("retry needs fresh ASCII output and fresh records")
    source = Path(parent.get("source_directory", Path(parent["output"]) / "source"))
    deps = Path(parent.get("dependency_directory", Path(parent["output"]) / "deps"))
    worker = Path(parent.get("worker_directory", Path(parent["output"]) / "worker"))
    for directory in (source, deps, worker):
        if output.resolve().is_relative_to(directory.resolve()) or directory.resolve().is_relative_to(output.resolve()):
            raise ValueError("retry output overlaps frozen inputs")
    lock = copy.deepcopy(parent)
    previous_build = Path(parent.get("build_directory", Path(parent["output"]) / "cmake-build"))
    previous_binary = Path(parent.get("binary_path", worker / "phone_chat_plan_worker"))
    output.mkdir(parents=True)
    records.mkdir(parents=True)
    (output / "tmp").mkdir()
    (output / "bin").mkdir()
    build_dir = output / "cmake-build"
    binary = output / "bin/phone_chat_plan_worker"
    for key, args in lock["commands"].items():
        lock["commands"][key] = [str(binary) if arg == str(previous_binary)
            else arg.replace(str(previous_build), str(build_dir)) for arg in args]
    lock.update({"parent_prepared_sha256": expected_sha256, "parent_prepared_path": str(prepared.absolute()),
        "output": str(output), "records": str(records), "source_directory": str(source),
        "dependency_directory": str(deps), "worker_directory": str(worker),
        "build_directory": str(build_dir), "binary_path": str(binary),
        "environment": {"TEMP": str(output / "tmp"), "TMP": str(output / "tmp")},
        "recipe_sha256": sha(__file__), "compilation_performed": False,
        "retry_input_policy": "reuse frozen source/dependency/worker inputs only; full tree recheck at build; no object/library reuse"})
    save(records / "prepared.json", lock)
    return lock


def build(prepared, expected_sha256):
    prepared = Path(prepared)
    if sha(prepared) != expected_sha256:
        raise ValueError("prepared plan SHA differs from reviewed plan")
    lock = json.loads(prepared.read_text(encoding="utf-8"))
    if sha(__file__) != lock["recipe_sha256"] or sha(lock["recipe_lock_path"]) != lock["recipe_lock_sha256"]:
        raise ValueError("reviewed recipe changed; prepare a fresh plan")
    output, records = Path(lock["output"]), Path(lock["records"])
    source = Path(lock.get("source_directory", output / "source"))
    deps = Path(lock.get("dependency_directory", output / "deps"))
    worker = Path(lock.get("worker_directory", output / "worker"))
    build_dir = Path(lock.get("build_directory", output / "cmake-build"))
    binary = Path(lock.get("binary_path", output / "worker/phone_chat_plan_worker"))
    environment = lock.get("environment", {})
    if build_dir.exists():
        raise ValueError("fresh build directory required; no resume")
    if tree_hash(source) != lock["prepared_source_tree_sha256"]:
        raise ValueError("prepared source tree changed")
    if tree_hash(deps) != lock["prepared_dependency_tree_sha256"]:
        raise ValueError("prepared dependency tree changed")
    for tool in [*lock["tools"].values(), lock["compiler"]]:
        if sha(tool["path"]) != tool["sha256"]:
            raise ValueError("prepared tool changed")
    for rel, expected in SOURCE_SHA.items():
        if sha(source / rel) != expected:
            raise ValueError("prepared source changed")
    for rel, expected in CLIENT_SHA.items():
        if sha(worker / Path(rel).name) != expected:
            raise ValueError("prepared worker changed")
    run(records, "configure", lock["commands"]["configure"], environment=environment)
    cache_path = build_dir / "CMakeCache.txt"
    cache = {}
    for line in cache_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith(("//", "#")) and ":" in line and "=" in line:
            key = line.split(":", 1)[0]
            cache[key] = line.split("=", 1)[1]
    for key, value in OPTIONS.items():
        if cache.get(key) != value:
            raise ValueError("configured option differs: " + key)
    if Path(cache["KLEIDIAI_SRC_DIR"]).resolve() != (deps / "kleidiai-1.16.0").resolve():
        raise ValueError("unexpected dependency directory")
    if "MNN_KLEIDIAI_ENABLED=1" not in (build_dir / "build.ninja").read_text(encoding="utf-8"):
        raise ValueError("KleidiAI was silently disabled")
    save(records / "configured-options.json", {key: cache[key] for key in [*OPTIONS, "KLEIDIAI_SRC_DIR"]})
    compile_logged(records, lock["commands"]["compile"], environment=environment)
    run(records, "link", lock["commands"]["link"], environment=environment)
    ar_output = run(records, "archive-members", [lock["tools"]["llvm-ar.exe"]["path"], "t", build_dir / "libMNN.a"])
    members = ar_output.decode().splitlines()
    if not members:
        raise ValueError("empty core archive")
    commands = json.loads((build_dir / "compile_commands.json").read_text(encoding="utf-8"))
    ninja_log = (build_dir / ".ninja_log").read_text(encoding="utf-8")
    source_evidence = []
    for rel in SOURCE_SHA:
        rows = [row for row in commands if Path(row["file"]).resolve() == (source / rel).resolve()]
        if not rows or not any(Path(row["file"]).name + ".o" in ninja_log for row in rows):
            raise ValueError("missing actual target source compilation: " + rel)
        source_evidence.append({"source": rel, "command_entries": rows, "ninja_output_seen": True})
    for name in ("llm.cpp.o", "CPUAttention.cpp.o", "CPUBackend.cpp.o", "StaticModule.cpp.o", "tokenizer.cpp.o"):
        if name not in members:
            raise ValueError("required source missing from archive: " + name)
    save(records / "compiled-source-evidence.json", {"core_sources": source_evidence,
        "kleidiai_commands": sum("kleidiai-1.16.0" in str(row) for row in commands),
        "archive_member_count": len(members), "compile_commands_sha256": sha(build_dir / "compile_commands.json"),
        "ninja_log_sha256": sha(build_dir / ".ninja_log")})
    receipt = {"source_commit": COMMIT, "compilation_performed": True, "device_commands": 0,
        "library_sha256": sha(build_dir / "libMNN.a"),
        "binary_sha256": sha(binary),
        "prepared_sha256": expected_sha256, "actual_exits": {"configure": 0, "compile": 0, "link": 0, "archive-members": 0},
        "archive_member_count": len(members), "compile_commands_path": str(build_dir / "compile_commands.json"),
        "ninja_log_path": str(build_dir / ".ninja_log"),
        "runtime_validation": "pending", "binary_path": str(binary),
        "library_path": str(build_dir / "libMNN.a"), "diagnostic": False, "actual_exit": 0,
        "source_path": "apps/android/phone_chat_plan_worker.cpp",
        "source_sha256": CLIENT_SHA["apps/android/phone_chat_plan_worker.cpp"],
        "helper_path": "apps/android/prefill_trunk_reuse.hpp",
        "helper_sha256": CLIENT_SHA["apps/android/prefill_trunk_reuse.hpp"]}
    save(records / "built.json", receipt)
    save(records / "build.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    for key in ("output", "records", "ndk", "cmake", "ninja"):
        prep.add_argument("--" + key, required=True)
    prep.add_argument("--mnn-source", dest="repository", required=True)
    prep.add_argument("--kleidiai-archive", dest="kleidi", required=True)
    prep.add_argument("--jobs", type=int, default=4)
    compile_parser = sub.add_parser("build")
    compile_parser.add_argument("--prepared", required=True)
    compile_parser.add_argument("--expected-sha256", required=True)
    retry = sub.add_parser("prepare-retry")
    for key in ("prepared", "expected-sha256", "output", "records"):
        retry.add_argument("--" + key, required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    result = {"prepare": prepare, "build": build, "prepare-retry": prepare_retry}[command](**args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
