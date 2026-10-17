"""Prepare an isolated sample-boundary harness link plan. Never compile or use ADB.

The existing fixed production archive is an externally pinned input. This does
not promote the diagnostic executable to the production profile registry.
"""
import argparse
import hashlib
import json
import stat
from pathlib import Path

import build_mnn_source as recipe

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests/android/plan_sample_logits.cpp"
# Root checked these exact CRLF bytes against fixed-commit git blobs: only LF/CRLF differs.
# This is a targeted boundary check, not full-tree or Android sysroot attestation.
BOUNDARY_SOURCE_SHA = {
    "transformers/llm/engine/include/llm/llm.hpp": "6ae9c5e405de20e92b91dc49ab86dbd2e3b1774d3755ff60034a5e1727108d61",
    "transformers/llm/engine/src/llmconfig.hpp": "b26132e61bbf12344ecd36e146595b5ea68f99ca9f3301bdf37ed9d78bf6a81a",
    "transformers/llm/engine/src/sampler.cpp": "24df24b027363103828649c37b41f7a858e97046b5c66d7ed5ecb6f639e0fe2d",
    "transformers/llm/engine/src/speculative_decoding/generate.cpp": "ba23fce581bf4c006ba55f8f305dc71502b5783fd55d93f265245351e85d616d",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checked(path, expected):
    path = Path(path).absolute()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("invalid external SHA-256")
    if not path.is_file() or sha(path) != expected:
        raise ValueError("input missing or SHA differs: " + str(path))
    return path


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)


def prepare(args):
    prepared_file = checked(args.prepared, args.expected_prepared_sha256)
    build_file = checked(args.build, args.expected_build_sha256)
    library = checked(args.library, args.expected_library_sha256)
    compiler = checked(args.compiler, args.expected_compiler_sha256)
    harness = checked(HARNESS, args.expected_harness_sha256)
    prepared, build = load(prepared_file), load(build_file)
    if (prepared.get("source_commit") != recipe.COMMIT or
            prepared.get("source_sha256") != recipe.SOURCE_SHA or
            prepared.get("input_sha256") != {**recipe.PATCHES, **recipe.CLIENT_SHA} or
            prepared.get("options") != recipe.OPTIONS or
            prepared.get("recipe_sha256") != sha(recipe.__file__) or
            prepared.get("recipe_lock_sha256") != sha(ROOT / "configs/android/mnn-source-build/lock.json")):
        raise ValueError("not the fixed source recipe")
    if (build.get("prepared_sha256") != args.expected_prepared_sha256 or
            build.get("source_commit") != recipe.COMMIT or
            build.get("diagnostic") is not False or
            build.get("compilation_performed") is not True or
            type(build.get("actual_exit")) is not int or build["actual_exit"] != 0 or
            not build.get("actual_exits") or any(type(v) is not int or v != 0 for v in build["actual_exits"].values()) or
            type(build.get("archive_member_count")) is not int or build["archive_member_count"] <= 0 or
            build.get("library_sha256") != args.expected_library_sha256 or
            Path(build.get("library_path", "")).resolve() != library.resolve() or
            prepared.get("compiler", {}).get("sha256") != args.expected_compiler_sha256 or
            Path(prepared["compiler"]["path"]).resolve() != compiler.resolve()):
        raise ValueError("build/archive/compiler binding differs")
    source = Path(prepared["source_directory"])
    worker = Path(prepared["worker_directory"])
    bindings = []

    def bind(path, expected=None):
        path = Path(path).absolute()
        value = sha(path)
        if expected is not None and value != expected:
            raise ValueError("frozen input differs: " + str(path))
        bindings.append({"path": str(path), "resolved_path": str(path.resolve()), "sha256": value})

    for path, expected in ((prepared_file, args.expected_prepared_sha256),
                           (build_file, args.expected_build_sha256),
                           (library, args.expected_library_sha256),
                           (compiler, args.expected_compiler_sha256),
                           (harness, args.expected_harness_sha256)):
        bind(path, expected)
    for relative, expected in recipe.SOURCE_SHA.items():
        bind(source / relative, expected)
    for relative, expected in recipe.CLIENT_SHA.items():
        bind(worker / Path(relative).name, expected)
        bind(ROOT / relative, expected)
    for relative, expected in BOUNDARY_SOURCE_SHA.items():
        bind(source / relative, expected)
    bind(recipe.__file__)
    bind(__file__)
    for name in ("clang.exe", "ld.lld.exe"):
        entry = prepared["tools"][name]
        bind(compiler.parent / name, entry["sha256"])
    output = Path(args.output).absolute()
    if output.exists() or not output.parent.is_dir() or not str(output).isascii() or " " in str(output):
        raise ValueError("output must be a fresh ASCII path, without spaces, under an existing parent")
    for ancestor in [output.parent, *output.parent.parents]:
        if ancestor.is_symlink() or (getattr(ancestor.stat(), "st_file_attributes", 0) &
                                     getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("output ancestry must not contain links")
    protected = [source, worker, Path(prepared["dependency_directory"]),
                 Path(prepared["build_directory"]), prepared_file.parent, build_file.parent,
                 HARNESS.parent, Path(__file__).parent, library.parent, compiler.parent]
    for path in protected:
        left, right = output.resolve(), path.resolve()
        if left.is_relative_to(right) or right.is_relative_to(left):
            raise ValueError("output overlaps an input tree")
    # Construct from the current fixed recipe constants, never execute recorded argv.
    copied = output / "plan_sample_logits.cpp"
    binary = output / "plan_sample_logits"
    command = [str(compiler)] + ["-D" + item for item in recipe.DEFINES]
    command += ["-g", "-O3", "-DNDEBUG", "-std=gnu++11", "-fPIC", "-fno-rtti", "-fno-exceptions", "-fPIE", "-pie", "-static-libstdc++"]
    for relative in ("include", "source", "express", "tools", "codegen", "schema/current", "3rd_party", "3rd_party/flatbuffers/include", "3rd_party/half", "transformers/llm/engine/include", "transformers/llm/engine/src"):
        command += ["-I" + str(source / relative)]
    command += ["-I" + str(worker), str(copied), "-Wl,--whole-archive", str(library),
                "-Wl,--no-whole-archive", "-pthread", "-llog", "-landroid", "-lm", "-latomic", "-o", str(binary)]
    output.mkdir()
    copied.write_bytes(harness.read_bytes())
    if sha(copied) != args.expected_harness_sha256:
        raise ValueError("harness changed while copying")
    # Recheck all inputs and aliases after the only input copy.
    for entry in bindings:
        if str(Path(entry["path"]).resolve()) != entry["resolved_path"] or sha(entry["path"]) != entry["sha256"]:
            raise ValueError("input changed while preparing")
    report = {"schema_version": 1, "status": "diagnostic_link_prepared",
              "production_admitted": False, "device_commands": 0, "model_requests": 0,
              "native_tool_commands_started": 0, "compilation_performed": False,
              "runtime_validation": "pending", "diagnostic_harness": True,
              "bindings": bindings, "harness_copy_sha256": sha(copied),
              "binary_path": str(binary), "command": command,
              "cwd": str(output), "environment": {"TEMP": str(output), "TMP": str(output)}}
    with (output / "prepared.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("prepared", "build", "library", "compiler", "harness"):
        if name != "harness":
            parser.add_argument("--" + name, required=True)
        parser.add_argument("--expected-" + name + "-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        report = prepare(args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, "diagnostic prepare failed: " + str(error) + "\n")
    print(json.dumps({key: report[key] for key in ("status", "binary_path", "compilation_performed", "device_commands")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
