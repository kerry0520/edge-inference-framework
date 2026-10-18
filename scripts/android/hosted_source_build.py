"""Explicit opt-in fresh Windows source build; default only prints an offline plan.

Dependencies are fetched before calling the existing offline recipe. No models,
device commands, native uploads, profile admission, build cache or retry/resume.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "configs/android/mnn-source-build/hosted-dependencies.json"
COMMIT = "d407447ed56c4121a11ccbd266dc184ca1ead0c2"
REPOSITORY = "https://github.com/alibaba/MNN.git"


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write("\n")


def load_manifest():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != 1 or manifest.get("source_commit") != COMMIT
            or manifest.get("source_repository") != REPOSITORY
            or set(manifest.get("downloads", {})) != {"ndk", "cmake", "ninja", "kleidiai"}):
        raise ValueError("unsupported hosted dependency manifest")
    return manifest


def plan(manifest):
    return {"status": "offline_plan", "run_requested": False,
            "network_requests": 0, "subprocesses": 0, "directories_created": 0,
            "dependency_preparation_requires_network": True,
            "recipe_downloads_dependencies": False,
            "network_isolation_enforced": False,
            "source_commit": COMMIT, "source_repository": REPOSITORY,
            "tool_combination": manifest["tool_combination"],
            "downloads": manifest["downloads"],
            "candidate_status": "candidate_pending", "production_admitted": False,
            "runtime_validation": "pending", "device_commands": 0, "model_requests": 0}


def download(spec, target, *, opener=None):
    """Stream a pinned asset; retain the failed partial file for diagnosis."""
    opener = urllib.request.urlopen if opener is None else opener
    h = hashlib.new(spec["algorithm"])
    measured_sha256 = hashlib.sha256()
    count = 0
    maximum = spec.get("size") or spec["max_size"]
    if not spec["url"].startswith("https://"):
        raise ValueError("dependency URL must be HTTPS")
    with opener(spec["url"], timeout=120) as response, Path(target).open("xb") as out:
        if not response.geturl().startswith("https://"):
            raise ValueError("dependency redirect downgraded HTTPS")
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            count += len(block)
            if count > maximum:
                raise ValueError("dependency exceeds pinned size/limit")
            out.write(block)
            h.update(block)
            measured_sha256.update(block)
    observed = {"bytes": count, "algorithm": spec["algorithm"], "digest": h.hexdigest(),
                "measured_sha256": measured_sha256.hexdigest()}
    save(Path(str(target) + ".observed.json"), observed)
    if ((spec.get("size") is not None and count != spec["size"])
            or h.hexdigest() != spec["digest"]):
        raise ValueError("dependency size/digest mismatch")
    return observed


def unzip(archive, destination):
    """Preflight every member before extracting, without OS links."""
    destination = Path(destination)
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        seen, total = set(), 0
        for member in entries:
            name = PurePosixPath(member.filename)
            mode = (member.external_attr >> 16) & 0xFFFF
            parts = member.filename.rstrip("/").split("/")
            reserved = {"CON", "PRN", "AUX", "NUL", *["COM" + str(n) for n in range(1, 10)],
                        *["LPT" + str(n) for n in range(1, 10)]}
            if (name.is_absolute() or ".." in name.parts or "\\" in member.orig_filename
                    or ":" in member.filename or not name.parts
                    or "\x00" in member.orig_filename
                    or any(not p or p in (".", "..") or p.rstrip(" .") != p
                           or p.split(".")[0].upper() in reserved for p in parts)
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or member.external_attr & 0x400
                    or member.flag_bits & 1):
                raise ValueError("unsafe dependency zip member")
            folded = str(name).casefold()
            if folded in seen:
                raise ValueError("duplicate dependency zip member")
            seen.add(folded)
            total += member.file_size
            if total > 6 * 1024**3:
                raise ValueError("dependency zip expanded limit exceeded")
        destination.mkdir()
        for member in entries:
            target = destination.joinpath(*PurePosixPath(member.filename).parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as inp, target.open("xb") as out:
                for block in iter(lambda: inp.read(1024 * 1024), b""):
                    out.write(block)


def command(records, label, argv, steps, *, popen=None):
    """Child logs go straight to disk; nonzero/launch failure stops the chain."""
    popen = subprocess.Popen if popen is None else popen
    argv = list(map(str, argv))
    save(records / (label + "-started.json"), {"arguments": argv, "actual_exit": None})
    row = {"step": label, "actual_exit": None}
    steps.append(row)
    with (records / (label + "-stdout.log")).open("xb") as out:
        with (records / (label + "-stderr.log")).open("xb") as err:
            child = popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                          env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            row["actual_exit"] = child.wait()
    save(records / (label + "-command.json"), {"arguments": argv, **row})
    if type(row["actual_exit"]) is not int or row["actual_exit"] != 0:
        raise RuntimeError("child failed: " + label)
    return (records / (label + "-stdout.log")).read_bytes()


def validate_work(work):
    work = Path(work).absolute()
    if work.exists() or not str(work).isascii() or " " in str(work):
        raise ValueError("work-dir must be new, ASCII, and contain no spaces")
    for parent in (work.parent, *work.parent.parents):
        if parent.is_symlink() or (getattr(parent.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("work-dir ancestry must not traverse links")
    return work


def execute(manifest, work, summary):
    work.mkdir()
    deps, records = work / "deps", work / "records"
    deps.mkdir()
    records.mkdir()
    summary["status"] = "dependency_preparation"
    summary["downloads"] = {}
    for name, spec in manifest["downloads"].items():
        archive = deps / (name + (".tar.gz" if name == "kleidiai" else ".zip"))
        summary["downloads"][name] = download(spec, archive)
        if name != "kleidiai":
            unzip(archive, deps / name)
    ndk = deps / "ndk" / manifest["downloads"]["ndk"]["directory"]
    cmake = deps / "cmake" / manifest["downloads"]["cmake"]["directory"] / "bin/cmake.exe"
    ninja = deps / "ninja/ninja.exe"
    bindir = ndk / "toolchains/llvm/prebuilt/windows-x86_64/bin"
    ar, readelf = bindir / "llvm-ar.exe", bindir / "llvm-readelf.exe"
    if "Pkg.Revision = 30.0.16248370" not in {
            line.strip() for line in (ndk / "source.properties").read_text().splitlines()}:
        raise ValueError("NDK revision mismatch")
    if sha(ninja) != manifest["downloads"]["ninja"]["executable_sha256"]:
        raise ValueError("Ninja executable digest mismatch")
    summary["tools"] = {}
    for name, path, expected in (("cmake", cmake, "cmake version 4.1.1"),
                                 ("ninja", ninja, "1.12.1"),
                                 ("clang", bindir / "clang.exe", None),
                                 ("llvm-ar", ar, None), ("llvm-readelf", readelf, None)):
        output = command(records, "version-" + name, [path, "--version"], summary["steps"])
        version = output.decode("utf-8", errors="replace").splitlines()[0]
        if expected and version.strip() != expected:
            raise ValueError("hosted tool version mismatch: " + name)
        summary["tools"][name] = {"sha256": sha(path), "version": version[:300]}
    repository = work / "mnn"
    for label, argv in (
            ("git-init", ["git", "init", repository]),
            ("git-remote", ["git", "-C", repository, "remote", "add", "origin", REPOSITORY]),
            ("git-fetch", ["git", "-c", "credential.helper=", "-C", repository, "fetch", "--depth=1", "origin", COMMIT]),
            ("git-identity", ["git", "-C", repository, "rev-parse", "FETCH_HEAD^{commit}"])):
        result = command(records, label, argv, summary["steps"])
    if result.decode().strip() != COMMIT:
        raise ValueError("MNN fetch identity mismatch")
    recipe = ROOT / "scripts/android/build_mnn_source.py"
    prepared_dir, build_dir = work / "recipe-records", work / "build"
    py = [sys.executable, "-B"]
    command(records, "prepare", py + [recipe, "prepare", "--mnn-source", repository,
            "--ndk", ndk, "--cmake", cmake, "--ninja", ninja,
            "--kleidiai-archive", deps / "kleidiai.tar.gz", "--output", build_dir,
            "--records", prepared_dir, "--jobs", "4"], summary["steps"])
    prepared = prepared_dir / "prepared.json"
    prepared_sha = sha(prepared)
    command(records, "build", py + [recipe, "build", "--prepared", prepared,
            "--expected-sha256", prepared_sha], summary["steps"])
    built = prepared_dir / "build.json"
    build_sha = sha(built)
    data = json.loads(built.read_text(encoding="utf-8"))
    candidate_dir, audit_dir = work / "candidate", work / "audit"
    command(records, "candidate", py + [ROOT / "scripts/android/phone_source_candidate.py", "prepare",
            "--manifest", built, "--expected-sha256", build_sha, "--output", candidate_dir,
            "--binary", data["binary_path"], "--library", data["library_path"],
            "--worker", build_dir / "worker/phone_chat_plan_worker.cpp",
            "--helper", build_dir / "worker/prefill_trunk_reuse.hpp"], summary["steps"])
    descriptor = candidate_dir / "candidate.json"
    command(records, "audit", py + [ROOT / "scripts/android/phone_source_build_audit.py",
            "--prepared", prepared, "--expected-prepared-sha256", prepared_sha,
            "--build", built, "--expected-build-sha256", build_sha,
            "--output", audit_dir, "--candidate", descriptor,
            "--expected-candidate-sha256", sha(descriptor), "--llvm-ar", ar,
            "--expected-llvm-ar-sha256", sha(ar), "--llvm-readelf", readelf,
            "--expected-llvm-readelf-sha256", sha(readelf)], summary["steps"])
    audited = json.loads((audit_dir / "audit.json").read_text(encoding="utf-8"))
    check_audit(audited)
    summary.update({"status": "hosted_build_audit_passed", "compilation_performed": True,
                    "worker_sha256": data["binary_sha256"], "library_sha256": data["library_sha256"],
                    "recipe_actual_exits": data["actual_exits"],
                    "actual_object_count": audited["actual_object_count"],
                    "archive_member_count": audited["archive_member_count"],
                    "actual_kleidi_object_count": audited["actual_kleidi_object_count"],
                    "elf_needed": json.loads((audit_dir / "derived.json").read_text(encoding="utf-8"))["needed"],
                    "auditor_native_commands": audited["native_tool_commands_started"]})


def check_audit(audited):
    expected = {"status": "build_audit_passed", "candidate_status": "candidate_pending",
                "production_admitted": False, "runtime_validation": "pending", "device_commands": 0,
                "model_requests": 0, "actual_object_count": 565, "archive_member_count": 565,
                "actual_kleidi_object_count": 101, "native_tool_commands_started": 4}
    if any(type(audited.get(k)) is not type(v) or audited.get(k) != v for k, v in expected.items()):
        raise ValueError("unexpected hosted source audit result")


def print_failure_tails(work, steps):
    """Only this run's last child plus a proven failed inner recipe phase."""
    if not steps:
        return
    label = steps[-1]["step"]
    pairs = [(work / "records", label)]
    if label == "build":
        for phase in ("configure", "compile", "link", "archive-members"):
            metadata = work / "recipe-records" / (phase + "-command.json")
            if metadata.is_file():
                try:
                    actual_exit = json.loads(metadata.read_text(encoding="utf-8"))["actual_exit"]
                except (OSError, ValueError, KeyError):
                    continue
                if type(actual_exit) is int and actual_exit != 0:
                    pairs.append((metadata.parent, phase))
                    break
    for directory, phase in pairs:
        for suffix in ("stdout", "stderr"):
            path = directory / (phase + "-" + suffix + ".log")
            if path.is_file():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 8192))
                    print("HOSTED_FAILURE_TAIL " + phase + " " + suffix + "\n"
                          + stream.read().decode("utf-8", errors="replace"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--work-dir")
    args = parser.parse_args(argv)
    manifest = load_manifest()
    if not args.run:
        print(json.dumps(plan(manifest), ensure_ascii=True))
        return 0
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        parser.error("--run requires a Windows x86_64 host")
    if not args.work_dir:
        parser.error("--run requires --work-dir")
    work = validate_work(args.work_dir)
    summary = {"schema_version": 1, "status": "starting", "steps": [],
               "source_commit": COMMIT, "tool_combination": manifest["tool_combination"],
               "candidate_status": "candidate_pending", "production_admitted": False,
               "runtime_validation": "pending", "device_commands": 0, "model_requests": 0,
               "network_isolation_enforced": False, "artifacts_uploaded": False,
               "manifest_sha256": sha(MANIFEST), "runner_sha256": sha(__file__)}
    code = 0
    try:
        execute(manifest, work, summary)
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile) as error:
        code = 1
        summary["status"] = "failed"
        summary["error"] = str(error)[:1000]
        # Full records live only on the ephemeral runner. Bounded tails survive in Actions logs.
        print_failure_tails(work, summary["steps"])
    if work.is_dir():
        save(work / "summary.json", summary)
    print("HOSTED_BUILD_SUMMARY " + json.dumps(summary, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
