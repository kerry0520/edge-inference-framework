"""Offline bundle contract; fixture identity is patched only within unit tests.

These tests execute no ADB/native process and do not represent device evidence.
"""
import contextlib
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_plan_profile as profile
import phone_chat_plan as plan
import phone_chat as router
import phone_chat_session as full
import test_phone_chat_router


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.payload = {"binary": b"unit fixture, never executable", "worker": b"unit source",
                        "helper": b"unit helper"}
        identity = deepcopy(profile.IDENTITY)
        for role, key in (("binary", "binary_sha256"), ("worker", "source_sha256"),
                          ("helper", "helper_sha256")):
            identity[key] = hashlib.sha256(self.payload[role]).hexdigest()
        self.identity = identity
        self.registry = patch.object(profile, "IDENTITY", identity)
        self.registry.start(); self.addCleanup(self.registry.stop)

    def bundle(self, label="bundle"):
        base = self.base / label; base.mkdir()
        for role, value in self.payload.items():
            (base / profile.FILES[role]).write_bytes(value)
        for role, value in (("runtime_build", profile.runtime_document()),
                            ("provenance", profile.provenance_document())):
            (base / profile.FILES[role]).write_text(json.dumps(value), encoding="utf8")
        (base / "profile.json").write_text(json.dumps(profile.profile_document()), encoding="utf8")
        return base / "profile.json"

    def edit(self, path, mutate):
        data = json.loads(path.read_text(encoding="utf8")); mutate(data)
        path.write_text(json.dumps(data), encoding="utf8")

    def no_legacy_read(self, path, *args, **kwargs):
        # Bundle reads remain permitted; historical results and archive reads do not.
        target = Path(path).resolve()
        if (target.is_relative_to(profile.ROOT / "results/android") or
                target.is_relative_to(profile.ROOT / "build") or target.suffix == ".a"):
            raise AssertionError("legacy build dependency: " + str(target))
        return self.real_read(path, *args, **kwargs)

    def test_move_unicode_space_prefix_without_legacy_builder_or_archive(self):
        original = self.bundle("old prefix")
        moved = self.base / "新目录 空格" / "runtime"
        moved.parent.mkdir(); shutil.move(str(original.parent), moved)
        chosen = moved / "profile.json"; adb = self.base / "adb fixture.exe"; adb.write_bytes(b"not executed")
        self.real_read = Path.read_bytes
        real_text = Path.read_text
        def text_guard(path, *a, **kw):
            value = str(path).replace("\\", "/")
            if "/results/android/" in value or value.endswith(".a"):
                raise AssertionError("legacy build read: " + value)
            return real_text(path, *a, **kw)
        with patch.object(plan, "resolve_plan_build", side_effect=AssertionError("legacy resolver")), \
                patch.object(Path, "read_bytes", lambda p,*a,**kw:self.no_legacy_read(p,*a,**kw)), \
                patch.object(Path, "read_text", text_guard), \
                patch.object(subprocess, "run", side_effect=AssertionError("no execution")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("no execution")):
            result = profile.resolve_profile(chosen)
            session = plan.PlanCacheSession("offline", self.base / "sessions", profile_path=chosen, adb_path=adb)
            self.assertEqual(session.binary_path, moved / profile.FILES["binary"])
            self.assertEqual(session.adb.exe, adb.resolve())
            self.assertEqual(session.profile, result)
            self.assertEqual(session.max_context_tokens, 512)
            self.assertFalse(original.exists())

    def test_binary_worker_helper_tamper_even_self_signed_profile_rejected(self):
        for index, (role, key) in enumerate((("binary", "binary_sha256"), ("worker", "source_sha256"),
                                            ("helper", "helper_sha256"))):
            for resign in (False, True):
                with self.subTest(role=role, resign=resign):
                    p = self.bundle(str(index)+str(resign)); target=p.parent/profile.FILES[role]
                    target.write_bytes(b"changed")
                    if resign:
                        self.edit(p, lambda d: d["identity"].update({key: profile.sha(target)}))
                    with self.assertRaises(ValueError): profile.resolve_profile(p)

    def test_metadata_fixed_identity_types_extra_fields_rejected(self):
        edits = [lambda d: d.update(schema_version=True), lambda d: d.update(schema_version=1.0),
                 lambda d: d.update(profile_id="another"), lambda d: d.update(extra="unsigned"),
                 lambda d: d["identity"].update(library_sha256="0"*64),
                 lambda d: d["limits"].update(max_context_tokens=8192)]
        for index, edit in enumerate(edits):
            p = self.bundle("meta"+str(index)); self.edit(p, edit)
            with self.subTest(index=index), self.assertRaises(ValueError): profile.resolve_profile(p)
        for role in ("runtime_build", "provenance"):
            p = self.bundle(role); self.edit(p.parent/profile.FILES[role], lambda d: d.update(schema_version=True))
            with self.subTest(role=role), self.assertRaises(ValueError): profile.resolve_profile(p)

    def test_missing_files_and_traversal_rejected(self):
        for role in profile.FILES:
            p = self.bundle("missing"+role); (p.parent/profile.FILES[role]).rename(p.parent/"missing-backup")
            with self.subTest(role=role), self.assertRaises(ValueError): profile.resolve_profile(p)
        for index, value in enumerate(("../outside", "/absolute", "..\\outside")):
            p = self.bundle("traverse"+str(index)); self.edit(p, lambda d: d["files"].update(binary=value))
            with self.subTest(value=value), self.assertRaises(ValueError): profile.resolve_profile(p)

    def test_external_file_and_profile_symlinks_rejected(self):
        outside = self.base/"external"; outside.write_bytes(self.payload["binary"])
        p = self.bundle("links"); target=p.parent/profile.FILES["binary"]
        target.rename(p.parent/"binary-backup")
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest("host cannot create symlink: " + str(error))
        with self.assertRaises(ValueError): profile.resolve_profile(p)
        link = self.base/"profile-link.json"; link.symlink_to(p)
        with self.assertRaises(ValueError): profile.resolve_profile(link)

    def test_invalid_profile_rejected_before_adb_construction_or_process(self):
        p = self.bundle(); self.edit(p, lambda d: d.update(schema_version=True))
        with patch.object(full, "SessionAdb") as adb, \
                patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as launch:
            with self.assertRaises(ValueError): plan.PlanCacheSession(profile_path=p, adb_path="missing")
            adb.assert_not_called(); run.assert_not_called(); launch.assert_not_called()

    def test_symlink_and_resolved_external_checks_without_host_privilege(self):
        # Separate from the real-link test: exercise both guards on Windows hosts
        # that prohibit creating symlinks. This is mocked path behavior, not an OS test.
        p = self.bundle(); real_link = Path.is_symlink; real_resolve = Path.resolve
        for role in ("profile", "binary"):
            target = p if role == "profile" else p.parent/profile.FILES["binary"]
            def is_link(path):
                return True if path == target else real_link(path)
            with self.subTest(role=role), patch.object(Path,"is_symlink",is_link):
                with self.assertRaises(ValueError): profile.resolve_profile(p)
        outside=self.base/"outside-payload";outside.write_bytes(self.payload["binary"])
        target=p.parent/profile.FILES["binary"]
        def resolved(path,*args,**kwargs):
            return outside if path == target else real_resolve(path,*args,**kwargs)
        with patch.object(Path,"resolve",resolved), self.assertRaises(ValueError):
            profile.resolve_profile(p)

    def test_changed_bundle_is_rejected_again_before_start_connects(self):
        p = self.bundle(); adb = self.base/"adb"; adb.write_bytes(b"fixture")
        with patch.object(full, "SessionAdb") as factory:
            factory.return_value.commands = []
            session = plan.PlanCacheSession(record_parent=self.base/"sessions", profile_path=p, adb_path=adb)
            (p.parent/profile.FILES["helper"]).write_bytes(b"changed after construction")
            with self.assertRaises(ValueError): session.start()
            factory.return_value.select.assert_not_called()

    def test_export_requires_accepted_legacy_build_then_runtime_is_independent(self):
        root = self.base/"source"
        inputs = {"binary":root/"build/plan-cache-chat-20261015/build-v2/phone_chat_plan_worker",
                  "worker":root/"apps/android/phone_chat_plan_worker.cpp",
                  "helper":root/"apps/android/prefill_trunk_reuse.hpp"}
        for role, path in inputs.items():
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(self.payload[role])
        with patch.object(plan,"resolve_plan_build",return_value=dict(self.identity)) as legacy:
            result=profile.export_bundle(self.base/"export",root=root)
            self.assertEqual(legacy.call_count,2)
        with patch.object(plan,"resolve_plan_build",side_effect=AssertionError("runtime must be independent")):
            self.assertEqual(profile.resolve_profile(result["profile_path"]),result)
        with self.assertRaises(FileExistsError): profile.export_bundle(self.base/"export",root=root)

    def test_router_defaults_forwarding_and_illegal_combinations(self):
        helper=test_phone_chat_router.RouterTests(); helper.test_default_stays_full_and_preserves_default_budgets()
        values=helper.invoke(["--stats","--prefill-plan-cache","--plan-profile","有 空格/profile.json",
                              "--adb-path","有 空格/adb.exe"],"phone_chat_plan")[0]
        self.assertEqual(values[-4:],["--plan-profile","有 空格/profile.json","--adb-path","有 空格/adb.exe"])
        self.assertEqual(values[values.index("--max-context-tokens")+1],"512")
        for args in (["--plan-profile","x"],["--adb-path","x"],
                     ["--prefill-plan-cache","--plan-profile","x"],
                     ["--stats","--mode","kv","--prefill-plan-cache","--plan-profile","x"],
                     ["--stats","--prefill-plan-cache","--plan-profile","x","--stats-build-tag","old"],
                     ["--stats","--prefill-plan-cache","--plan-profile","x","--max-context-tokens","513"]):
            with self.subTest(args=args), patch.object(sys,"argv",["chat",*args]), \
                    patch.object(router.importlib,"import_module") as load, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error: router.main()
                self.assertEqual(error.exception.code,2);load.assert_not_called()


if __name__ == "__main__": unittest.main()
