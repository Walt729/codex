"""Offline integration tests: the fake CLI never invokes a model or a network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "runner.py"
FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, re, sys, time
prompt = sys.stdin.read()
phase = re.search(r"Your role is (\w+)", prompt).group(1)
args = sys.argv
last = pathlib.Path(args[args.index("-o") + 1])
root = pathlib.Path.cwd()
mode = os.environ.get("FAKE_MODE", "normal")
print(json.dumps({"phase": phase}), flush=True)
if mode == "quota":
    print("usage_limit_reached", file=sys.stderr, flush=True)
    time.sleep(30)
if mode == "expired_login":
    print("Your access token could not be refreshed. Please sign in again.", file=sys.stderr, flush=True)
    time.sleep(30)
if mode == "proxy":
    print("ProxyConnect failed: HTTP CONNECT tunnel status 403 Forbidden", file=sys.stderr, flush=True)
    time.sleep(30)
if mode == "hang":
    time.sleep(30)
if mode == "error":
    print("explicit CLI failure", file=sys.stderr)
    sys.exit(1)
time.sleep(float(os.environ.get("FAKE_DELAY", "0.12")))
if mode == "missing_review" and phase == "review":
    sys.exit(0)
if mode == "invalid":
    last.write_text("not JSON")
    sys.exit(0)
evidence = []
if phase == "research" and mode != "stall":
    evidence = ["evidence/" + last.parent.name + ".txt"]
    (root / evidence[0]).write_text("Concrete checked result: " + last.parent.name)
status = "complete" if phase == "final" or mode == "early" else "continue"
if mode == "premature":
    status = "complete" if phase == "research" else "continue"
last.write_text(json.dumps({"status": status, "summary": "Checked evidence for " + phase,
    "evidence": evidence, "next_action": "Check the next concrete question.",
    "progress": mode != "stall"}))
'''


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.task = self.root / "task"
        self.fake = self.root / "fake-codex"
        self.fake.write_text(FAKE)
        self.fake.chmod(0o755)
        self.env = os.environ.copy()
        self.env["FAKE_DELAY"] = "0.02"

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(RUNNER), *args],
                                capture_output=True, text=True, env=self.env, timeout=12)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def init(self, total=5, review=2, final=1, slice=1):
        return self.cli("init", "--task-dir", str(self.task), "--topic", "Prove this test claim.",
                        "--total-seconds", str(total), "--review-seconds", str(review),
                        "--final-seconds", str(final), "--slice-seconds", str(slice),
                        "--model", "same-test-model")

    def run_task(self, action="run", *extra, expected=0):
        return self.cli(action, str(self.task), "--codex-bin", str(self.fake), *extra, expected=expected)

    def wait_active(self):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            state = json.loads((self.task / "state.json").read_text())
            if state.get("active", {}).get("pid") if state.get("active") else False:
                return state
            time.sleep(0.02)
        self.fail("Child never became active")

    def start_hanging(self):
        self.env["FAKE_MODE"] = "hang"
        return subprocess.Popen([sys.executable, str(RUNNER), "run", str(self.task),
                                 "--codex-bin", str(self.fake)], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_init_has_no_model_call_and_rejects_reinitialization(self):
        state = self.init()
        self.assertEqual(state["status"], "pending")
        self.assertIsNone(state["started_at"])
        self.assertEqual(list((self.task / "calls").iterdir()), [])
        self.cli("init", "--task-dir", str(self.task), "--topic", "overwrite", expected=2)
        self.assertEqual((self.task / "original.md").read_text(), "Prove this test claim.")

    def test_periodic_review_final_order_and_same_model(self):
        self.init(total=2.5, review=0.8, final=0.4, slice=0.5)
        self.env["FAKE_DELAY"] = "0.12"
        state = self.run_task()
        phases = [a["phase"] for a in state["attempts"]]
        self.assertEqual(phases[0], "research")
        self.assertIn("review", phases)
        self.assertEqual(phases[-1], "final")
        self.assertEqual(state["status"], "finished")
        self.assertTrue((self.task / "final.md").exists())
        reviews = [a for a in state["attempts"] if a["phase"] == "review" and a["completed"]]
        self.assertGreaterEqual(len(reviews), 1)
        for record in state["attempts"]:
            command = json.loads((self.task / record["directory"] / "command.json").read_text())
            self.assertIn("same-test-model", command)
            if record["phase"] != "research":
                self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertLessEqual(state["attempts"][-1]["ended_at"], state["deadline_at"] + 0.5)

    def test_early_completion_still_gets_independent_final(self):
        self.init()
        self.env["FAKE_MODE"] = "early"
        state = self.run_task()
        self.assertEqual([a["phase"] for a in state["attempts"]], ["research", "final"])
        self.assertEqual(state["status"], "finished")

    def test_rejected_early_completion_resumes_research(self):
        self.init()
        self.env["FAKE_MODE"] = "premature"
        state = self.run_task("run", "--max-steps", "3")
        self.assertEqual([a["phase"] for a in state["attempts"]], ["research", "final", "research"])
        self.assertEqual(state["status"], "paused")
        self.assertTrue(state["last_review"].endswith("-early-final.md"))
        self.assertFalse((self.task / "final.md").exists())
        self.assertTrue((self.task / state["last_review"]).is_file())
        prompt = (self.task / state["attempts"][1]["directory"] / "prompt.txt").read_text()
        self.assertIn("at most 1.0 seconds", prompt)

    def test_pause_and_resume_preserve_deadline(self):
        self.init()
        first = self.run_task("run", "--max-steps", "1")
        second = self.run_task("resume", "--max-steps", "1")
        self.assertEqual(first["deadline_at"], second["deadline_at"])
        self.assertEqual(first["started_at"], second["started_at"])
        self.assertEqual(second["counter"], 2)
        self.assertEqual(second["status"], "paused")

    def test_incomplete_review_is_not_recorded_as_complete(self):
        self.init(total=2.5, review=0.4, final=0.5, slice=0.8)
        self.env["FAKE_MODE"] = "missing_review"
        state = self.run_task(expected=2)
        self.assertEqual(state["status"], "blocked")
        self.assertIsNone(state["last_review"])
        self.assertEqual(list((self.task / "reviews").iterdir()), [])
        self.assertFalse(state["attempts"][-1]["completed"])
        self.assertEqual(state["attempts"][-1]["phase"], "review")

    def test_budget_expired_on_resume_never_calls_model(self):
        state = self.init()
        state.update(started_at=time.time() - 10, deadline_at=time.time() - 5,
                     next_review_at=time.time() - 8, status="stopped")
        (self.task / "state.json").write_text(json.dumps(state))
        final = self.run_task("resume", expected=2)
        self.assertEqual(final["status"], "expired")
        self.assertEqual(final["counter"], 0)
        self.assertFalse((self.task / "final.md").exists())

    def test_stop_terminates_child_and_resume_keeps_budget(self):
        self.init(total=10, review=5, final=1, slice=4)
        proc = self.start_hanging()
        try:
            initial = self.wait_active()
            self.cli("stop", str(self.task))
            out, err = proc.communicate(timeout=3)
            self.assertEqual(proc.returncode, 0, err)
            state = json.loads(out)
            self.assertEqual(state["status"], "stopped")
            self.assertEqual(state["attempts"][-1]["outcome"], "stopped")
            self.env["FAKE_MODE"] = "normal"
            resumed = self.run_task("resume", "--max-steps", "1")
            self.assertEqual(initial["deadline_at"], resumed["deadline_at"])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_duplicate_supervisor_is_rejected(self):
        self.init(total=10, review=5, final=1, slice=4)
        proc = self.start_hanging()
        try:
            self.wait_active()
            rejected = subprocess.run([sys.executable, str(RUNNER), "run", str(self.task)],
                                      capture_output=True, text=True, timeout=2)
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("already has", rejected.stderr)
            self.cli("stop", str(self.task))
            proc.communicate(timeout=3)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_access_failure_detected_while_cli_retries(self):
        self.init()
        self.env["FAKE_MODE"] = "quota"
        before = time.monotonic()
        state = self.run_task(expected=2)
        self.assertLess(time.monotonic() - before, 2)
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["counter"], 1)
        self.assertEqual(state["attempts"][0]["outcome"], "access_denied")

    def test_expired_login_is_detected_without_waiting_for_retries(self):
        self.init()
        self.env["FAKE_MODE"] = "expired_login"
        before = time.monotonic()
        state = self.run_task(expected=2)
        self.assertLess(time.monotonic() - before, 2)
        self.assertEqual(state["counter"], 1)
        self.assertEqual(state["attempts"][0]["outcome"], "access_denied")

    def test_proxy_connect_policy_denial_stops_without_retry(self):
        self.init()
        self.env["FAKE_MODE"] = "proxy"
        before = time.monotonic()
        state = self.run_task(expected=2)
        self.assertLess(time.monotonic() - before, 2)
        self.assertEqual(state["counter"], 1)
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["attempts"][0]["outcome"], "access_denied")

    def test_crashed_supervisor_retires_orphan_before_resume(self):
        self.init(total=10, review=5, final=1, slice=4)
        proc = self.start_hanging()
        try:
            initial = self.wait_active()
            orphan_pid = initial["active"]["pid"]
            proc.kill()
            proc.communicate(timeout=3)
            self.env["FAKE_MODE"] = "normal"
            state = self.run_task("resume", "--max-steps", "1")
            self.assertEqual(state["attempts"][0]["outcome"], "interrupted")
            self.assertEqual(state["deadline_at"], initial["deadline_at"])
            stat = Path(f"/proc/{orphan_pid}/stat")
            self.assertTrue(not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_unsafe_orphan_identity_is_blocked_without_launch(self):
        state = self.init()
        state.update(status="running", started_at=time.time(), deadline_at=time.time() + 5,
                     next_review_at=time.time() + 2,
                     active={"pid": os.getpid(), "process_start": "wrong-identity"})
        (self.task / "state.json").write_text(json.dumps(state))
        blocked = self.run_task("resume", expected=2)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["counter"], 0)
        self.assertIn("orphan cleanup failed", blocked["reason"])

    def test_no_progress_triggers_reviews_and_continues_with_budget(self):
        self.init(total=8, review=5, final=1, slice=1)
        self.env["FAKE_MODE"] = "stall"
        state = self.run_task("run", "--max-steps", "6")
        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["stall_reviews"], 2)
        self.assertEqual(sum(a["phase"] == "review" for a in state["attempts"]), 2)
        self.assertGreater(state["deadline_at"], time.time())

    def test_original_charter_mutation_is_rejected(self):
        self.init()
        (self.task / "original.md").chmod(0o644)
        (self.task / "original.md").write_text("changed")
        rejected = subprocess.run([sys.executable, str(RUNNER), "run", str(self.task)],
                                  capture_output=True, text=True, timeout=2)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("charter changed", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
