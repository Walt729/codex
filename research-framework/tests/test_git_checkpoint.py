"""Git integration tests use a local bare repository, never GitHub or a model."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "git_checkpoint.py"
spec = importlib.util.spec_from_file_location("research_git_checkpoint", MODULE)
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)


class GitCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "repo"
        self.remote = self.home / "remote.git"
        self.repo.mkdir()
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("Framework\n")
        self.git("add", "README.md")
        self.git("commit", "-m", "Initial framework")
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.git("remote", "add", "origin", str(self.remote))
        self.task = self.repo / "research-runs" / "first"
        self.task.mkdir(parents=True)
        (self.task / "original.md").write_text("Prove a claim.\n")
        (self.task / "notes").mkdir()
        (self.task / "notes" / "one.md").write_text("Actual evidence.\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def sync(self, **kwargs):
        # Only identity validation is mocked. add/commit/push and remote verification are real.
        with patch.object(checkpoint, "remote_identity", return_value=checkpoint.DEFAULT_REMOTE.casefold()):
            return checkpoint.sync_task(self.task, **kwargs)

    def remote_head(self):
        return subprocess.check_output(["git", "--git-dir", str(self.remote),
                                        "rev-parse", "refs/heads/main"]).decode().strip()

    def test_real_local_commit_and_push_only_task_files(self):
        (self.repo / "unrelated.txt").write_text("Do not commit this.\n")
        (self.task / "runner.lock").write_text("transient")
        (self.task / "git-sync.log").write_text("self-reference")
        cache = self.task / "__pycache__"
        cache.mkdir()
        (cache / "x.pyc").write_bytes(b"cache")
        before = self.git("rev-parse", "HEAD").decode().strip()
        result = self.sync()
        self.assertEqual(result["status"], "synced", result)
        self.assertTrue(result["pushed"])
        self.assertNotEqual(result["commit"], before)
        self.assertEqual(self.remote_head(), result["commit"])
        files = self.git("ls-tree", "-r", "--name-only", "HEAD").decode().splitlines()
        self.assertEqual(files, ["README.md", "research-runs/first/notes/one.md",
                                 "research-runs/first/original.md"])
        self.assertEqual((self.repo / "unrelated.txt").read_text(), "Do not commit this.\n")

    def test_pre_staged_other_file_is_unchanged_and_not_committed(self):
        unrelated = self.repo / "user-work.txt"
        unrelated.write_text("user staged content")
        self.git("add", "user-work.txt")
        before_head = self.git("rev-parse", "HEAD")
        before_index = self.git("ls-files", "--stage", "-z")
        result = self.sync()
        self.assertFalse(result["pushed"])
        self.assertIn("Pre-staged", result["error"])
        self.assertEqual(self.git("rev-parse", "HEAD"), before_head)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), before_index)
        self.assertEqual(unrelated.read_text(), "user staged content")

    def test_failed_push_retains_commit_and_unchanged_retry_still_pushes(self):
        self.git("remote", "set-url", "origin", str(self.home / "missing.git"))
        before = self.git("rev-parse", "HEAD").decode().strip()
        first = self.sync()
        self.assertFalse(first["pushed"])
        self.assertNotEqual(first["commit"], before)
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), first["commit"])
        self.assertEqual(self.git("diff", "--cached", "--name-only"), b"")
        self.git("remote", "set-url", "origin", str(self.remote))
        second = self.sync()
        self.assertTrue(second["pushed"], second)
        self.assertEqual(second["commit"], first["commit"])
        self.assertEqual(self.remote_head(), first["commit"])

    def test_mismatched_origin_blocks_without_staging(self):
        result = checkpoint.sync_task(self.task)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Origin identity", result["error"])
        self.assertEqual(self.git("diff", "--cached", "--name-only"), b"")

    def test_sensitive_file_is_rejected_without_reading_its_contents(self):
        (self.task / ".env").write_text("do-not-print-secret")
        result = self.sync()
        self.assertFalse(result["pushed"])
        self.assertIn("sensitive filename", result["error"])
        self.assertNotIn("do-not-print-secret", json.dumps(result))
        self.assertEqual(self.git("diff", "--cached", "--name-only"), b"")

    def test_symlink_escape_is_rejected(self):
        (self.task / "outside").symlink_to(self.home)
        result = self.sync()
        self.assertFalse(result["pushed"])
        self.assertIn("symlink", result["error"])

    def test_nested_git_metadata_is_rejected(self):
        (self.task / ".git").mkdir()
        result = self.sync()
        self.assertFalse(result["pushed"])
        self.assertIn("nested Git", result["error"])

    def test_oversized_file_is_rejected_without_loading_it(self):
        with (self.task / "large.bin").open("wb") as stream:
            stream.truncate(checkpoint.MAX_FILE_BYTES)
        result = self.sync()
        self.assertFalse(result["pushed"])
        self.assertIn("100 MiB", result["error"])

    def test_detached_head_is_rejected(self):
        self.git("checkout", "--detach")
        result = self.sync()
        self.assertFalse(result["pushed"])
        self.assertIn("Detached HEAD", result["error"])

    def test_total_timeout_prevents_any_work(self):
        before = self.git("rev-parse", "HEAD")
        result = self.sync(timeout=0)
        self.assertFalse(result["pushed"])
        self.assertIn("time budget", result["error"])
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_deleted_task_file_is_committed_and_pushed(self):
        self.assertTrue(self.sync()["pushed"])
        (self.task / "notes" / "one.md").unlink()
        result = self.sync()
        self.assertTrue(result["pushed"], result)
        files = self.git("ls-tree", "-r", "--name-only", "HEAD").decode()
        self.assertNotIn("notes/one.md", files)

    def test_non_fast_forward_never_resets_local_or_remote_work(self):
        self.assertTrue(self.sync()["pushed"])
        peer = self.home / "peer"
        subprocess.run(["git", "clone", "--branch", "main", str(self.remote), str(peer)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for args in (("config", "user.name", "Peer"),
                     ("config", "user.email", "peer@example.invalid")):
            subprocess.run(["git", "-C", str(peer), *args], check=True)
        (peer / "peer-work.txt").write_text("Keep this remote commit.\n")
        for args in (("add", "peer-work.txt"), ("commit", "-m", "Peer work"),
                     ("push", "origin", "main")):
            subprocess.run(["git", "-C", str(peer), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        remote_before = self.remote_head()
        (self.task / "notes" / "two.md").write_text("Keep this local checkpoint.\n")
        result = self.sync()
        self.assertFalse(result["pushed"])
        self.assertIn("rejected", result["error"])
        self.assertEqual(self.remote_head(), remote_before)
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), result["commit"])
        self.assertEqual((self.task / "notes" / "two.md").read_text(), "Keep this local checkpoint.\n")

    def test_hanging_push_is_bounded_by_total_sync_budget(self):
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\nsleep 5\n")
        hook.chmod(0o755)
        before = time.monotonic()
        result = self.sync(timeout=0.7)
        self.assertFalse(result["pushed"])
        self.assertIn("time budget", result["error"])
        self.assertLess(time.monotonic() - before, 1.8)
        self.assertIsNotNone(result["commit"])
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), result["commit"])

    def test_identity_parser_sanitizes_https_ssh_and_scp(self):
        expected = "github.com/walt729/codex"
        for remote in ("https://username:secret@github.com/Walt729/codex.git",
                       "ssh://git@github.com/Walt729/codex.git",
                       "git@github.com:Walt729/codex.git"):
            self.assertEqual(checkpoint.remote_identity(remote), expected)
        self.assertIsNone(checkpoint.remote_identity("/tmp/local.git"))
        self.assertIsNone(checkpoint.remote_identity("https://github.com/owner/repo/extra"))


if __name__ == "__main__":
    unittest.main()
