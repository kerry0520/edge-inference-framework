"""Offline candidate model identity/protocol checks; no ADB/model execution."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_chat_plan as plan
import phone_chat_session as full
import phone_model_session as candidate
from phone_infer import EXPECTED_HARDWARE_SERIAL
from test_phone_chat_plan import output
from test_phone_stream_session import FakeConnection


def identity_output():
    lines = []
    for name, entry in candidate.MODEL_FILES.items():
        lines.extend([entry["sha256"] + "  " + candidate.REMOTE_DIRECTORY + "/" + name,
                      str(entry["size"])])
    return ("\n".join(lines) + "\n").encode()


class ModelSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="phone-model-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.profile = self.directory / "model.json"
        self.profile.write_text(json.dumps(candidate.model_profile_document()), encoding="utf-8")

    def session(self, **kwargs):
        with patch.object(plan, "resolve_plan_build", return_value={"identity": "offline-test"}):
            return candidate.LargerModelSession(self.profile, serial="offline",
                record_parent=self.directory / "sessions", **kwargs)

    def test_exact_profile_is_fixed_and_returns_independent_document(self):
        document = candidate.model_profile_document()
        self.assertEqual(candidate.resolve_model_profile(self.profile), document)
        self.assertEqual(document["remote_directory"], candidate.REMOTE_DIRECTORY)
        self.assertEqual(document["source"], {"repo": "MNN/Qwen3-1.7B-MNN",
            "revision": "cc0cae045f11c525fba613066ce7c54da03ac4dd"})
        document["files"]["llm.mnn"]["size"] = 1
        self.assertEqual(candidate.model_profile_document()["files"]["llm.mnn"]["size"], 461520)

    def test_unknown_profile_fields_and_changed_identity_fail_before_adb(self):
        original = candidate.model_profile_document()
        mutations = []
        for key, value in (("schema_version", True), ("model_id", "qwen3-1.7b-mnn-w4"),
                           ("remote_directory", full.MODEL), ("runtime", {"reuse_kv": True}),
                           ("config", {"backend_type": "gpu"}), ("binary", "/tmp/worker")):
            document = deepcopy(original)
            document[key] = value
            mutations.append(document)
        for key, value in (("repo", "other/model"), ("revision", "master")):
            document = deepcopy(original)
            document["source"][key] = value
            mutations.append(document)
        for key, value in (("size", True), ("sha256", "0" * 64)):
            document = deepcopy(original)
            document["files"]["llm.mnn"][key] = value
            mutations.append(document)
        document = deepcopy(original)
        del document["files"]["tokenizer.txt"]
        mutations.append(document)
        document = deepcopy(original)
        document["files"]["../extra"] = deepcopy(document["files"]["llm.mnn"])
        mutations.append(document)
        with patch.object(full, "SessionAdb") as adb:
            for document in mutations:
                with self.subTest(document=document):
                    self.profile.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        candidate.LargerModelSession(self.profile)
            adb.assert_not_called()

    def test_explicit_native_profile_or_adb_combinations_are_rejected(self):
        for kwargs in ({"profile_path": "profile.json"}, {"adb_path": "adb"},
                       {"profile_path": "profile.json", "adb_path": "adb"}):
            with self.subTest(kwargs=kwargs), patch.object(full, "SessionAdb") as adb:
                with self.assertRaises(ValueError):
                    self.session(**kwargs)
                adb.assert_not_called()

    def test_budget_is_not_expanded_and_default_plan_identity_is_verified(self):
        with patch.object(full, "SessionAdb") as adb:
            with self.assertRaises(ValueError):
                self.session(max_context_tokens=513)
            adb.assert_not_called()
        with patch.object(plan, "resolve_plan_build", side_effect=ValueError("native not admitted")):
            with self.assertRaisesRegex(ValueError, "native not admitted"):
                candidate.LargerModelSession(self.profile)

    def test_candidate_config_is_instance_separate_and_baseline_only(self):
        original = deepcopy(plan.PlanCacheSession.extra_config)
        session = self.session()
        self.assertFalse(session.plan_requested)
        self.assertFalse(session.extra_config["prefill_head_elision"])
        self.assertEqual(session.extra_config["base_dir"], candidate.REMOTE_DIRECTORY + "/")
        self.assertEqual(session.extra_config["tokenizer_file"], "tokenizer.txt")
        self.assertEqual(session.model_name, "Qwen3-1.7B")
        self.assertNotEqual(session.release, plan.PlanCacheSession.release)
        self.assertEqual(plan.PlanCacheSession.extra_config, original)
        self.assertEqual(session.binary_path, plan.PlanCacheSession.binary_path)
        self.assertFalse(session.record["model_candidate"]["admitted_model"])

    def test_candidate_identity_checks_four_remote_sha_and_sizes(self):
        session = self.session()
        adb = Mock()
        adb.shell.return_value = identity_output()
        result = session.model_identity_for_session(adb)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[candidate.REMOTE_DIRECTORY + "/llm.mnn"], candidate.MODEL_FILES["llm.mnn"])
        command = adb.shell.call_args.args[0]
        self.assertNotIn("0.6B", command)
        self.assertNotIn("tokenizer-android-lf", command)
        self.assertEqual(command.count("sha256sum"), 4)
        self.assertEqual(command.count("wc -c"), 4)

    def test_identity_malformed_sha_size_path_and_count_are_rejected(self):
        session = self.session()
        valid = identity_output()
        bad = [valid.replace(b"a6352f", b"000000", 1), valid.replace(b"461520", b"461519", 1),
               valid.replace(b"461520", b"True", 1), valid.replace(b"cc0cae045f11", b"wrong", 1),
               b"\n".join(valid.splitlines()[:-1]) + b"\n", valid + b"extra\n"]
        for content in bad:
            with self.subTest(content=content[:80]), self.assertRaises(RuntimeError):
                session.model_identity_for_session(Mock(shell=Mock(return_value=content)))

    def test_profile_changed_before_start_never_enters_original_start(self):
        session = self.session()
        document = candidate.model_profile_document()
        document["source"]["revision"] = "master"
        self.profile.write_text(json.dumps(document), encoding="utf-8")
        with patch.object(full.Session, "start") as start:
            with self.assertRaises(ValueError):
                session.start()
            start.assert_not_called()

    def test_parent_start_uses_instance_model_hook_before_any_deployment(self):
        # Execute the real original startup until mkdir, where the offline mock
        # deliberately stops. No Popen/socket/native worker is started.
        session = self.session()
        session.adb = Mock()
        # Reuse the existing identity constraint without duplicating its value.
        session.adb.select.return_value = {"hardware_serial": EXPECTED_HARDWARE_SERIAL}
        session.adb.shell.side_effect = [identity_output(), RuntimeError("stop before deployment")]
        binary = self.directory / "worker"
        binary.write_bytes(b"offline binary")
        worker = self.directory / "worker.cpp"
        worker.write_bytes(b"offline source")
        manifest = self.directory / "build.json"
        manifest.write_text(json.dumps({"binary_sha256": full.sha(binary),
                                      "source_sha256": full.sha(worker)}), encoding="utf-8")
        session.binary_path, session.worker_source, session.manifest_path = binary, worker, manifest
        with patch.object(full, "model_identity", side_effect=AssertionError("old model forbidden")), \
                patch.object(full.subprocess, "Popen") as process:
            with self.assertRaisesRegex(RuntimeError, "stop before deployment"):
                full.Session.start(session)
            process.assert_not_called()
        self.assertEqual(len(session.record["model_before"]), 4)
        self.assertTrue(all(candidate.REMOTE_DIRECTORY in name for name in session.record["model_before"]))
        session.adb.push_verified.assert_not_called()

    def test_default_identity_hook_still_uses_original_module_function(self):
        session = full.Session("offline", self.directory / "default")
        adb = Mock()
        with patch.object(full, "model_identity", return_value={"old-model": "unchanged"}) as identity:
            self.assertEqual(session.model_identity_for_session(adb), {"old-model": "unchanged"})
            identity.assert_called_once_with(adb)

    def test_default_and_candidate_close_pass_correct_identity_hook(self):
        session = self.session()
        adb = Mock(shell=Mock(return_value=identity_output()))
        with patch.object(full, "close_session", side_effect=lambda owner, exchange, identity: identity(adb)), \
                patch.object(full, "model_identity", side_effect=AssertionError("old model forbidden")):
            self.assertEqual(len(session.close()), 4)
        default = full.Session("offline", self.directory / "default-close")
        with patch.object(full, "close_session", side_effect=lambda owner, exchange, identity: identity(adb)), \
                patch.object(full, "model_identity", return_value={"old-model": "unchanged"}) as identity:
            self.assertEqual(default.close(), {"old-model": "unchanged"})
            identity.assert_called_once_with(adb)

    def test_full_history_and_baseline_metrics_do_not_claim_plan_reuse(self):
        session = self.session()
        session.record["ready"] = {"pid": 123}
        session.socket = FakeConnection(lambda request: output(request, candidate=False))
        first = session.ask("question")
        second = session.ask("follow up")
        self.assertEqual(len(session.history), 4)
        for result in (first, second):
            self.assertFalse(result["plan_cache"]["requested"])
            self.assertFalse(result["plan_cache"]["prepared"])
            self.assertFalse(result["plan_cache"]["cache_hit"])
            self.assertEqual(result["metrics"]["reused_tokens"], 0)

    def test_optimized_result_is_rejected_before_history_commit(self):
        session = self.session()
        session.record["ready"] = {"pid": 123}
        session.socket = FakeConnection(lambda request: output(request, candidate=True))
        with self.assertRaises(RuntimeError):
            session.ask("question")
        self.assertEqual(session.history, [])
        self.assertTrue(session.transport_uncertain)


if __name__ == "__main__":
    unittest.main()
