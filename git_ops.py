"""Git plumbing used by the Phase B worker (B3+).

Two concerns:

- `is_clean(repo_root)` — pre-flight: does the user have uncommitted
  changes in the target repo? If so the worker refuses the job entirely.
  Spirit of the rule: don't interfere with in-flight work there.
- `branch_and_push(...)` — take a diff produced by `transform.py`, land
  it as a pushed branch named `rae-local-agent/<job-id>` on the target
  repo's origin. Uses `git worktree add` so the user's active checkout
  is never touched. If `test_cmd` is given (B4), runs it in the worktree
  between apply and commit; a non-zero exit raises `TestsFailedError`
  and nothing is pushed.

Why subprocess over a git library: no new dep, straight-line reads, and
the error messages come back as git's own stderr — which is exactly
what we want surfaced in status.txt. If the subprocess churn ever
becomes a problem we can swap to pygit2 behind this module's seam.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

# Baked into every commit the worker makes. Avoids depending on the host's
# `git config user.*` and makes authorship obvious in git log.
COMMIT_AUTHOR_NAME = "rae-local-agent"
COMMIT_AUTHOR_EMAIL = "rae-local-agent@noreply.local"


class GitError(RuntimeError):
    """A git subprocess returned non-zero. The message carries stderr."""


class TestsFailedError(RuntimeError):
    """Test command returned non-zero. `.log` holds the combined output."""

    def __init__(self, log: str):
        super().__init__("test command failed")
        self.log = log


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git, raise GitError on non-zero with stderr as the message."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        step = " ".join(cmd[:2])  # e.g. "git fetch", "git apply"
        raise GitError(f"{step} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def is_clean(repo_root: Path) -> bool:
    """True iff `git status --porcelain` is empty in repo_root."""
    result = _run(["git", "status", "--porcelain"], cwd=repo_root)
    return result.stdout.strip() == ""


def branch_and_push(
    repo_root: Path,
    base_branch: str,
    new_branch: str,
    diff_text: str,
    commit_message: str,
    test_cmd: list[str] | None = None,
) -> None:
    """Land a diff as a pushed branch on origin, isolated via worktree.

    Steps: fetch origin/<base>, create a worktree with a fresh branch
    rooted at origin/<base>, write the diff, `git apply`. If `test_cmd`
    is given, run it in the worktree with stderr merged into stdout —
    a non-zero exit raises `TestsFailedError` with the combined output
    as `.log` and nothing is pushed. On pass, add+commit with a baked-
    in author, push `-u origin <new_branch>`.

    The user's active checkout in `repo_root` is not touched. The
    worktree and the local `<new_branch>` ref are both cleaned up on
    exit regardless of outcome — on success the branch now lives on
    origin, on failure nothing of value is lost.
    """
    _run(["git", "fetch", "origin", base_branch], cwd=repo_root)

    with tempfile.TemporaryDirectory(prefix="rae-wt-") as parent:
        # git worktree add CREATES the target path, so hand it a child of
        # the tempdir that doesn't exist yet.
        wt = Path(parent) / "wt"
        _run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                new_branch,
                str(wt),
                f"origin/{base_branch}",
            ],
            cwd=repo_root,
        )
        try:
            patch = wt / ".rae-local-agent.patch"
            patch.write_text(diff_text, encoding="utf-8")
            _run(["git", "apply", str(patch)], cwd=wt)
            patch.unlink()

            if test_cmd is not None:
                tr = subprocess.run(
                    test_cmd,
                    cwd=wt,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                if tr.returncode != 0:
                    raise TestsFailedError(tr.stdout)

            _run(["git", "add", "-A"], cwd=wt)
            _run(
                [
                    "git",
                    "-c",
                    f"user.name={COMMIT_AUTHOR_NAME}",
                    "-c",
                    f"user.email={COMMIT_AUTHOR_EMAIL}",
                    "commit",
                    "-m",
                    commit_message,
                ],
                cwd=wt,
            )
            _run(["git", "push", "-u", "origin", new_branch], cwd=wt)
        finally:
            _run(
                ["git", "worktree", "remove", "--force", str(wt)],
                cwd=repo_root,
            )
            # Drop the local branch ref: on success it exists on origin,
            # on failure (test gate, push error) it was never pushed.
            # Leaving them would clutter the user's branch listing over
            # time. Silent on error — the branch may not exist if worktree
            # add itself failed.
            subprocess.run(
                ["git", "branch", "-D", new_branch],
                cwd=repo_root,
                capture_output=True,
            )
