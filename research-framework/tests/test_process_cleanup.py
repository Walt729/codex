"""Offline process ownership checks; no Codex model or network requests."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("process_cleanup_runner", BASE / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def alive(pid):
    identity = runner.group_identity(pid)
    return bool(identity and identity["state"] not in ("Z", "X"))


class ProcessCleanupTests(unittest.TestCase):
    def leader_and_child(self, exit_leader=False):
        child_code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);time.sleep(20)"
        script = ("import subprocess,sys,time;"
                  f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],"
                  "stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True);"
                  "p.stdout.readline();"
                  "print(p.pid,flush=True);" + ("" if exit_leader else "time.sleep(20)"))
        proc = subprocess.Popen([sys.executable, "-c", script], start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        runner.register_owned_group(proc)
        child = int(proc.stdout.readline())
        self.addCleanup(proc.stdout.close)
        self.addCleanup(lambda: runner.kill_group(proc))
        return proc, child

    def assert_child_stopped(self, child):
        end = time.monotonic() + 1
        while alive(child) and time.monotonic() < end:
            time.sleep(0.01)
        self.assertFalse(alive(child), f"Owned descendant {child} is still running")

    def test_finished_leader_does_not_leave_live_descendants(self):
        proc, child = self.leader_and_child(exit_leader=True)
        until = time.monotonic() + 2
        while not runner.owned_group_finished(proc) and time.monotonic() < until:
            time.sleep(0.01)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(alive(child))
        self.assertTrue(runner.kill_group(proc))
        self.assert_child_stopped(child)

    def test_stop_kills_descendant_after_leader_accepts_term(self):
        proc, child = self.leader_and_child()
        self.assertTrue(runner.kill_group(proc))
        self.assert_child_stopped(child)

    def test_changed_birth_identity_is_not_signalled(self):
        proc, child = self.leader_and_child()
        original = proc._research_group["leader_start"]
        proc._research_group["leader_start"] = "different-birth"
        try:
            self.assertFalse(runner.kill_group(proc))
            self.assertIsNone(proc.poll())
            self.assertTrue(alive(child))
        finally:
            proc._research_group["leader_start"] = original

    def test_unregistered_group_is_not_signalled(self):
        proc, child = self.leader_and_child()
        owned = proc._research_group
        proc._research_group = None
        try:
            self.assertFalse(runner.kill_group(proc))
            self.assertIsNone(proc.poll())
        finally:
            proc._research_group = owned

    def test_unreapable_process_reports_cleanup_unverified(self):
        proc = Mock(pid=12345)
        proc.wait.side_effect = subprocess.TimeoutExpired("owned test process", 1)
        with patch.object(runner, "owned_group_members", return_value=[12345]), \
             patch.object(runner.os, "killpg"):
            self.assertFalse(runner.kill_group(proc))

    def test_successful_fake_cli_cleans_its_background_child(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            task = home / "task"
            fake = home / "fake-codex"
            fake.write_text('''#!/usr/bin/env python3
import json,pathlib,subprocess,sys
sys.stdin.read()
p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(20)"],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
root=pathlib.Path.cwd()
(root/"evidence"/"child.txt").write_text(str(p.pid))
pathlib.Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"continue","summary":"Offline process test only","evidence":["evidence/child.txt"],"next_action":"Stop the fake subprocess","progress":True}))
''')
            fake.chmod(0o755)
            initial = subprocess.run([sys.executable, str(BASE / "runner.py"), "init",
                                      "--task-dir", str(task), "--topic", "Offline cleanup test",
                                      "--total-seconds", "5", "--review-seconds", "3",
                                      "--final-seconds", "1", "--slice-seconds", "1"],
                                     capture_output=True, text=True, timeout=3)
            self.assertEqual(initial.returncode, 0, initial.stderr)
            completed = subprocess.run([sys.executable, str(BASE / "runner.py"), "run", str(task),
                                        "--codex-bin", str(fake), "--max-steps", "1"],
                                       capture_output=True, text=True, timeout=4)
            child_path = task / "evidence" / "child.txt"
            child = int(child_path.read_text()) if child_path.exists() else None
            try:
                self.assertEqual(completed.returncode, 0, completed.stderr)
                state = json.loads(completed.stdout)
                self.assertEqual(state["status"], "paused")
                self.assertEqual(state["attempts"][0]["outcome"], "ok")
                self.assertIsNotNone(child)
                self.assert_child_stopped(child)
            finally:
                if child and alive(child):
                    os.kill(child, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
