"""Supervisor integration with checkpoint outcomes, without network/model calls."""
import argparse
import importlib.util
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
spec = importlib.util.spec_from_file_location("sync_test_runner", BASE / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class SyncRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "task"
        runner.init_task(argparse.Namespace(
            topic="Verify the original claim", topic_file=None, task_dir=str(self.root),
            total_seconds=10, final_seconds=2, review_seconds=5, slice_seconds=1,
            model=None, sync_git=True))
        self.args = argparse.Namespace(task_dir=str(self.root), action="run",
                                       codex_bin="unused", max_steps=0)

    def fake_invocation(self, root, state, phase, seconds, args, stopped):
        state["counter"] += 1
        (root / "notes" / f"step-{state['counter']}.md").write_text("Verified work")
        return {"outcome": "ok", "completed": True}, {
            "status": "complete", "summary": "Independently checked result",
            "next_action": "", "progress": True, "evidence": []}

    def test_backup_failure_prevents_any_model_call(self):
        with patch("git_checkpoint.sync_task", return_value={
                "pushed": False, "error": "Push rejected", "commit": None}), \
             patch.object(runner, "invoke") as model:
            state = runner.run_task(self.args)
        model.assert_not_called()
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["sync_error"], "Push rejected")
        self.assertTrue((self.root / "checkpoint.md").exists())

    def test_final_report_is_present_before_final_push(self):
        snapshots = []
        def backup(root, **kwargs):
            snapshots.append((runner.read_state(root)["status"], (root / "final.md").exists()))
            return {"pushed": True, "error": None, "commit": "fake"}
        with patch("git_checkpoint.sync_task", side_effect=backup), \
             patch.object(runner, "invoke", side_effect=self.fake_invocation):
            state = runner.run_task(self.args)
        self.assertEqual(state["status"], "finished")
        self.assertEqual(len(snapshots), 3)
        self.assertEqual(snapshots[-1], ("finished", True))

    def test_final_upload_failure_retains_report_but_blocks(self):
        outcomes = [{"pushed": True, "error": None, "commit": "fake"}] * 2 + [
            {"pushed": False, "error": "Network unavailable", "commit": "local-only"}]
        with patch("git_checkpoint.sync_task", side_effect=outcomes), \
             patch.object(runner, "invoke", side_effect=self.fake_invocation):
            state = runner.run_task(self.args)
        self.assertEqual(state["status"], "blocked")
        self.assertTrue((self.root / "final.md").is_file())
        self.assertEqual(state["sync_error"], "Network unavailable")

    def test_expired_budget_does_not_start_a_push(self):
        state = runner.read_state(self.root)
        state.update(started_at=time.time()-20, deadline_at=time.time()-10,
                     next_review_at=time.time()-15, status="stopped")
        runner.save(self.root, state)
        self.args.action = "resume"
        with patch("git_checkpoint.sync_task") as backup, patch.object(runner, "invoke") as model:
            result = runner.run_task(self.args)
        self.assertEqual(result["status"], "expired")
        backup.assert_not_called()
        model.assert_not_called()

    def test_explicit_sync_only_retries_backup_without_model_or_budget_reset(self):
        before = runner.read_state(self.root)
        with patch("git_checkpoint.sync_task", return_value={
                "status": "synced", "pushed": True, "commit": "fake", "error": None}) as backup, \
             patch.object(runner, "invoke") as model, contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(["sync", str(self.root)])
        self.assertEqual(code, 0)
        backup.assert_called_once()
        model.assert_not_called()
        self.assertEqual(before, runner.read_state(self.root))


if __name__ == "__main__":
    unittest.main()
