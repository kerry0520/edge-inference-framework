"""Audit a fixed Android source build offline; never build, deploy, or admit it.

The four native commands are read-only ar/readelf inspections. Existing build
logs are evidence, not a cryptographic attestation of the whole source/sysroot.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

import build_mnn_source as recipe
import phone_source_candidate as candidate

ROOT = Path(__file__).resolve().parents[2]
RECIPE_SHA256 = "4bee47bee8b50a81a7775c4266f43030b7971f5bef327c408a195659019b7bb9"
LOCK_SHA256 = "9bc0ed4bd0e181d6503e80c0148fd86187485ec2581aad7120d19d79e4ec4bad"
TOOL_KEYS = {"cmake", "ninja", "clang.exe", "ld.lld.exe", "llvm-ar.exe",
             "llvm-ranlib.exe", "ndk_properties", "ndk_toolchain"}
INCLUDES = ("include", "source", "express", "tools", "codegen", "schema/current",
            "3rd_party", "3rd_party/flatbuffers/include", "3rd_party/half",
            "transformers/llm/engine/include", "transformers/llm/engine/src")
SYSTEM_NEEDED = {"liblog.so", "libandroid.so", "libm.so", "libdl.so", "libc.so"}


class AuditError(ValueError):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


def require(condition, message):
    if not condition:
        raise ValueError(message)


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def canonical(value):
    require(type(value) is str and bool(value.strip()), "invalid recorded path")
    return Path(value).resolve(strict=True)


class Evidence:
    """Bind all consumed files before/after; historical aliases resolve first."""
    def __init__(self):
        self.files = {}
        self.aliases = []

    def location(self, value):
        path = canonical(str(value))
        if Path(value).absolute() != path:
            row = {"recorded": str(value), "resolved": str(path)}
            if row not in self.aliases:
                self.aliases.append(row)
        return path

    def directory(self, value):
        path = self.location(value)
        require(path.is_dir(), "evidence directory missing")
        return path

    def file(self, value, expected=None):
        path = self.location(value)
        candidate.plain(str(path))
        require(path.is_file(), "evidence is not a file")
        digest = candidate.sha(path)
        if expected is not None:
            require(candidate.valid_hash(expected) and digest == expected, "evidence SHA differs: " + str(path))
        require(str(path) not in self.files or self.files[str(path)] == digest, "evidence changed during audit")
        self.files[str(path)] = digest
        return path

    def data(self, value):
        path = self.file(value)
        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=candidate.pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON constant")))
        return data

    def unchanged(self):
        require(all(canonical(row["recorded"]) == Path(row["resolved"]) for row in self.aliases),
                "historical path alias changed during audit")
        require(all(candidate.sha(candidate.plain(path)) == digest for path, digest in self.files.items()),
                "evidence changed before audit completed")


def commands(plan):
    """Reconstruct the fixed recipe, never execute these recorded commands."""
    source = Path(plan["source_directory"])
    deps = Path(plan["dependency_directory"])
    worker = Path(plan["worker_directory"])
    build = Path(plan["build_directory"])
    tools = plan["tools"]
    configure = [tools["cmake"]["path"], "-S", str(source), "-B", str(build), "-G", "Ninja",
                 "-DCMAKE_MAKE_PROGRAM=" + tools["ninja"]["path"],
                 "-DCMAKE_TOOLCHAIN_FILE=" + tools["ndk_toolchain"]["path"],
                 "-DKLEIDIAI_SRC_DIR=" + str(deps / "kleidiai-1.16.0"),
                 "-DCMAKE_CXX_FLAGS=-DPREFILL_REUSE_DIAGNOSTIC=0 -I" + str(worker)]
    configure += ["-D" + key + "=" + value for key, value in recipe.OPTIONS.items()]
    recorded_compile = plan["commands"]["compile"]
    require(type(recorded_compile) is list and len(recorded_compile) == 7,
            "invalid compile command shape")
    jobs = recorded_compile[-1]
    require(type(jobs) is str and re.fullmatch(r"[1-9][0-9]*", jobs) is not None,
            "invalid build parallel count")
    compile_argv = [tools["cmake"]["path"], "--build", str(build), "--target", "MNN", "--parallel", jobs]
    link = [plan["compiler"]["path"]] + ["-D" + item for item in recipe.DEFINES]
    link += ["-g", "-O3", "-DNDEBUG", "-std=gnu++11", "-fPIC", "-fno-rtti", "-fno-exceptions",
             "-fPIE", "-pie", "-static-libstdc++"]
    link += ["-I" + str(source / rel) for rel in INCLUDES]
    link += ["-I" + str(worker), str(worker / "phone_chat_plan_worker.cpp"),
             "-Wl,--whole-archive", str(build / "libMNN.a"), "-Wl,--no-whole-archive",
             "-pthread", "-llog", "-landroid", "-lm", "-latomic", "-o", plan["binary_path"]]
    return {"configure": configure, "compile": compile_argv, "link": link}


def check_plan(plan, evidence, *, root=ROOT, depth=0):
    require(depth <= 8, "retry ancestry exceeds audit limit")
    require(type(plan) is dict and type(plan.get("schema")) is int and plan["schema"] == 1,
            "unsupported prepared schema")
    require(plan["source_commit"] == recipe.COMMIT and plan["source_sha256"] == recipe.SOURCE_SHA,
            "fixed source identity differs")
    require(plan["input_sha256"] == {**recipe.PATCHES, **recipe.CLIENT_SHA}, "fixed client/patch inputs differ")
    require(plan["options"] == recipe.OPTIONS and plan["kleidi_sha256"] == recipe.KLEIDI_SHA
            and plan["ndk_revision"] == recipe.NDK_REVISION, "fixed build configuration differs")
    require(plan["compilation_performed"] is False and type(plan["device_commands"]) is int
            and plan["device_commands"] == 0, "prepared execution state differs")
    require(plan["recipe_sha256"] == RECIPE_SHA256 and plan["recipe_lock_sha256"] == LOCK_SHA256,
            "unsupported recipe or lock")
    evidence.file(root / "scripts/android/build_mnn_source.py", RECIPE_SHA256)
    evidence.file(root / "configs/android/mnn-source-build/lock.json", LOCK_SHA256)
    evidence.file(plan["recipe_lock_path"], LOCK_SHA256)
    require(set(plan["tools"]) == TOOL_KEYS, "unknown prepared tool set")
    for row in [*plan["tools"].values(), plan["compiler"]]:
        require(type(row) is dict and set(row) == {"path", "sha256"}, "invalid prepared tool record")
        evidence.file(row["path"], row["sha256"])
    require(plan["ndk_properties_sha256"] == plan["tools"]["ndk_properties"]["sha256"], "NDK properties pin differs")
    ndk = canonical(plan["tools"]["ndk_properties"]["path"]).parent
    compiler_bin = ndk / "toolchains/llvm/prebuilt/windows-x86_64/bin"
    require(canonical(plan["tools"]["ndk_properties"]["path"]) == ndk / "source.properties"
            and canonical(plan["tools"]["ndk_toolchain"]["path"]) == ndk / "build/cmake/android.toolchain.cmake"
            and canonical(plan["compiler"]["path"]) == compiler_bin / "aarch64-linux-android24-clang++.cmd",
            "NDK toolchain/compiler layout differs")
    require(all(canonical(plan["tools"][name]["path"]) == compiler_bin / name
                for name in ("clang.exe", "ld.lld.exe", "llvm-ar.exe", "llvm-ranlib.exe")),
            "NDK tools do not share the fixed compiler directory")
    source, deps, worker, build, output = [evidence.directory(plan[key]) for key in
        ("source_directory", "dependency_directory", "worker_directory", "build_directory", "output")]
    require(build == output / "cmake-build", "unexpected build directory layout")
    require(all(not build.is_relative_to(p) and not p.is_relative_to(build)
                for p in (source, deps, worker)), "build overlaps frozen inputs")
    require(plan["environment"] == {"TEMP": str(Path(plan["output"]) / "tmp"),
                                     "TMP": str(Path(plan["output"]) / "tmp")}, "TEMP/TMP layout differs")
    require(all(value.isascii() and " " not in value for value in plan["environment"].values()),
            "recipe requires ASCII TEMP/TMP without spaces")
    for rel, digest in recipe.SOURCE_SHA.items():
        evidence.file(source / rel, digest)
    for rel, digest in plan["input_sha256"].items():
        evidence.file(root / rel, digest)
        if rel.startswith("apps/"):
            evidence.file(worker / Path(rel).name, digest)
    require(set(plan["commands"]) == {"configure", "compile", "link"}
            and plan["commands"] == commands(plan), "commands differ from fixed recipe")
    if "parent_prepared_path" in plan or "parent_prepared_sha256" in plan:
        parentpath = evidence.file(plan["parent_prepared_path"], plan["parent_prepared_sha256"])
        parent = evidence.data(parentpath)
        check_plan(parent, evidence, root=root, depth=depth + 1)
        retained = ("source_directory", "dependency_directory", "worker_directory", "prepared_source_tree_sha256",
                    "prepared_dependency_tree_sha256", "source_sha256", "input_sha256", "tools", "compiler", "options")
        require(all(plan[k] == parent[k] for k in retained), "retry changed frozen inputs")
        require(output != canonical(parent["output"]), "retry reuses parent output")
        require(Path(plan["binary_path"]).resolve() == output / "bin/phone_chat_plan_worker", "retry binary layout differs")
        moved = {key: [plan["binary_path"] if arg == parent["binary_path"] else
                        arg.replace(parent["build_directory"], plan["build_directory"]) for arg in argv]
                 for key, argv in parent["commands"].items()}
        require(plan["commands"] == moved, "retry is not exact output relocation")
    else:
        require(source == output / "source" and deps == output / "deps" and worker == output / "worker",
                "fresh input layout differs")
        require(Path(plan["binary_path"]).resolve() == worker / "phone_chat_plan_worker", "fresh binary layout differs")
    return source, deps, worker, build, output


def raw_command(evidence, records, label, expected, environment):
    row = evidence.data(records / (label + "-command.json"))
    require(type(row.get("actual_exit")) is int and row["actual_exit"] == 0,
            label + " recorded actual exit is not zero")
    require(row.get("arguments") == expected and row.get("environment") == environment,
            label + " recorded command/environment differs")
    require(row.get("cwd") is None, label + " unexpected working directory")
    stdout = evidence.file(records / (label + "-stdout.log")).read_bytes()
    evidence.file(records / (label + "-stderr.log"))
    if label != "compile":
        require(type(row.get("stdout_bytes")) is int and row["stdout_bytes"] == len(stdout)
                and row.get("stdout_sha256") == hashlib.sha256(stdout).hexdigest(), label + " stdout binding differs")
    return stdout


def windows_tokens(command):
    """Split Windows compiler command lines, retaining ordinary backslashes."""
    tokens, current = [], []
    quoted = False
    index = 0
    while index < len(command):
        char = command[index]
        if char.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
            index += 1
        elif char == "\\":
            end = index
            while end < len(command) and command[end] == "\\":
                end += 1
            count = end - index
            if end < len(command) and command[end] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                else:
                    quoted = not quoted
                index = end + 1
            else:
                current.append("\\" * count)
                index = end
        elif char == '"':
            quoted = not quoted
            index += 1
        else:
            current.append(char)
            index += 1
    require(not quoted, "unclosed quote in Windows compiler command")
    if current:
        tokens.append("".join(current))
    return tokens


def exact_define(tokens, name, expected):
    values = []
    for index, token in enumerate(tokens):
        undefinition = (tokens[index + 1] if index + 1 < len(tokens) else "") if token == "-U" else token[2:] if token.startswith("-U") else ""
        require(undefinition != name, "compile undefines required macro: " + name)
        definition = (tokens[index + 1] if index + 1 < len(tokens) else "") if token == "-D" else token[2:] if token.startswith("-D") else ""
        macro, separator, value = definition.partition("=")
        if macro == name:
            values.append(value if separator else None)
    require(bool(values) and all(value == expected for value in values),
            "compile define differs: " + name)


def actual_objects(evidence, build, source, deps, members):
    ninja = evidence.file(build / "build.ninja").read_text(encoding="utf-8")
    # The fixed recipe emits relative object names without spaces. Join Ninja
    # continuations, and fail closed on other escapes/variables in this target.
    ninja = re.sub(r"\$\r?\n[ \t]*", "", ninja)
    target = [line for line in ninja.splitlines() if line.startswith("build libMNN.a:")]
    require(len(target) == 1 and "$" not in target[0], "unsupported or ambiguous MNN archive Ninja target")
    match = re.fullmatch(r"build libMNN\.a: CXX_STATIC_LIBRARY_LINKER__MNN_Release (.+)", target[0])
    require(match is not None, "unexpected archive rule")
    expected_tokens = match[1].split(" || ", 1)[0].split()
    require(bool(expected_tokens) and all(value.endswith(".o") and "|" not in value for value in expected_tokens),
            "unsupported archive object dependency syntax")
    target_objects = [(build / value.replace("\\", "/")).resolve() for value in expected_tokens]
    require(len(set(target_objects)) == len(target_objects)
            and all(path.is_relative_to(build) for path in target_objects), "invalid archive object dependency paths")
    cc = evidence.data(build / "compile_commands.json")
    require(type(cc) is list and bool(cc), "empty compilation database")
    entries = {}
    for row in cc:
        directory = evidence.directory(row["directory"])
        require(directory == build, "compilation database uses another build")
        file = Path(row["file"])
        file = (file if file.is_absolute() else directory / file).resolve()
        require(file.is_relative_to(source) or file.is_relative_to(deps), "compile source outside frozen inputs")
        command = row.get("command")
        if command is None:
            require(type(row.get("arguments")) is list and all(type(value) is str for value in row["arguments"]), "missing compile argv")
            tokens = row["arguments"]
        else:
            require(type(command) is str, "invalid compile command")
            tokens = windows_tokens(command)
        require(tokens.count("-o") == 1 and tokens.index("-o") + 1 < len(tokens), "compile output missing or repeated")
        output = Path(tokens[tokens.index("-o") + 1].replace("\\", "/"))
        output = (output if output.is_absolute() else directory / output).resolve()
        require(output.is_relative_to(build) and output.suffix == ".o", "compile output outside current object tree")
        if "output" in row:
            require(Path(row["output"]).resolve() == output, "compile output metadata differs")
        entries.setdefault(output, []).append({"source": file, "tokens": tokens})
    text = evidence.file(build / ".ninja_log").read_text(encoding="utf-8")
    lines = text.splitlines()
    require(bool(lines) and re.fullmatch(r"# ninja log v[56]", lines[0]) is not None, "invalid ninja log header")
    objects = []
    archive_seen = 0
    all_outputs = []
    for line in lines[1:]:
        cells = line.split("\t")
        require(len(cells) == 5, "invalid ninja log row")
        start, end = int(cells[0]), int(cells[1])
        require(0 <= start <= end and int(cells[2]) >= 0, "invalid ninja times")
        output = Path(cells[3].replace("\\", "/"))
        output = (output if output.is_absolute() else build / output).resolve()
        require(output.is_relative_to(build), "ninja output escapes current build")
        all_outputs.append(output)
        if output.suffix == ".o":
            require(output in entries and len(entries[output]) == 1, "actual object lacks unique compile command")
            evidence.file(output)
            objects.append(output)
        elif output == build / "libMNN.a":
            archive_seen += 1
    require(bool(objects) and len(set(all_outputs)) == len(all_outputs) and archive_seen == 1,
            "not a single nonempty object/archive build log")
    require(Counter(objects) == Counter(target_objects), "actual objects do not cover the complete archive target")
    require(Counter(p.name for p in objects) == Counter(members), "archive does not match actual object multiplicities")
    five = []
    kai = []
    for rel in recipe.SOURCE_SHA:
        matching = [p for p in objects if entries[p][0]["source"] == source / rel]
        require(len(matching) == 1, "changed source lacks unique actual object: " + rel)
        tokens = entries[matching[0]][0]["tokens"]
        exact_define(tokens, "PREFILL_REUSE_DIAGNOSTIC", "0")
        exact_define(tokens, "MNN_KLEIDIAI_ENABLED", "1")
        five.append({"source": rel, "output": str(matching[0].relative_to(build))})
    for output in objects:
        row = entries[output][0]
        if row["source"].is_relative_to(deps / "kleidiai-1.16.0"):
            exact_define(row["tokens"], "MNN_KLEIDIAI_ENABLED", "1")
            kai.append(str(output.relative_to(build)))
    require(bool(kai), "no actual Kleidi objects")
    return {"compile_commands_count": len(cc), "target_object_count": len(target_objects), "actual_object_count": len(objects),
            "archive_member_count": len(members), "actual_kleidi_object_count": len(kai),
            "actual_object_name_counts": dict(Counter(p.name for p in objects)),
            "archive_member_name_counts": dict(Counter(members)), "five_changed_sources": five,
            "kleidi_objects": kai}


def inspect_elf(header, dynamic, segments):
    require("ELF64" in header and "AArch64" in header and re.search(r"Type:\s+DYN", header),
            "binary is not ELF64 AArch64 DYN")
    require(re.search(r"FLAGS_1.*PIE", dynamic) is not None, "binary lacks PIE flag")
    require("Requesting program interpreter: /system/bin/linker64" in segments, "unexpected program interpreter")
    needed = re.findall(r"\(NEEDED\).*?\[([^]]+)\]", dynamic)
    require(bool(needed) and set(needed) <= SYSTEM_NEEDED, "unexpected dynamic library dependency")
    return needed


def tool(output, label, argv, state):
    state["native_tool_commands_started"] += 1
    result = None
    try:
        result = subprocess.run([str(value) for value in argv], capture_output=True, timeout=60, shell=False)
        stdout, stderr, code, timed_out = result.stdout, result.stderr, result.returncode, False
    except subprocess.TimeoutExpired as error:
        stdout, stderr, code, timed_out = error.stdout or b"", error.stderr or b"", None, True
    except OSError as error:
        stdout, stderr, code, timed_out = b"", str(error).encode(), None, False
    (output / (label + "-stdout.log")).write_bytes(stdout)
    (output / (label + "-stderr.log")).write_bytes(stderr)
    save(output / (label + "-command.json"), {"arguments": [str(value) for value in argv],
         "actual_exit": code, "timed_out": timed_out})
    require(type(code) is int and code == 0, label + " native inspection failed")
    return stdout.decode("utf-8", errors="strict")


def audit(args, output, state, *, root=ROOT, tool_runner=None, write_receipt=True):
    runner = tool if tool_runner is None else tool_runner
    evidence = Evidence()
    prepared_path, plan = candidate.read_pinned(args.prepared, args.expected_prepared_sha256)
    evidence.file(prepared_path, args.expected_prepared_sha256)
    if not Path(args.build).exists():
        raise AuditError("missing_build_receipt", "build receipt is missing; no build success can be audited")
    build_path, built = candidate.read_pinned(args.build, args.expected_build_sha256)
    evidence.file(build_path, args.expected_build_sha256)
    source, deps, worker, build, _ = check_plan(plan, evidence, root=root)
    require(candidate.receipt_kind(built) == "recipe_receipt", "only modern recipe builds are supported")
    require(built["prepared_sha256"] == args.expected_prepared_sha256 and built["compilation_performed"] is True
            and type(built["actual_exit"]) is int and built["actual_exit"] == 0
            and all(type(value) is int and value == 0 for value in built["actual_exits"].values())
            and type(built["device_commands"]) is int and built["device_commands"] == 0, "unsuccessful build receipt")
    records = evidence.directory(plan["records"])
    require(build_path == records / "build.json", "build receipt outside prepared records")
    binary = evidence.file(plan["binary_path"], built["binary_sha256"])
    library = evidence.file(build / "libMNN.a", built["library_sha256"])
    require(canonical(built["binary_path"]) == binary and canonical(built["library_path"]) == library
            and canonical(built["compile_commands_path"]) == build / "compile_commands.json"
            and canonical(built["ninja_log_path"]) == build / ".ninja_log", "build receipt paths differ")
    ar = candidate.plain(args.llvm_ar)
    readelf = candidate.plain(args.llvm_readelf)
    evidence.file(ar, args.expected_llvm_ar_sha256)
    evidence.file(readelf, args.expected_llvm_readelf_sha256)
    require(candidate.sha(ar) == plan["tools"]["llvm-ar.exe"]["sha256"], "ar differs from prepared tool")
    for label in ("configure", "compile", "link"):
        raw_command(evidence, records, label, plan["commands"][label], plan["environment"])
    recorded_members = raw_command(evidence, records, "archive-members",
        [plan["tools"]["llvm-ar.exe"]["path"], "t", str(Path(plan["build_directory"]) / "libMNN.a")], {})
    configured = evidence.data(records / "configured-options.json")
    cache = {}
    for line in evidence.file(build / "CMakeCache.txt").read_text(encoding="utf-8").splitlines():
        if not line.startswith(("//", "#")) and ":" in line and "=" in line:
            key = line.split(":", 1)[0]
            require(key not in cache, "duplicate CMake cache key")
            cache[key] = line.split("=", 1)[1]
    require(all(configured.get(key) == value and cache.get(key) == value for key, value in recipe.OPTIONS.items()),
            "actual configured options differ")
    require(canonical(configured["KLEIDIAI_SRC_DIR"]) == deps / "kleidiai-1.16.0"
            and canonical(cache["KLEIDIAI_SRC_DIR"]) == deps / "kleidiai-1.16.0", "configured dependency path differs")
    require(cache.get("CMAKE_GENERATOR") == "Ninja", "configured generator differs")
    cache_paths = {"CMAKE_HOME_DIRECTORY": source, "CMAKE_CACHEFILE_DIR": build,
                   "CMAKE_MAKE_PROGRAM": canonical(plan["tools"]["ninja"]["path"]),
                   "CMAKE_TOOLCHAIN_FILE": canonical(plan["tools"]["ndk_toolchain"]["path"])}
    for key, expected in cache_paths.items():
        require(evidence.location(cache[key]) == expected, "configured path differs: " + key)
    ndk = canonical(plan["tools"]["ndk_properties"]["path"]).parent
    for key in ("ANDROID_NDK", "CMAKE_ANDROID_NDK"):
        if key in cache:
            require(evidence.location(cache[key]) == ndk, "configured NDK differs")
    for key, tool_name in (("CMAKE_CXX_COMPILER_AR", "llvm-ar.exe"), ("CMAKE_C_COMPILER_AR", "llvm-ar.exe"),
                           ("CMAKE_CXX_COMPILER_RANLIB", "llvm-ranlib.exe"), ("CMAKE_C_COMPILER_RANLIB", "llvm-ranlib.exe")):
        require(evidence.location(cache[key]) == canonical(plan["tools"][tool_name]["path"]),
                "configured compiler tool differs: " + key)
    members = recorded_members.decode("utf-8").splitlines()
    require(bool(members) and all(Path(name).name == name and name.endswith(".o") for name in members),
            "invalid archive members")
    require(built["archive_member_count"] == len(members), "receipt archive count differs")
    derived = actual_objects(evidence, build, source, deps, members)
    assets = {"binary": {"path": str(binary), "sha256": built["binary_sha256"]},
              "library": {"path": str(library), "sha256": built["library_sha256"]}}
    for role, rel in (("worker", candidate.WORKER), ("helper", candidate.HELPER)):
        path = evidence.file(worker / Path(rel).name, recipe.CLIENT_SHA[rel])
        assets[role] = {"path": str(path), "sha256": recipe.CLIENT_SHA[rel]}
    require(bool(args.candidate) == bool(args.expected_candidate_sha256), "candidate and external pin must be supplied together")
    if args.candidate:
        descriptor = candidate.inspect(args.candidate, args.expected_candidate_sha256)
        evidence.file(args.candidate, args.expected_candidate_sha256)
        require(descriptor["manifest"] == {"path": str(build_path), "sha256": args.expected_build_sha256}
                and descriptor["assets"] == assets, "candidate does not bind this build and four assets")
    # Recheck before running the only permitted native commands as well as after.
    evidence.unchanged()
    observed_members = runner(output, "archive-members", [ar, "t", library], state).splitlines()
    require(observed_members == members, "current archive members differ from original raw record")
    header = runner(output, "elf-header", [readelf, "-h", binary], state)
    dynamic = runner(output, "elf-dynamic", [readelf, "-d", binary], state)
    segments = runner(output, "elf-segments", [readelf, "-l", binary], state)
    derived["needed"] = inspect_elf(header, dynamic, segments)
    evidence.unchanged()
    save(output / "derived.json", derived)
    save(output / "input-bindings.json", {"files": evidence.files, "historical_path_aliases": evidence.aliases})
    result = {"schema_version": 1, "kind": "source_build_audit", "status": "build_audit_passed",
              "candidate_status": "candidate_pending", "production_admitted": False,
              "runtime_validation": "pending", "compilation_performed_by_auditor": False,
              "device_commands": 0, "model_requests": 0, **state,
              "prepared_sha256": args.expected_prepared_sha256, "build_sha256": args.expected_build_sha256,
              "assets": assets, "actual_object_count": derived["actual_object_count"],
              "archive_member_count": derived["archive_member_count"],
              "actual_kleidi_object_count": derived["actual_kleidi_object_count"],
              "scope": "Fixed recipe, selected source/client bytes and existing build evidence; no full tree/sysroot audit, device correctness, or performance claim."}
    save(output / "audit.json", result)
    if write_receipt and tool_runner is None:
        from phone_source_build_audit_replay import create_receipt
        create_receipt(args, output)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("prepared", "expected-prepared-sha256", "build", "expected-build-sha256", "output",
                "llvm-ar", "expected-llvm-ar-sha256", "llvm-readelf", "expected-llvm-readelf-sha256"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--candidate")
    parser.add_argument("--expected-candidate-sha256")
    args = parser.parse_args(argv)
    output = Path(args.output).absolute()
    # Validate the new output's ancestry without touching previous evidence.
    try:
        require(not output.exists() and not output.is_symlink(), "output exists; preserve previous evidence")
        for component in (output.parent, *output.parent.parents):
            require(not component.is_symlink() and not (getattr(component.lstat(), "st_file_attributes", 0) & 0x400),
                    "output ancestry contains a link")
        output = output.resolve()
        _, preflight_plan = candidate.read_pinned(args.prepared, args.expected_prepared_sha256)
        for key in ("output", "records", "source_directory", "dependency_directory", "worker_directory", "build_directory"):
            protected = Path(preflight_plan[key]).resolve()
            require(not output.is_relative_to(protected) and not protected.is_relative_to(output),
                    "report output overlaps build inputs or original evidence")
        output.mkdir()
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(str(error))
        return 1
    state = {"native_tool_commands_started": 0}
    try:
        result = audit(args, output, state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
        save(output / "audit-failure.json", {"status": "build_audit_failed", "error": str(error),
             "failure_class": getattr(error, "category", "audit_rejected"),
             "production_admitted": False, "compilation_performed_by_auditor": False,
             "device_commands": 0, "model_requests": 0, **state,
             "scope": "Audit rejection does not by itself classify an actual compilation or model failure."})
        print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
