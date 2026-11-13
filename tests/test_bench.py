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

    def test_recovered_provider_error_is_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = bench.Path(directory) / "agent.log"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "step_finish", "part": {"reason": "tool-calls"}}),
                        json.dumps(
                            {
                                "type": "error",
                                "error": {
                                    "type": "provider.internal",
                                    "status": 200,
                                    "message": "the GPU reported a fatal error and the engine ended",
                                },
                            }
                        ),
                        json.dumps({"type": "step_start"}),
                        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
                    ]
                )
                + "\n"
            )
            analysis = bench.analyze_provider_errors(path)
            self.assertTrue(analysis["recovered"])
            self.assertFalse(analysis["terminal"])
            self.assertFalse(analysis["retryable"])
            self.assertIsNone(analysis["last_error"])
            self.assertIsNone(bench.parse_provider_error(path))

    def test_unrecovered_transient_error_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = bench.Path(directory) / "agent.log"
            path.write_text(
                json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "type": "provider.internal",
                            "status": 502,
                            "message": "bad gateway",
                        },
                    }
                )
                + "\n"
            )
            analysis = bench.analyze_provider_errors(path)
            self.assertFalse(analysis["recovered"])
            self.assertFalse(analysis["terminal"])
            self.assertTrue(analysis["retryable"])
            self.assertEqual(analysis["last_error"]["status"], 502)

    def test_provider_retry_delay(self) -> None:
        args = argparse.Namespace(provider_retry_delay=30.0)
        agent = {"provider_error": {"retry_after_ms": 90000}}
        self.assertEqual(bench.provider_retry_delay(agent, [{}], args), 90.0)
        self.assertEqual(bench.provider_retry_delay(agent, [{}, {}], args), 180.0)
        self.assertLessEqual(
            bench.provider_retry_delay(agent, [{} for _ in range(10)], args), 900.0
        )
        agent = {"provider_error": {}}
        self.assertEqual(bench.provider_retry_delay(agent, [{}], args), 30.0)

    def test_refresh_provider_status_reclassifies_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = bench.Path(directory)
            (run / "agent-round-1.log").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "error",
                                "error": {
                                    "type": "provider.internal",
                                    "status": 200,
                                    "message": "the GPU reported a fatal error",
                                },
                            }
                        ),
                        json.dumps({"type": "step_start"}),
                        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
                    ]
                )
                + "\n"
            )
            meta = {
                "rounds": [{"round": 1, "provider_error": True}],
                "provider_errors": [],
                "provider_error_count": 1,
                "agent_error": {"type": "provider.internal"},
                "outcome": "provider-error",
                "watchdog": "exit",
                "completion": False,
                "files_changed": 2,
                "agent_ok": False,
            }
            self.assertTrue(bench.refresh_provider_status(meta, run))
            self.assertIsNone(meta["agent_error"])
            self.assertTrue(meta["agent_ok"])
            self.assertEqual(meta["outcome"], "agent-exit-with-changes")
            self.assertFalse(meta["rounds"][0]["provider_error"])
            self.assertEqual(meta["rounds"][0]["provider_recovered"], 1)
            self.assertEqual(meta["provider_recovered"], 1)
            self.assertFalse(bench.refresh_provider_status(meta, run))

    def test_refresh_provider_status_keeps_terminal_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = bench.Path(directory)
            (run / "agent-round-1.log").write_text(
                json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "type": "provider.free-limit",
                            "status": 429,
                            "message": "Free usage limit reached",
                        },
                    }
                )
                + "\n"
            )
            meta = {
                "rounds": [{"round": 1, "provider_error": True}],
                "agent_error": {"type": "provider.free-limit"},
                "outcome": "provider-error",
                "agent_ok": False,
            }
            bench.refresh_provider_status(meta, run)
            self.assertEqual(meta["agent_error"]["type"], "provider.free-limit")
            self.assertFalse(meta["agent_ok"])
            self.assertEqual(meta["outcome"], "provider-error")

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


class MeasurementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = bench.load_manifest()

    def test_combine_repeats_uses_median_speedup(self) -> None:
        first = fake_results(self.manifest, decode_speedup=1.20, encode_speedup=1.00)
        second = fake_results(self.manifest, decode_speedup=1.40, encode_speedup=1.20)
        combined = bench.combine_repeat_results(
            [
                {"result": first, "test_ok": True},
                {"result": second, "test_ok": True},
            ],
            self.manifest,
        )
        self.assertTrue(combined["result_valid"])
        self.assertTrue(combined["test_ok"])
        perf = combined["result"]["perf"]
        self.assertEqual(perf["repeats"], 2)
        for item in perf["decode"]:
            self.assertAlmostEqual(item["speedup"], 1.30)
        for item in perf["encode"]:
            self.assertAlmostEqual(item["speedup"], 1.10)

    def test_combine_repeats_requires_every_gate(self) -> None:
        good = fake_results(self.manifest, 1.2)
        bad = fake_results(
            self.manifest, 1.2, fail_case="p0.decode-lossless:gray-12-512-d5-4x1024-lrcp"
        )
        combined = bench.combine_repeat_results(
            [{"result": good, "test_ok": True}, {"result": bad, "test_ok": True}],
            self.manifest,
        )
        self.assertFalse(combined["result"]["all_gates"])
        self.assertFalse(combined["test_ok"])

    def test_combine_repeats_requires_every_perf_item(self) -> None:
        good = fake_results(self.manifest, 1.2)
        broken = fake_results(self.manifest, 1.2)
        broken["perf"]["decode"][0]["valid"] = False
        combined = bench.combine_repeat_results(
            [{"result": good, "test_ok": True}, {"result": broken, "test_ok": True}],
            self.manifest,
        )
        self.assertFalse(combined["result"]["perf_valid"])
        self.assertFalse(combined["result"]["perf"]["decode_valid"])

    def test_combine_repeats_reports_missing_results(self) -> None:
        combined = bench.combine_repeat_results(
            [{"result": None, "test_ok": False, "error": "boom"}, {"result": None, "test_ok": False}],
            self.manifest,
        )
        self.assertFalse(combined["result_valid"])
        self.assertIn("boom", combined["error"])

    def test_strip_jsonc(self) -> None:
        text = """
        {
          // line comment
          "a": 1, /* block comment */
          "b": [1, 2,],
          "url": "http://example.com//x",
        }
        """
        data = json.loads(bench.strip_jsonc(text))
        self.assertEqual(data["a"], 1)
        self.assertEqual(data["b"], [1, 2])
        self.assertEqual(data["url"], "http://example.com//x")

    def test_prepare_agent_config_disables_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config" / "opencode"
            config.mkdir(parents=True)
            (config / "opencode.jsonc").write_text(
                '{\n  // keep providers\n  "model": "p/m",\n  "tools": {"skill": false}\n}\n'
            )
            run_dir = root / "run"
            staged = bench.prepare_agent_config(config, run_dir)
            self.assertTrue((staged / "opencode.json").exists())
            self.assertFalse((staged / "opencode.jsonc").exists())
            data = json.loads((staged / "opencode.json").read_text())
            self.assertFalse(data["tools"]["question"])
            self.assertFalse(data["tools"]["skill"])
            # the user's config is untouched
            self.assertTrue((config / "opencode.jsonc").exists())
            self.assertIn("// keep providers", (config / "opencode.jsonc").read_text())

    def test_prepare_agent_config_from_explicit_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "custom" / "opencode.json"
            source.parent.mkdir(parents=True)
            source.write_text('{"model": "p/m", "tools": {}}')
            run_dir = root / "run"
            resolved = bench.resolve_config_dir(str(source), run_dir)
            self.assertTrue(resolved.exists())
            staged = bench.prepare_agent_config(resolved, run_dir)
            self.assertTrue(resolved.exists())
            self.assertTrue((staged / "opencode.json").exists())
            data = json.loads((staged / "opencode.json").read_text())
            self.assertFalse(data["tools"]["question"])

    def test_round_clock_and_continue_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            clock = bench.write_round_time(workdir, 2, 600.0, started_at=1_700_000_000.0)
            text = clock.read_text()
            self.assertIn("deadline", text)
            self.assertIn("2023-11-14T", text)
            prompt = bench.write_continue_prompt(workdir, 2, 1, 420.0)
            self.assertTrue(prompt.exists())
            self.assertIn("7 minutes", prompt.read_text())


if __name__ == "__main__":
    unittest.main()
