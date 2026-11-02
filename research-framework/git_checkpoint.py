"""Bounded, conservative checkpoints for one research task in its selected Git repository."""
import fcntl
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from urllib.parse import urlsplit


DEFAULT_REMOTE = "github.com/Walt729/codex"
MAX_FILE_BYTES = 100 * 1024 * 1024
EXCLUDED_NAMES = {"runner.lock", "stop.request", "git-sync.jsonl", "git-sync.log",
                  "git-sync-state.json"}
SENSITIVE_NAMES = {".env", "auth.json", "credentials", "credentials.json", ".git-credentials",
                   ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "id_dsa"}


class CheckpointError(Exception):
    pass


def remote_identity(value):
    """Return host/owner/repository without printing or returning credential values."""
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "ssh") or parsed.query or parsed.fragment:
            return None
        host, path = parsed.hostname, parsed.path
    else:
        match = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", value)
        if not match:
            return None
        host, path = match.groups()
    parts = path.strip("/").split("/")
    if len(parts) != 2 or not host or any(p in ("", ".", "..") for p in parts):
        return None
    parts[-1] = parts[-1].removesuffix(".git")
    if not parts[-1]:
        return None
    return (host + "/" + "/".join(parts)).casefold()


def _push_target(remote, identity):
    # Keep existing HTTPS authentication. Translate SSH GitHub remotes only for this call,
    # using the platform-supported HTTPS route, without rewriting repository configuration.
    if remote.startswith("ssh://") or re.match(r"[^/]+@[^/:]+:", remote):
        return "https://" + identity + ".git"
    return "origin"


def _excluded(path):
    return ("__pycache__" in path.parts or path.name.endswith((".pyc", ".pyo"))
            or path.name in EXCLUDED_NAMES)


def _sensitive(path):
    return (any(p.casefold() in (".aws", ".ssh") for p in path.parts)
            or path.name.casefold() in SENSITIVE_NAMES
            or path.name.casefold().startswith(".env.")
            or path.suffix.casefold() in (".pem", ".key", ".p12", ".pfx"))


def _walk_error(_error):
    raise CheckpointError("Research artifacts could not all be inspected; synchronization refused.")


class _Git:
    def __init__(self, cwd, deadline):
        self.cwd = cwd
        self.deadline = deadline
        self.env = os.environ.copy()
        self.env["GIT_TERMINAL_PROMPT"] = "0"

    def run(self, *args, data=None, ok=(0,)):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CheckpointError("Git checkpoint exceeded its total time budget.")
        proc = subprocess.Popen(["git", "--literal-pathspecs", "-C", str(self.cwd), *args],
                                stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
                                start_new_session=True)
        try:
            out, err = proc.communicate(input=data, timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise CheckpointError("Git checkpoint exceeded its total time budget.") from None
        if proc.returncode not in ok:
            text = err.decode(errors="replace").casefold()
            if args and args[0] == "push":
                if "non-fast-forward" in text or "fetch first" in text or "rejected" in text:
                    reason = "Git push was rejected; local commits retained. No merge, reset or force was attempted."
                elif any(s in text for s in ("403", "401", "permission", "authentication", "denied")):
                    reason = "Git push access was denied; local commits retained. Existing authentication was preserved."
                else:
                    reason = "Git push failed; local commits retained. Check the repository connection."
            else:
                reason = "Git checkpoint operation failed; no unrelated changes were committed."
            raise CheckpointError(reason)
        return out, proc.returncode


def sync_task(task_root, expected_remote=DEFAULT_REMOTE, timeout=60):
    """Commit this task's artifacts and normally push the current branch; never force/reset.

    Any pre-staged work blocks synchronization unchanged. A failed push retains the local
    commit, and a subsequent call always retries push even if the task files did not change.
    Returned errors deliberately omit raw Git stderr and credential-bearing remote URLs.
    """
    result = {"status": "blocked", "commit": None, "pushed": False, "error": None}
    selected = []
    staged_here = False
    git = None
    lock = None
    try:
        if timeout <= 0:
            raise CheckpointError("Git checkpoint has no remaining time budget.")
        task = Path(task_root).absolute()
        if task.is_symlink():
            raise CheckpointError("Task directories must not be symlinks.")
        task = task.resolve(strict=True)
        if not task.is_dir():
            raise CheckpointError("Task path is not a directory.")
        git = _Git(task, time.monotonic() + timeout)
        raw, _ = git.run("rev-parse", "--show-toplevel")
        root = Path(os.fsdecode(raw.rstrip(b"\n"))).resolve()
        if not task.is_relative_to(root) or task == root:
            raise CheckpointError("Task must be a nonempty relative directory inside its Git checkout.")
        relative = task.relative_to(root)
        if ".git" in relative.parts:
            raise CheckpointError("Task must not be inside Git metadata.")
        git.cwd = root
        raw, _ = git.run("config", "--get", "remote.origin.url")
        remote = os.fsdecode(raw.rstrip(b"\n"))
        identity = remote_identity(remote)
        expected = expected_remote.casefold().removesuffix(".git").rstrip("/")
        if not identity or identity != expected:
            raise CheckpointError("Origin identity does not match the explicitly selected research repository.")
        raw, code = git.run("symbolic-ref", "--quiet", "--short", "HEAD", ok=(0, 1))
        if code:
            raise CheckpointError("Detached HEAD cannot be pushed as an unattended research checkpoint.")
        branch = os.fsdecode(raw.rstrip(b"\n"))
        raw, _ = git.run("rev-parse", "--absolute-git-dir")
        metadata = Path(os.fsdecode(raw.rstrip(b"\n")))
        lock = (metadata / "research-checkpoint.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CheckpointError("Another Git checkpoint is already running.") from None
        raw, _ = git.run("diff", "--cached", "--name-only", "-z")
        if raw:
            raise CheckpointError("Pre-staged changes exist; checkpoint left the index and user work unchanged.")

        candidates = set()
        for folder, dirs, files in os.walk(task, followlinks=False, onerror=_walk_error):
            for name in dirs + files:
                path = Path(folder) / name
                rel = path.relative_to(root)
                if path.is_symlink():
                    raise CheckpointError("Research task contains a symlink; synchronization refused.")
                if name == ".git":
                    raise CheckpointError("Research task contains nested Git metadata; synchronization refused.")
                if _sensitive(rel.relative_to(relative)):
                    raise CheckpointError("Research task contains a credential-sensitive filename; synchronization refused.")
                if not path.is_dir() and not path.is_file():
                    raise CheckpointError("Research task contains an unsupported special file; synchronization refused.")
                if path.is_file() and not _excluded(rel):
                    if path.stat().st_size >= MAX_FILE_BYTES:
                        raise CheckpointError("Research task contains a file of at least 100 MiB; synchronization refused.")
                    candidates.add(str(rel))
        raw, _ = git.run("ls-files", "-z", "--", str(relative))
        for name in raw.split(b"\0"):
            if name:
                rel = Path(os.fsdecode(name))
                if ".git" in rel.parts or _sensitive(rel.relative_to(relative)):
                    raise CheckpointError("Tracked research paths contain Git metadata or sensitive filenames.")
                if not _excluded(rel):
                    candidates.add(str(rel))
        selected = sorted(candidates)
        if selected:
            data = b"\0".join(os.fsencode(p) for p in selected) + b"\0"
            # Exact, literal pathspecs only, including tracked deletions. Git's own ignore
            # rules remain effective; an ignored selected file causes a visible failure.
            staged_here = True
            git.run("add", "--all", "--pathspec-from-file=-", "--pathspec-file-nul", data=data)
            raw, _ = git.run("diff", "--cached", "--name-only", "-z")
            staged = {os.fsdecode(p) for p in raw.split(b"\0") if p}
            if not staged.issubset(set(selected)):
                raise CheckpointError("Unrelated staged changes appeared; checkpoint refused to commit them.")
            if staged:
                git.run("-c", "user.name=Codex", "-c", "user.email=codex@openai.com",
                        "commit", "--only", "-m", "Save research checkpoint",
                        "--pathspec-from-file=-", "--pathspec-file-nul", data=data)
            staged_here = False
        raw, code = git.run("rev-parse", "--verify", "HEAD^{commit}", ok=(0, 128))
        if code:
            raise CheckpointError("Research repository has no commit to push.")
        result["commit"] = raw.decode().strip()
        git.run("push", _push_target(remote, identity), "HEAD:refs/heads/" + branch)
        result.update(status="synced", pushed=True)
    except (CheckpointError, OSError, ValueError) as exc:
        result["error"] = str(exc) if isinstance(exc, CheckpointError) else "Git checkpoint could not safely access its task or repository."
        if staged_here and git and selected:
            # Undo only staging performed by this call; never reset or modify files.
            try:
                data = b"\0".join(os.fsencode(p) for p in selected) + b"\0"
                _, code = git.run("rev-parse", "--verify", "HEAD", ok=(0, 128))
                if code:
                    git.run("rm", "--cached", "--ignore-unmatch", "--pathspec-from-file=-",
                            "--pathspec-file-nul", data=data)
                else:
                    git.run("restore", "--staged", "--pathspec-from-file=-", "--pathspec-file-nul", data=data)
            except (CheckpointError, OSError):
                result["error"] += " Task changes may remain staged; inspect before retrying."
    finally:
        if lock:
            lock.close()
    return result
