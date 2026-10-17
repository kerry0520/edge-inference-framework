"""Small synthetic build evidence; no compiler, device, or real native tools."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/android"
sys.path.insert(0, str(SCRIPTS))
import phone_source_build_audit as audit


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-audit-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "repo"
        self.output = self.base / "arbitrary-new-build"
        self.records = self.base / "build-records"
        self.source = self.output / "source"
        self.deps = self.output / "deps"
        self.worker = self.output / "worker"
        self.build = self.output / "cmake-build"
        for path in (self.root, self.records, self.source, self.deps / "kleidiai-1.16.0",
                     self.worker, self.build, self.output / "tmp"):
            path.mkdir(parents=True, exist_ok=True)
        for rel in ["scripts/android/build_mnn_source.py", "configs/android/mnn-source-build/lock.json",
                    *audit.recipe.PATCHES, *audit.recipe.CLIENT_SHA]:
            self.write(self.root / rel, (audit.ROOT / rel).read_bytes())
        changed = {}
        for index, rel in enumerate(audit.recipe.SOURCE_SHA):
            path = self.source / rel
            self.write(path, ("changed source " + str(index)).encode())
            changed[rel] = audit.candidate.sha(path)
        self.source_patch = patch.object(audit.recipe, "SOURCE_SHA", changed)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        for rel in audit.recipe.CLIENT_SHA:
            self.write(self.worker / Path(rel).name, (audit.ROOT / rel).read_bytes())
        tools_dir = self.base / "tools"
        ndk = tools_dir / "ndk"
        compiler_bin = ndk / "toolchains/llvm/prebuilt/windows-x86_64/bin"
        self.tools = {}
        for name in sorted(audit.TOOL_KEYS):
            path = (tools_dir / (name + ".exe") if name in ("cmake", "ninja") else
                    ndk / "source.properties" if name == "ndk_properties" else
                    ndk / "build/cmake/android.toolchain.cmake" if name == "ndk_toolchain" else compiler_bin / name)
            self.write(path, ("tool " + name).encode())
            self.tools[name] = {"path": str(path), "sha256": audit.candidate.sha(path)}
        compiler = compiler_bin / "aarch64-linux-android24-clang++.cmd"
        self.write(compiler, b"synthetic compiler is never executed")
        self.readelf = compiler_bin / "llvm-readelf.exe"
        self.write(self.readelf, b"synthetic readelf; mocked subprocess only")
        self.binary = self.worker / "phone_chat_plan_worker"
        head = bytearray(64)
        head[:6], head[18:20] = b"\x7fELF\x02\x01", b"\xb7\x00"
        self.write(self.binary, bytes(head) + b"new output, not a known production hash")
        self.library = self.build / "libMNN.a"
        self.write(self.library, b"!<arch>\nsynthetic archive")
        self.plan = {"schema": 1, "source_commit": audit.recipe.COMMIT,
                     "source_sha256": changed, "input_sha256": {**audit.recipe.PATCHES, **audit.recipe.CLIENT_SHA},
                     "kleidi_sha256": audit.recipe.KLEIDI_SHA, "ndk_revision": audit.recipe.NDK_REVISION,
                     "options": audit.recipe.OPTIONS, "compilation_performed": False, "device_commands": 0,
                     "recipe_sha256": audit.RECIPE_SHA256, "recipe_lock_sha256": audit.LOCK_SHA256,
                     "recipe_lock_path": str(self.root / "configs/android/mnn-source-build/lock.json"),
                     "ndk_properties_sha256": self.tools["ndk_properties"]["sha256"], "tools": self.tools,
                     "compiler": {"path": str(compiler), "sha256": audit.candidate.sha(compiler)},
                     "source_directory": str(self.source), "dependency_directory": str(self.deps),
                     "worker_directory": str(self.worker), "build_directory": str(self.build),
                     "output": str(self.output), "binary_path": str(self.binary), "records": str(self.records),
                     "prepared_source_tree_sha256": "a" * 64, "prepared_dependency_tree_sha256": "b" * 64,
                     "environment": {"TEMP": str(self.output / "tmp"), "TMP": str(self.output / "tmp")},
                     "commands": {"compile": ["unused"] * 6 + ["4"]}}
        self.plan["commands"] = audit.commands(self.plan)
        self.prepared_path = self.records / "prepared.json"
        self.build_path = self.records / "build.json"
        self.cc = []
        self.object_names = []
        files = [self.source / rel for rel in changed]
        files += [self.deps / "kleidiai-1.16.0/kai/actual.c",
                  self.source / "extra/one/shared.cpp", self.source / "extra/two/shared.cpp"]
        for index, path in enumerate(files):
            if not path.exists():
                self.write(path, b"other source")
            rel = "obj" + str(index) + "/" + path.name + ".o"
            self.write(self.build / rel, b"compiled object " + str(index).encode())
            self.object_names.append(rel)
            self.cc.append({"directory": str(self.build), "file": str(path),
                            "command": "clang -DPREFILL_REUSE_DIAGNOSTIC=0 -DMNN_KLEIDIAI_ENABLED=1 -o " + rel})
        self.members = [Path(name).name for name in self.object_names]
        self.built = {"source_commit": audit.recipe.COMMIT, "diagnostic": False, "actual_exit": 0,
                      "binary_path": str(self.binary), "binary_sha256": audit.candidate.sha(self.binary),
                      "library_path": str(self.library), "library_sha256": audit.candidate.sha(self.library),
                      "source_path": audit.candidate.WORKER, "source_sha256": audit.recipe.CLIENT_SHA[audit.candidate.WORKER],
                      "helper_path": audit.candidate.HELPER, "helper_sha256": audit.recipe.CLIENT_SHA[audit.candidate.HELPER],
                      "runtime_validation": "pending", "compilation_performed": True, "device_commands": 0,
                      "prepared_sha256": "", "actual_exits": {key: 0 for key in ("configure", "compile", "link", "archive-members")},
                      "archive_member_count": len(self.members), "compile_commands_path": str(self.build / "compile_commands.json"),
                      "ninja_log_path": str(self.build / ".ninja_log")}
        self.args = argparse.Namespace(prepared=str(self.prepared_path), expected_prepared_sha256="",
                     build=str(self.build_path), expected_build_sha256="", llvm_ar=self.tools["llvm-ar.exe"]["path"],
                     expected_llvm_ar_sha256=self.tools["llvm-ar.exe"]["sha256"], llvm_readelf=str(self.readelf),
                     expected_llvm_readelf_sha256=audit.candidate.sha(self.readelf), candidate=None,
                     expected_candidate_sha256=None)
        self.persist()

    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def json(self, path, value):
        self.write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())

    def persist(self):
        self.json(self.prepared_path, self.plan)
        self.args.expected_prepared_sha256 = audit.candidate.sha(self.prepared_path)
        self.built["prepared_sha256"] = self.args.expected_prepared_sha256
        self.json(self.build_path, self.built)
        self.args.expected_build_sha256 = audit.candidate.sha(self.build_path)
        self.json(self.build / "compile_commands.json", self.cc)
        self.write(self.build / ".ninja_log", ("# ninja log v6\n" + "".join(
            "0\t1\t100\t" + name + "\t123\n" for name in self.object_names) + "2\t3\t101\tlibMNN.a\tabc\n").encode())
        self.write(self.build / "build.ninja", ("build libMNN.a: CXX_STATIC_LIBRARY_LINKER__MNN_Release "
            + " ".join(self.object_names) + " || order_only\n").encode())
        config = {**audit.recipe.OPTIONS, "KLEIDIAI_SRC_DIR": str(self.deps / "kleidiai-1.16.0")}
        self.json(self.records / "configured-options.json", config)
        config.update({"CMAKE_HOME_DIRECTORY": str(self.source), "CMAKE_CACHEFILE_DIR": str(self.build),
                       "CMAKE_GENERATOR": "Ninja", "CMAKE_MAKE_PROGRAM": self.tools["ninja"]["path"],
                       "CMAKE_TOOLCHAIN_FILE": self.tools["ndk_toolchain"]["path"],
                       "CMAKE_CXX_COMPILER_AR": self.tools["llvm-ar.exe"]["path"],
                       "CMAKE_C_COMPILER_AR": self.tools["llvm-ar.exe"]["path"],
                       "CMAKE_CXX_COMPILER_RANLIB": self.tools["llvm-ranlib.exe"]["path"],
                       "CMAKE_C_COMPILER_RANLIB": self.tools["llvm-ranlib.exe"]["path"]})
        self.write(self.build / "CMakeCache.txt", "".join(key + ":STRING=" + value + "\n" for key, value in config.items()).encode())
        for label in ("configure", "compile", "link", "archive-members"):
            stdout = ("\n".join(self.members) + "\n").encode() if label == "archive-members" else b"raw original output\n"
            self.write(self.records / (label + "-stdout.log"), stdout)
            self.write(self.records / (label + "-stderr.log"), b"")
            argv = self.plan["commands"].get(label, [self.tools["llvm-ar.exe"]["path"], "t", str(self.library)])
            self.json(self.records / (label + "-command.json"), {"arguments": argv, "actual_exit": 0,
                      "environment": {} if label == "archive-members" else self.plan["environment"],
                      "cwd": None, "stdout_bytes": len(stdout), "stdout_sha256": hashlib.sha256(stdout).hexdigest()})

    def native(self, argv, **kwargs):
        self.assertEqual(kwargs, {"capture_output": True, "timeout": 60, "shell": False})
        flag = argv[1]
        data = {"t": "\n".join(self.members) + "\n", "-h": "Class: ELF64\nMachine: AArch64\nType: DYN\n",
                "-d": "(FLAGS_1) Flags: PIE\n(NEEDED) Shared library: [libc.so]\n",
                "-l": "Requesting program interpreter: /system/bin/linker64\n"}[flag]
        return subprocess.CompletedProcess(argv, 0, data.encode(), b"")

    def run_audit(self, effect=None):
        out = self.base / ("audit-" + str(len(list(self.base.glob("audit-*")))))
        out.mkdir()
        state = {"native_tool_commands_started": 0}
        with patch.object(audit.subprocess, "run", side_effect=effect or self.native) as run:
            result = audit.audit(self.args, out, state, root=self.root)
        return result, out, run

    def test_new_build_happy_counter_and_readonly_four_tools(self):
        result, output, run = self.run_audit()
        self.assertEqual(result["actual_object_count"], 8)
        self.assertEqual(result["archive_member_count"], 8)
        self.assertEqual(result["actual_kleidi_object_count"], 1)
        self.assertEqual(result["candidate_status"], "candidate_pending")
        self.assertFalse(result["production_admitted"])
        self.assertFalse(result["compilation_performed_by_auditor"])
        self.assertEqual(result["device_commands"], 0)
        self.assertEqual(result["native_tool_commands_started"], 4)
        self.assertEqual([row.args[0][1] for row in run.call_args_list], ["t", "-h", "-d", "-l"])
        derived = json.loads((output / "derived.json").read_text())
        self.assertEqual(derived["archive_member_name_counts"]["shared.cpp.o"], 2)

    def test_external_pins_and_tool_pins_reject_before_native(self):
        def unexpected(*args, **kwargs):
            self.fail("native command started before a pin rejection")
        for key in ("expected_prepared_sha256", "expected_build_sha256", "expected_llvm_ar_sha256", "expected_llvm_readelf_sha256"):
            with self.subTest(key=key):
                original = getattr(self.args, key)
                setattr(self.args, key, "0" * 64)
                with self.assertRaises(ValueError):
                    self.run_audit(unexpected)
                setattr(self.args, key, original)

    def test_receipt_and_raw_nonzero_or_boolean_rejected(self):
        for value in (1, False, True):
            with self.subTest(receipt=value):
                self.built["actual_exits"]["compile"] = value
                self.persist()
                with self.assertRaises(ValueError):
                    self.run_audit()
        self.built["actual_exits"]["compile"] = 0
        self.persist()
        path = self.records / "configure-command.json"
        row = json.loads(path.read_text())
        row["actual_exit"] = 1
        self.json(path, row)
        with self.assertRaisesRegex(ValueError, "configure recorded actual exit"):
            self.run_audit()

    def test_plan_and_raw_together_changed_cannot_authorize_other_link(self):
        self.plan["commands"]["link"][-1] = str(self.base / "other-binary")
        self.persist()
        with self.assertRaisesRegex(ValueError, "commands differ from fixed recipe"):
            self.run_audit()

    def test_missing_object_in_both_log_and_archive_is_rejected_by_target(self):
        target = (self.build / "build.ninja").read_bytes()
        self.object_names.pop()
        self.members.pop()
        self.built["archive_member_count"] -= 1
        self.persist()
        self.write(self.build / "build.ninja", target)
        with self.assertRaisesRegex(ValueError, "complete archive target"):
            self.run_audit()

    def test_duplicate_basename_multiplicity_and_changed_source_presence(self):
        self.members[-1] = "different.cpp.o"
        self.persist()
        with self.assertRaisesRegex(ValueError, "multiplicities"):
            self.run_audit()
        self.members[-1] = "shared.cpp.o"
        self.cc[0]["file"] = self.cc[-1]["file"]
        self.persist()
        with self.assertRaisesRegex(ValueError, "changed source lacks unique"):
            self.run_audit()

    def test_kleidi_define_and_stdout_binding(self):
        self.cc[5]["command"] = self.cc[5]["command"].replace("-DMNN_KLEIDIAI_ENABLED=1", "")
        self.persist()
        with self.assertRaisesRegex(ValueError, "compile define differs"):
            self.run_audit()
        self.cc[5]["command"] += " -DMNN_KLEIDIAI_ENABLED=1"
        self.persist()
        self.write(self.records / "link-stdout.log", b"changed raw output")
        with self.assertRaisesRegex(ValueError, "stdout binding differs"):
            self.run_audit()

    def test_elf_dependency_and_tool_failures_preserve_raw(self):
        for failure in ("dependency", "nonzero", "timeout"):
            with self.subTest(failure=failure):
                def effect(argv, **kwargs):
                    if argv[1] == "-d":
                        if failure == "dependency":
                            return subprocess.CompletedProcess(argv, 0, b"(FLAGS_1) PIE\n(NEEDED) [libevil.so]\n", b"")
                        if failure == "nonzero":
                            return subprocess.CompletedProcess(argv, 3, b"partial", b"inspection failed")
                        raise subprocess.TimeoutExpired(argv, 60, output=b"partial", stderr=b"timeout")
                    return self.native(argv, **kwargs)
                with self.assertRaises(ValueError):
                    self.run_audit(effect)
                out = sorted(self.base.glob("audit-*"))[-1]
                self.assertTrue((out / "elf-dynamic-command.json").is_file())

    def test_mutation_during_readonly_tool_is_rejected(self):
        def effect(argv, **kwargs):
            result = self.native(argv, **kwargs)
            if argv[1] == "-l":
                self.write(self.binary, self.binary.read_bytes() + b"changed")
            return result
        with self.assertRaisesRegex(ValueError, "changed before audit completed"):
            self.run_audit(effect)

    def test_optional_candidate_binding_and_state_unchanged(self):
        folder = self.base / "candidate"
        bound = audit.candidate.prepare(str(self.build_path), self.args.expected_build_sha256, str(folder),
            binary=str(self.binary), library=str(self.library), worker=str(self.worker / "phone_chat_plan_worker.cpp"),
            helper=str(self.worker / "prefill_trunk_reuse.hpp"))
        self.args.candidate = bound["candidate_path"]
        self.args.expected_candidate_sha256 = bound["candidate_sha256"]
        self.run_audit()
        self.assertEqual(audit.candidate.sha(self.args.candidate), bound["candidate_sha256"])
        self.assertFalse(json.loads(Path(self.args.candidate).read_text())["accepted_profile"])
        self.args.expected_candidate_sha256 = "0" * 64
        with self.assertRaises(ValueError):
            self.run_audit()

    def test_ninja_continuation_supported_and_variable_rejected(self):
        path = self.build / "build.ninja"
        original = path.read_text()
        path.write_text(original.replace(" obj1/", " $\n  obj1/"))
        self.run_audit()
        path.write_text(original.replace("obj1/", "$unknown/"))
        with self.assertRaisesRegex(ValueError, "unsupported or ambiguous"):
            self.run_audit()

    def test_retry_frozen_inputs_and_parent_pin(self):
        parent = copy.deepcopy(self.plan)
        parent_path = self.prepared_path
        new_output = self.base / "retry-build"
        new_build = new_output / "cmake-build"
        new_build.mkdir(parents=True)
        (new_output / "tmp").mkdir()
        (new_output / "bin").mkdir()
        retry = copy.deepcopy(parent)
        retry.update({"output": str(new_output), "build_directory": str(new_build),
                      "binary_path": str(new_output / "bin/phone_chat_plan_worker"),
                      "parent_prepared_path": str(parent_path), "parent_prepared_sha256": audit.candidate.sha(parent_path),
                      "environment": {"TEMP": str(new_output / "tmp"), "TMP": str(new_output / "tmp")}})
        retry["commands"] = {key: [retry["binary_path"] if arg == parent["binary_path"] else
                    arg.replace(parent["build_directory"], retry["build_directory"]) for arg in argv]
                    for key, argv in parent["commands"].items()}
        audit.check_plan(retry, audit.Evidence(), root=self.root)
        retry["prepared_source_tree_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "retry changed frozen inputs"):
            audit.check_plan(retry, audit.Evidence(), root=self.root)

    def cli_arguments(self, output):
        values = vars(self.args).copy()
        values["output"] = str(output)
        return [part for key, value in values.items() if value is not None
                for part in ("--" + key.replace("_", "-"), value)]

    def test_report_overlap_rejected_before_creation(self):
        output = self.source / "must-not-be-created"
        with patch.object(audit.subprocess, "run") as run:
            self.assertEqual(audit.main(self.cli_arguments(output)), 1)
            run.assert_not_called()
        self.assertFalse(output.exists())

    def test_missing_real_receipt_classification_is_not_build_failure_claim(self):
        self.args.build = str(self.records / "missing-build.json")
        output = self.base / "missing-receipt-audit"
        with patch.object(audit.subprocess, "run") as run:
            self.assertEqual(audit.main(self.cli_arguments(output)), 1)
            run.assert_not_called()
        report = json.loads((output / "audit-failure.json").read_text())
        self.assertEqual(report["failure_class"], "missing_build_receipt")
        self.assertEqual(report["native_tool_commands_started"], 0)

    def test_cache_duplicate_and_path_identity_rejected(self):
        path = self.build / "CMakeCache.txt"
        original = path.read_text()
        for text in (original + "CMAKE_GENERATOR:STRING=Ninja\n",
                     original.replace("CMAKE_GENERATOR:STRING=Ninja", "CMAKE_GENERATOR:STRING=Other"),
                     original.replace("CMAKE_HOME_DIRECTORY:STRING=" + str(self.source),
                                      "CMAKE_HOME_DIRECTORY:STRING=" + str(self.deps))):
            with self.subTest(cache=text[-60:]):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    self.run_audit()

    def test_precise_macro_values_and_conflicting_redefinition_rejected(self):
        original = self.cc[0]["command"]
        for command in (original.replace("MNN_KLEIDIAI_ENABLED=1", "MNN_KLEIDIAI_ENABLED=10"),
                        original.replace("PREFILL_REUSE_DIAGNOSTIC=0", "PREFILL_REUSE_DIAGNOSTIC=01"),
                        original + " -DPREFILL_REUSE_DIAGNOSTIC=1",
                        original + " -UMNN_KLEIDIAI_ENABLED"):
            with self.subTest(command=command):
                self.cc[0]["command"] = command
                self.persist()
                with self.assertRaisesRegex(ValueError, "compile (define|undefines)"):
                    self.run_audit()

    def test_windows_tokenizer_retains_paths_and_exact_defines(self):
        tokens = audit.windows_tokens('"C:\\Program Files\\NDK\\clang.exe" -I"C:\\a b\\inc" '
                                      '-DPREFILL_REUSE_DIAGNOSTIC=0 -o "obj\\a b.cpp.o"')
        self.assertEqual(tokens, ["C:\\Program Files\\NDK\\clang.exe", "-IC:\\a b\\inc",
                                  "-DPREFILL_REUSE_DIAGNOSTIC=0", "-o", "obj\\a b.cpp.o"])
        audit.exact_define(tokens + ["-D", "PREFILL_REUSE_DIAGNOSTIC=0"], "PREFILL_REUSE_DIAGNOSTIC", "0")


if __name__ == "__main__":
    unittest.main()
