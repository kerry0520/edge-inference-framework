"""Offline CLI routing contract; never starts ADB or a model worker."""
import contextlib
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_chat as router


class RouterTests(unittest.TestCase):
    def invoke(self, arguments, module_name, failure=None):
        original = ["phone_chat.py"] + arguments
        seen = []

        def run():
            seen.append(list(sys.argv))
            if failure:
                raise failure
            return 17

        module = SimpleNamespace(__file__="selected_session.py", main=run)
        with patch.object(sys, "argv", original), patch.object(
                router.importlib, "import_module", return_value=module) as load:
            if failure:
                with self.assertRaises(type(failure)):
                    router.main()
            else:
                self.assertEqual(router.main(), 17)
            self.assertIs(sys.argv, original)
            load.assert_called_once_with(module_name)
        return seen

    def test_default_stays_full_and_preserves_default_budgets(self):
        self.assertEqual(self.invoke([], "phone_chat_session"), [[
            "selected_session.py", "--max-new-tokens", "64",
            "--max-context-tokens", "2048"]])

    def test_all_explicit_modes_forward_unicode_prompt_device_and_budgets(self):
        # A prompt remains one argument even with spaces/newlines; ADB address
        # and budgets must reach the selected session without reinterpretation.
        for mode, module in (("full", "phone_chat_session"),
                             ("kv", "phone_chat_kv_session"),
                             ("query", "phone_chat_query_session")):
            with self.subTest(mode=mode):
                seen = self.invoke(["--mode", mode, "--serial", "192.168.5.30:38211",
                                    "--prompt", "你好 空格\n第二行", "--max-new-tokens", "128",
                                    "--max-context-tokens", "4096"], module)
                self.assertEqual(seen, [["selected_session.py", "--max-new-tokens", "128",
                                        "--max-context-tokens", "4096", "--serial",
                                        "192.168.5.30:38211", "--prompt", "你好 空格\n第二行"]])

    def test_session_error_or_system_exit_restores_original_arguments(self):
        for error in (RuntimeError("offline failure"), SystemExit(7)):
            with self.subTest(error=type(error).__name__):
                self.invoke(["--mode", "query"], "phone_chat_query_session", error)

    def test_stats_routes_only_full_and_kv_without_changing_default(self):
        for mode in ("full", "kv"):
            seen=self.invoke(["--mode", mode, "--stats"], "phone_chat_metrics")
            self.assertEqual(seen, [["selected_session.py", "--max-new-tokens", "64",
                                    "--max-context-tokens", "2048", "--mode", mode]])
        with patch.object(sys,"argv",["phone_chat.py","--mode","query","--stats"]), \
                patch.object(router.importlib,"import_module") as load, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error: router.main()
            self.assertEqual(error.exception.code,2)
            load.assert_not_called()

    def test_invalid_selection_and_budgets_never_import_session(self):
        for arguments in (["--mode", "unknown"], ["--max-new-tokens", "0"],
                          ["--max-new-tokens", "513"], ["--max-context-tokens", "0"],
                          ["--max-context-tokens", "8193"]):
            with self.subTest(arguments=arguments), patch.object(sys, "argv", ["phone_chat.py"] + arguments), \
                    patch.object(router.importlib, "import_module") as load, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    router.main()
                self.assertEqual(error.exception.code, 2)
                load.assert_not_called()

    def test_help_explains_opt_in_device_scope_without_importing_session(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["phone_chat.py", "--help"]), \
                patch.object(router.importlib, "import_module") as load, \
                contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as error:
                router.main()
            self.assertEqual(error.exception.code, 0)
            load.assert_not_called()
        help_text = "".join(output.getvalue().split())
        for phrase in ("query", "本机候选实验入口", "Mi11/Qwen3-0.6B", "full为默认", "待核对原版输出差异"):
            self.assertIn(phrase, help_text)

    def test_build_tag_is_forwarded_only_with_stats_and_legal_mode(self):
        for mode in ("full", "kv"):
            seen = self.invoke(["--mode", mode, "--stats", "--stats-build-tag", "fresh_1"], "phone_chat_metrics")
            self.assertEqual(seen[0][-4:], ["--mode", mode, "--stats-build-tag", "fresh_1"])
        for arguments in (["--stats-build-tag", "fresh_1"],
                          ["--stats", "--mode", "query", "--stats-build-tag", "fresh_1"],
                          ["--stats", "--stats-build-tag", "../bad"],
                          ["--stats", "--stats-build-tag", "_--"]):
            with self.subTest(arguments=arguments), patch.object(sys, "argv", ["phone_chat.py"] + arguments), \
                    patch.object(router.importlib, "import_module") as load, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error: router.main()
                self.assertEqual(error.exception.code, 2)
                load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
