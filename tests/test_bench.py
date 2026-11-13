from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bench  # noqa: E402


def fake_results(
    manifest: dict,
    speedup: float = 1.0,
    *,
    fail_case: str | None = None,
    decode_speedup: float | None = None,
    encode_speedup: float | None = None,
) -> dict:
    families = {
        "p0.decode-lossless": {
            case["id"]: True
            for case in manifest["correctness"] + manifest.get("decode_extra", [])
        },
        "p0.encode-lossless": {case["id"]: True for case in manifest["correctness"]},
        "p0.dicom-201": {"ABDOM_1": True},
        "p0.robustness": {
            name: True
            for name in ("truncated", "garbage", "empty", "bad-magic", "wrong-raw-size")
        },
        "p0.determinism": {"same-input": True},
    }
    if fail_case:
        family, case = fail_case.split(":", 1)
        families[family][case] = False
    perf = {"decode": [], "encode": []}
    for kind in ("decode", "encode"):
        value = speedup
        if kind == "decode" and decode_speedup is not None:
            value = decode_speedup
        if kind == "encode" and encode_speedup is not None:
            value = encode_speedup
        for workload in manifest["perf"][kind]:
            perf[kind].append(
                {
                    "id": workload["id"],
                    "kind": kind,
                    "valid": True,
                    "speedup": value,
                    "seed_mp_s": 100.0,
                    "candidate_mp_s": 100.0 * value,
                    "reference_mp_s": 110.0,
                }
            )
    perf.update(
        {
            "decode_speedup": speedup,
            "encode_speedup": speedup,
            "decode_valid": True,
            "encode_valid": True,
        }
    )
    return {
        "schema": bench.TEST_SCHEMA,
        "suite_version": manifest["suite_version"],
        "correctness": {"families": families},
        "all_gates": not fail_case,
        "perf": perf,
        "perf_valid": True,
        "fixture_seed": 1,
    }


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = bench.load_manifest()
        self.build = {"build_ok": True, "seed_build_ok": True, "typecheck_ok": True, "binary": True}

    def score(
        self,
        speedup: float = 1.0,
        fail_case: str | None = None,
        decode_speedup: float | None = None,
        encode_speedup: float | None = None,
    ) -> dict:
        data = fake_results(
            self.manifest,
            speedup,
            fail_case=fail_case,
            decode_speedup=decode_speedup,
            encode_speedup=encode_speedup,
        )
        return bench.score_run(
            {"result": data},
            self.build,
            manifest=self.manifest,
            result_valid=True,
        )

    def test_noop_scores_zero_and_is_eligible(self) -> None:
        score = self.score(1.0)
        self.assertEqual(score["score"], 0.0)
        self.assertTrue(score["gate_ok"])
        self.assertTrue(score["perf_valid"])
        self.assertTrue(score["eligible"])

    def test_target_scores_one_hundred(self) -> None:
        target_decode = float(self.manifest["targets"]["decode"])
        target_encode = float(self.manifest["targets"]["encode"])
        score = self.score(decode_speedup=target_decode, encode_speedup=target_encode)
        self.assertAlmostEqual(score["score"], 100.0, places=6)
        self.assertTrue(score["eligible"])

    def test_double_target_caps_at_two_hundred(self) -> None:
        target_decode = float(self.manifest["targets"]["decode"])
        target_encode = float(self.manifest["targets"]["encode"])
        score = self.score(
            decode_speedup=target_decode * 2.0,
            encode_speedup=target_encode * 2.0,
        )
        self.assertAlmostEqual(score["score"], 200.0, places=6)

    def test_slower_than_seed_scores_zero(self) -> None:
        score = self.score(0.5)
        self.assertEqual(score["score"], 0.0)
        self.assertTrue(score["eligible"])

    def test_noise_floor_scores_zero(self) -> None:
        score = self.score(1.02)
        self.assertEqual(score["score"], 0.0)
        self.assertTrue(score["eligible"])

    def test_gate_failure_is_ineligible(self) -> None:
        score = self.score(1.25, fail_case="p0.decode-lossless:gray-12-512-d5-4x1024-lrcp")
        self.assertFalse(score["gate_ok"])
        self.assertFalse(score["eligible"])
        self.assertEqual(score["score"], 0.0)

    def test_seed_build_failure_is_ineligible(self) -> None:
        data = fake_results(self.manifest, 1.25)
        score = bench.score_run(
            {"result": data},
            {"build_ok": True, "seed_build_ok": False},
            manifest=self.manifest,
            result_valid=True,
        )
        self.assertFalse(score["eligible"])
        self.assertEqual(score["score"], 0.0)

    def test_missing_perf_item_invalidates(self) -> None:
        data = fake_results(self.manifest, 1.25)
        data["perf"]["decode"].pop()
        score = bench.score_run(
            {"result": data},
            self.build,
            manifest=self.manifest,
            result_valid=True,
        )
        self.assertFalse(score["perf_valid"])
        self.assertEqual(score["score"], 0.0)

    def test_result_validation(self) -> None:
        data = fake_results(self.manifest)
        self.assertIsNone(bench.validate_result(data, self.manifest))
        broken = fake_results(self.manifest)
        del broken["correctness"]["families"]["p0.determinism"]
        self.assertIsNotNone(bench.validate_result(broken, self.manifest))


class SubmissionTests(unittest.TestCase):
    def test_submission_roundtrip(self) -> None:
        meta = {
            "run_id": "r1",
            "model": "provider/model",
            "provider": "provider",
            "suite_version": bench.SUITE_VERSION,
            "score": 42.0,
            "eligible": True,
            "decode_speedup": 1.1,
            "encode_speedup": 1.2,
            "target_decode": 1.25,
            "target_encode": 1.25,
            "gates": [],
        }
        submission = bench.submission_from_score(meta)
        self.assertEqual(bench.validate_submission(submission), [])

    def test_bad_submission(self) -> None:
        problems = bench.validate_submission({"schema": "nope"})
        self.assertTrue(problems)


class HarnessTests(unittest.TestCase):
    def test_cpu_set_flags(self) -> None:
        args = argparse.Namespace(cpu_set="0", no_cpu_set=False)
        self.assertEqual(bench.cpu_set_flags(args), ["--cpuset-cpus", "0", "--cpus", "1"])
        args.no_cpu_set = True
        self.assertEqual(bench.cpu_set_flags(args), [])

    def test_round_plan(self) -> None:
        args = argparse.Namespace(round_plan="75,75,45", no_rounds=False, timeout=14400)
        self.assertEqual(bench.parse_round_plan(args), [75.0, 75.0, 45.0])
        args.no_rounds = True
        self.assertEqual(bench.parse_round_plan(args), [240.0])
        with self.assertRaises(SystemExit):
            bench.parse_round_plan(
                argparse.Namespace(round_plan="0,75", no_rounds=False, timeout=14400)
            )

    def test_round_agent_command(self) -> None:
        args = argparse.Namespace(
            model="provider/model",
            variant=None,
            agent=None,
            pure=False,
            agent_cmd=None,
        )
        command = bench.agent_command(
            args,
            round_number=2,
            prompt_file=bench.Path("ROUND-2.md"),
            session_id="ses_123",
        )
        self.assertIn("--session ses_123", command)
        self.assertIn("/work/ROUND-2.md", command)
        command = bench.agent_command(args, round_number=2, continue_session=True)
        self.assertIn("--continue", command)

    def test_provider_error_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = bench.Path(directory) / "agent.log"
            path.write_text(
                json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "type": "provider.free-limit",
                            "status": 429,
                            "message": "Free usage limit reached",
                        },
                        "retryAfterMs": 23958000,
                    }
                )
                + "\n"
            )
            error = bench.parse_provider_error(path)
            self.assertIsNotNone(error)
            self.assertEqual(error["type"], "provider.free-limit")
            self.assertEqual(error["status"], 429)

    def test_aggregate_skips_agent_error(self) -> None:
        row = {
            "run_id": "bad",
            "model": "m",
            "variant": "default",
            "score": "0",
            "eligible": "false",
            "outcome": "agent-error",
            "agent_error": '{"type":"provider.free-limit"}',
        }
        self.assertEqual(bench.aggregate_entries([row]), [])

    def test_aggregate_single_and_multi_run(self) -> None:
        row = {
            "run_id": "a",
            "model": "m",
            "variant": "default",
            "score": "30",
            "eligible": "true",
            "gate_ok": "true",
            "decode_speedup": "1.2",
            "encode_speedup": "1.1",
            "model_time_s": "10",
            "network_policy": "open",
            "cpu_set": "0",
        }
        one = bench.aggregate_entries([dict(row)])
        self.assertEqual(one[0]["runs"], 1)
        self.assertEqual(one[0]["confidence"], "single-run")
        multi = bench.aggregate_entries([dict(row), {**row, "run_id": "b", "score": "50"}, {**row, "run_id": "c", "score": "40"}])
        self.assertEqual(multi[0]["runs"], 3)
        self.assertEqual(multi[0]["confidence"], "multi-run")
        self.assertEqual(multi[0]["median_score"], 40.0)


class LeaderboardTests(unittest.TestCase):
    def test_order_and_escape(self) -> None:
        rows = [
            {
                "run_id": "a",
                "model": "z/model",
                "score": "10",
                "eligible": "true",
                "decode_speedup": "1.1",
                "encode_speedup": "1.1",
            },
            {
                "run_id": "b",
                "model": "<script>",
                "score": "20",
                "eligible": "false",
                "decode_speedup": "1.2",
                "encode_speedup": "1.2",
            },
        ]
        page = bench.render_leaderboard(rows)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        entries = bench.leaderboard_entries(rows)
        self.assertEqual(entries[0]["run_id"], "b")
        self.assertAlmostEqual(entries[0]["_score"], 15.0)
        self.assertAlmostEqual(entries[1]["_score"], 10.0)


if __name__ == "__main__":
    unittest.main()
