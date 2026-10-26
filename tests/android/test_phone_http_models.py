"""Model selection tests use session doubles, not phone inference."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from phone_http_backend import PhoneBackend
from test_phone_http_backend import FakeSession


class ModelBackendTests(unittest.TestCase):
    def test_explicit_model_profile_is_forwarded_once(self):
        session = FakeSession()
        factory = Mock(return_value=session)
        backend = PhoneBackend(model_profile="larger.json", session_factory=factory)
        backend.start()
        self.assertEqual(factory.call_args.kwargs["model_profile"], "larger.json")
        self.assertEqual(backend.info()["model"], "Qwen3-1.7B")
        backend.close()
        session.close.assert_called_once_with()

    def test_model_profile_does_not_replace_default_native_with_overrides(self):
        for override in ({"profile_path": "other.json"}, {"adb_path": "other-adb"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                PhoneBackend(model_profile="larger.json", **override)

    def test_model_selection_uses_only_explicit_model_class(self):
        session = FakeSession()
        with patch("phone_model_session.LargerModelSession", return_value=session) as model, \
                patch("phone_http_backend.PlanCacheSession") as default:
            backend = PhoneBackend(model_profile="larger.json")
            backend.start()
            model.assert_called_once()
            default.assert_not_called()
            backend.close()

    def test_rejected_model_never_falls_back_to_smaller_model(self):
        with patch("phone_model_session.LargerModelSession", side_effect=ValueError("rejected")), \
                patch("phone_http_backend.PlanCacheSession") as default:
            backend = PhoneBackend(model_profile="untrusted.json")
            with self.assertRaises(ValueError):
                backend.start()
            default.assert_not_called()
            self.assertEqual(backend.info()["state"], "closed")


if __name__ == "__main__":
    unittest.main()
