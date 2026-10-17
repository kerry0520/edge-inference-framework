"""Synthetic sampled rows; never compile, connect ADB, or execute a model."""
import array
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import compare_plan_sample_logits as comparison


class SampleComparisonTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sample-comparison-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases = self.root / "cases.json"
        self.write(self.cases, {"schema_version": 1, "cases": [
            {"id": "one", "input_ids": [42], "max_new_tokens": 1}]})
        self.case_pin = comparison.sha(self.cases)
        self.dirs = []
        for mode in ("baseline", "candidate"):
            folder = self.root / mode
            folder.mkdir()
            self.dirs.append(folder)
            scores = array.array("f", [1.0] * comparison.VOCAB)
            scores[42] = 2.0
            (folder / "logits.f32").write_bytes(scores.tobytes())
            step = {"request": 0, "id": "one", "step": 0, "selected_token": 42,
                    "selected_is_stop": False,
                    "count": comparison.VOCAB, "file_float_offset": 0, "offset_bytes": 0,
                    "shape": [1, 1, comparison.VOCAB], "offset": 0, "size": comparison.VOCAB,
                    "slice_applied": False, "source_float_count": comparison.VOCAB,
                    "gen_seq_len": 0, "all_seq_len": 1}
            request = {"request": 0, "id": "one", "ok": True, "mode": mode, "input_ids": [42], "output_ids": [42],
                       "input_tokens": 1, "text": "a", "status": "max_tokens_finished", "status_code": 2, "sample_count": 1,
                       "plan_cache": {"requested": mode == "candidate", "eligible": False,
                                      "prepared": False, "cache_hit": False, "trunk_count": 0,
                                      "full_count": 2, "decode_count": 1}}
            self.write(folder / "steps.jsonl", step)
            self.write(folder / "requests.jsonl", request)
            self.write(folder / "run.json", {"mode": mode, "cases_sha256": self.case_pin,
                       "adb_actual_exit": 0, "native_actual_exit": 0,
                       "binary_sha256": "a" * 64, "library_sha256": "b" * 64,
                       "model_before": {name: "c" * 64 for name in comparison.MODEL_FILES},
                       "model_after": {name: "c" * 64 for name in comparison.MODEL_FILES}})
            self.rebind(folder)

    def write(self, path, obj):
        path.write_bytes((json.dumps(obj) + "\n").encode())

    def rebind(self, folder):
        receipt = comparison.read_json(folder / "run.json")
        receipt["files"] = {name: comparison.sha(folder / name)
                            for name in ("logits.f32", "steps.jsonl", "requests.jsonl")}
        self.write(folder / "run.json", receipt)

    def compare(self):
        return comparison.compare(self.cases, self.case_pin, *self.dirs)

    def test_complete_rows_and_scope(self):
        result = self.compare()
        self.assertEqual(result["compared_rows"], 1)
        self.assertTrue(result["all_bitwise_equal"])
        self.assertFalse(result["production_elf_direct_logits_verified"])
        self.assertFalse(result["performance_claim"])

    def test_different_score_and_nonfinite_rejected(self):
        folder = self.dirs[1]
        for value, pattern in ((1.1, "outside tolerance"), (float("nan"), "nonfinite")):
            row = array.array("f", [1.0] * comparison.VOCAB)
            row[42] = 2.0
            row[-1] = value
            (folder / "logits.f32").write_bytes(row.tobytes())
            self.rebind(folder)
            with self.assertRaisesRegex(ValueError, pattern):
                self.compare()

    def test_hash_and_truncated_rows_rejected(self):
        folder = self.dirs[1]
        (folder / "logits.f32").write_bytes(b"\0" * 4)
        with self.assertRaisesRegex(ValueError, "raw hash"):
            self.compare()
        self.rebind(folder)
        with self.assertRaisesRegex(ValueError, "dump rows"):
            self.compare()

    def test_bool_or_nonzero_exit_rejected(self):
        for value in (True, 1):
            receipt = comparison.read_json(self.dirs[1] / "run.json")
            receipt["native_actual_exit"] = value
            self.write(self.dirs[1] / "run.json", receipt)
            with self.assertRaisesRegex(ValueError, "exit zero"):
                self.compare()

    def test_slice_offsets_and_token_sequence_rejected(self):
        folder = self.dirs[1]
        for key, value, message in (("selected_token", 43, "sequence/token"),
                                    ("offset_bytes", 4, "file offsets"),
                                    ("slice_applied", True, "slice semantics")):
            step = comparison.read_json(self.dirs[0] / "steps.jsonl")
            step[key] = value
            self.write(folder / "steps.jsonl", step)
            self.rebind(folder)
            with self.assertRaisesRegex(ValueError, message):
                self.compare()

    def test_route_and_model_identity_rejected(self):
        folder = self.dirs[1]
        request = comparison.read_json(folder / "requests.jsonl")
        request["plan_cache"]["trunk_count"] = 1
        self.write(folder / "requests.jsonl", request)
        self.rebind(folder)
        with self.assertRaisesRegex(ValueError, "execution route"):
            self.compare()
        request["plan_cache"]["trunk_count"] = 0
        self.write(folder / "requests.jsonl", request)
        self.rebind(folder)
        receipt = comparison.read_json(folder / "run.json")
        receipt["model_after"][comparison.MODEL + "llm.mnn"] = "d" * 64
        self.write(folder / "run.json", receipt)
        with self.assertRaisesRegex(ValueError, "model identities"):
            self.compare()

    def test_greedy_row_and_tolerance_guards(self):
        folder = self.dirs[1]
        scores = array.array("f", [1.0] * comparison.VOCAB)
        (folder / "logits.f32").write_bytes(scores.tobytes())
        self.rebind(folder)
        with self.assertRaisesRegex(ValueError, "greedy selected"):
            self.compare()
        for value in (float("nan"), float("inf"), -1, True):
            with self.assertRaisesRegex(ValueError, "tolerances"):
                comparison.compare(self.cases, self.case_pin, *self.dirs, atol=value)


if __name__ == "__main__":
    unittest.main()
