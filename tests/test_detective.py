import json
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import detective  # noqa: E402


SAMPLE_FAIL_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2" failures="1" errors="0" skipped="0">
  <testcase classname="tests.test_checkout" name="test_cart_lock" time="0.14">
    <failure message="TimeoutError: lock held by worker">Traceback...</failure>
  </testcase>
  <testcase classname="tests.test_pricing" name="test_discount" time="0.01" />
</testsuite>
"""

SAMPLE_PASS_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2" failures="0" errors="0" skipped="0">
  <testcase classname="tests.test_checkout" name="test_cart_lock" time="0.09" />
  <testcase classname="tests.test_pricing" name="test_discount" time="0.01" />
</testsuite>
"""


class TestFlakyDetective(unittest.TestCase):
    def test_parse_junit_with_sidecar_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml_path = Path(tmp) / "junit-shard-1.xml"
            meta_path = Path(tmp) / "junit-shard-1.meta.json"
            xml_path.write_text(SAMPLE_FAIL_XML, encoding="utf-8")
            meta_path.write_text(
                json.dumps(
                    {
                        "commit_sha": "abc1234",
                        "shard": "shard-1",
                        "runner_id": "runner-a",
                        "seed": "42",
                        "run_attempt": 1,
                    }
                ),
                encoding="utf-8",
            )

            obs = detective.parse_junit_file(xml_path)
            self.assertEqual(len(obs), 2)
            self.assertEqual(obs[0].test_id, "tests.test_checkout::test_cart_lock")
            self.assertEqual(obs[0].outcome, "failed")
            self.assertEqual(obs[0].shard, "shard-1")
            self.assertEqual(obs[0].seed, "42")
            self.assertEqual(obs[0].test_order, 1)
            self.assertEqual(obs[1].outcome, "passed")
            self.assertEqual(obs[1].test_order, 2)

    def test_same_sha_rerun_detects_timing_flake(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "attempt1.xml"
            p2 = Path(tmp) / "attempt2.xml"
            p1.write_text(SAMPLE_FAIL_XML, encoding="utf-8")
            p2.write_text(SAMPLE_PASS_XML, encoding="utf-8")

            obs1 = detective.parse_junit_file(
                p1, commit_sha="deadbeef", default_shard="shard-1", default_seed="7"
            )
            obs2 = detective.parse_junit_file(
                p2, commit_sha="deadbeef", run_attempt=2, default_shard="shard-1", default_seed="7"
            )

            state = detective.load_state(Path(tmp) / "state.json")
            detective.update_state(state, obs1)
            detective.update_state(state, obs2)

            cart = state["tests"]["tests.test_checkout::test_cart_lock"]
            discount = state["tests"]["tests.test_pricing::test_discount"]

            self.assertTrue(cart["is_flaky"])
            self.assertEqual(cart["flake_events"], 1)
            self.assertEqual(cart["flake_rate"], 0.5)
            self.assertEqual(cart["classification"], "timing/race")
            self.assertEqual(cart["consecutive_clean_runs"], 1)

            self.assertFalse(discount["is_flaky"])
            self.assertEqual(discount["flake_rate"], 0.0)

    def test_shard_specific_failure_classified_as_isolation(self):
        state = {"version": 1, "tests": {}, "job_flakes": []}
        obs = [
            detective.TestObservation(
                test_id="tests.test_session::test_temp_port",
                outcome="failed",
                commit_sha="sha-1",
                shard="shard-1",
                seed="101",
            ),
            detective.TestObservation(
                test_id="tests.test_session::test_temp_port",
                outcome="passed",
                commit_sha="sha-1",
                shard="shard-2",
                seed="202",
            ),
        ]
        detective.update_state(state, obs)
        rec = state["tests"]["tests.test_session::test_temp_port"]
        self.assertTrue(rec["is_flaky"])
        self.assertEqual(
            rec["classification"], "isolation/shared-state (shard-specific)"
        )

    def test_fifty_clean_runs_clears_flaky_flag(self):
        state = {"version": 1, "tests": {}, "job_flakes": []}
        obs = [
            detective.TestObservation(
                test_id="tests.test_net::test_retry",
                outcome="failed",
                commit_sha="sha-1",
            ),
            detective.TestObservation(
                test_id="tests.test_net::test_retry",
                outcome="passed",
                commit_sha="sha-1",
                run_attempt=2,
            ),
        ]
        for i in range(49):
            obs.append(
                detective.TestObservation(
                    test_id="tests.test_net::test_retry",
                    outcome="passed",
                    commit_sha=f"clean-{i}",
                )
            )
        detective.update_state(state, obs)
        rec = state["tests"]["tests.test_net::test_retry"]
        self.assertEqual(rec["consecutive_clean_runs"], 50)
        self.assertFalse(rec["is_flaky"])

    def test_markdown_report_contains_sticky_marker_and_leaderboard(self):
        state = {"version": 1, "tests": {}, "job_flakes": []}
        fail_obs = detective.TestObservation(
            test_id="tests.test_checkout::test_cart_lock",
            outcome="failed",
            commit_sha="abc1234",
            shard="shard-1",
        )
        pass_obs = detective.TestObservation(
            test_id="tests.test_checkout::test_cart_lock",
            outcome="passed",
            commit_sha="abc1234",
            run_attempt=2,
            shard="shard-1",
        )
        detective.update_state(state, [fail_obs, pass_obs])
        report = detective.render_markdown_report(state, [fail_obs], [])

        self.assertIn(detective.STICKY_MARKER, report)
        self.assertIn("Known flake detected", report)
        self.assertIn("Flake Leaderboard", report)
        self.assertIn("tests.test_checkout::test_cart_lock", report)


class TestHardening(unittest.TestCase):
    def test_idempotent_identity(self):
        state = {"tests": {}}
        first = detective.TestObservation("test", "failed", commit_sha="sha", run_id="1")
        detective.update_state(state, [first, first])
        detective.update_state(state, [first])
        self.assertEqual(len(state["tests"]["test"]["history"]), 1)
        for changes in ({"run_attempt": 2}, {"shard": "other"}, {"run_id": "2"}, {"commit_sha": "other"}):
            detective.update_state(state, [detective.TestObservation(
                "test", "passed", **({"commit_sha": "sha", "run_id": "1"} | changes)
            )])
        self.assertEqual(len(state["tests"]["test"]["history"]), 5)
        detective.update_state(state, [])
        self.assertEqual(len(state["tests"]["test"]["history"]), 5)

    def test_malformed_and_empty_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            for content in ("", "<testsuite>"):
                path = Path(tmp) / "broken.xml"
                path.write_text(content)
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(detective.parse_junit_file(path), [])
                self.assertIn(f"::warning file={path}::", output.getvalue())

    def test_unexplained_gating_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            root = Path(tmp)
            xml = root / "report.xml"
            xml.write_text(SAMPLE_FAIL_XML)
            output = root / "outputs"
            args = ["--junit-glob", str(xml), "--state-file", str(root / "state.json")]
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(detective.main(args), 0)
                self.assertEqual(detective.main(args + ["--fail-on-unexplained"]), 1)
                self.assertIn("unexplained-failures=1", output.read_text())
                xml.write_text(SAMPLE_PASS_XML)
                self.assertEqual(detective.main(args + ["--run-attempt", "2", "--fail-on-unexplained"]), 0)
                xml.write_text(SAMPLE_FAIL_XML)
                self.assertEqual(detective.main(args + ["--run-attempt", "3", "--fail-on-unexplained"]), 0)
                self.assertIn("unexplained-failures=0", output.read_text())
                self.assertEqual(detective.main(["--junit-glob", str(root / "missing*"), "--state-file", str(root / "empty.json"), "--fail-on-unexplained"]), 0)

    def test_comment_pagination(self):
        def response(payload):
            result = MagicMock()
            result.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            return result
        for found in (True, False):
            pages = [response([{"body": "other"}] * 100), response(
                [{"id": 123, "body": detective.STICKY_MARKER}] if found else []
            ), response({})]
            with patch.object(detective.urllib.request, "urlopen", side_effect=pages) as urlopen:
                self.assertTrue(detective.upsert_pr_comment("owner/repo", 1, "body", "token"))
                self.assertIn("page=2", urlopen.call_args_list[1].args[0].full_url)
                self.assertEqual(urlopen.call_args_list[2].args[0].method, "PATCH" if found else "POST")
        with patch.object(detective.urllib.request, "urlopen", side_effect=detective.urllib.error.URLError("offline")):
            self.assertFalse(detective.upsert_pr_comment("owner/repo", 1, "body", "token"))


if __name__ == "__main__":
    unittest.main()
