"""Tests for the B3 git plumbing.

All tests use a scratch repo + a scratch bare "remote" on the local
filesystem so no network is involved. The scratch repo is created with
an initial commit on `main`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import git_ops


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo_pair(tmp_path):
    """A working repo + a bare remote wired as `origin`.

    Yields (repo_root, remote_bare_dir). The repo has one commit on
    `main` adding `hello.py`.
    """
    remote = tmp_path / "remote.git"
    _run(["git", "init", "--bare", "-b", "main", str(remote)], tmp_path)

    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@t"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "hello.py").write_text("def greet(name):\n    return 'hi ' + name\n")
    _run(["git", "add", "hello.py"], repo)
    _run(["git", "commit", "-q", "-m", "init"], repo)
    _run(["git", "remote", "add", "origin", str(remote)], repo)
    _run(["git", "push", "-q", "-u", "origin", "main"], repo)

    return repo, remote


# ---------- is_clean -----------------------------------------------------


def test_is_clean_true_on_fresh_repo(repo_pair):
    repo, _ = repo_pair
    assert git_ops.is_clean(repo) is True


def test_is_clean_false_on_untracked_file(repo_pair):
    repo, _ = repo_pair
    (repo / "scratch.txt").write_text("stray")
    assert git_ops.is_clean(repo) is False


def test_is_clean_false_on_unstaged_modification(repo_pair):
    repo, _ = repo_pair
    (repo / "hello.py").write_text("# touched\n")
    assert git_ops.is_clean(repo) is False


# ---------- branch_and_push happy path ----------------------------------


_HELLO_DIFF = """\
--- a/hello.py
+++ b/hello.py
@@ -1,2 +1,2 @@
-def greet(name):
+def greet(name) -> str:
     return 'hi ' + name
"""


def test_branch_and_push_lands_on_remote(repo_pair):
    repo, remote = repo_pair
    git_ops.branch_and_push(
        repo_root=repo,
        base_branch="main",
        new_branch="rae-local-agent/test-1",
        diff_text=_HELLO_DIFF,
        commit_message="add return type hint to greet",
    )

    # Remote now has the branch.
    result = _run(
        ["git", "--git-dir", str(remote), "branch", "--list", "rae-local-agent/test-1"],
        repo,
    )
    assert "rae-local-agent/test-1" in result.stdout


def test_branch_and_push_commit_has_baked_in_author(repo_pair):
    repo, remote = repo_pair
    git_ops.branch_and_push(
        repo_root=repo,
        base_branch="main",
        new_branch="rae-local-agent/test-2",
        diff_text=_HELLO_DIFF,
        commit_message="add return type hint",
    )
    author = _run(
        [
            "git",
            "--git-dir",
            str(remote),
            "log",
            "-1",
            "--format=%an <%ae>",
            "rae-local-agent/test-2",
        ],
        repo,
    ).stdout.strip()
    assert author == f"{git_ops.COMMIT_AUTHOR_NAME} <{git_ops.COMMIT_AUTHOR_EMAIL}>"


def test_branch_and_push_commit_subject_is_spec(repo_pair):
    repo, remote = repo_pair
    git_ops.branch_and_push(
        repo_root=repo,
        base_branch="main",
        new_branch="rae-local-agent/test-3",
        diff_text=_HELLO_DIFF,
        commit_message="rename greet to say_hello",
    )
    subject = _run(
        [
            "git",
            "--git-dir",
            str(remote),
            "log",
            "-1",
            "--format=%s",
            "rae-local-agent/test-3",
        ],
        repo,
    ).stdout.strip()
    assert subject == "rename greet to say_hello"


def test_branch_and_push_leaves_user_checkout_untouched(repo_pair):
    """Worktree isolation: HEAD stays on main, no files appear in work tree."""
    repo, _ = repo_pair
    before = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
    assert before == "main"

    git_ops.branch_and_push(
        repo_root=repo,
        base_branch="main",
        new_branch="rae-local-agent/test-4",
        diff_text=_HELLO_DIFF,
        commit_message="s",
    )

    after = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
    assert after == "main"
    # The patch file we scribble inside the worktree must not leak.
    assert not (repo / ".rae-local-agent.patch").exists()
    # The local hello.py reflects main, not the branched version.
    assert "-> str" not in (repo / "hello.py").read_text()


# ---------- branch_and_push failure cases -------------------------------


def test_branch_and_push_raises_on_unappliable_diff(repo_pair):
    repo, _ = repo_pair
    bogus = "--- a/does-not-exist.py\n+++ b/does-not-exist.py\n@@ -1 +1 @@\n-x\n+y\n"
    with pytest.raises(git_ops.GitError, match="git apply"):
        git_ops.branch_and_push(
            repo_root=repo,
            base_branch="main",
            new_branch="rae-local-agent/test-5",
            diff_text=bogus,
            commit_message="s",
        )


def test_branch_and_push_raises_on_missing_base_branch(repo_pair):
    repo, _ = repo_pair
    with pytest.raises(git_ops.GitError, match="git fetch"):
        git_ops.branch_and_push(
            repo_root=repo,
            base_branch="nonexistent-branch",
            new_branch="rae-local-agent/test-6",
            diff_text=_HELLO_DIFF,
            commit_message="s",
        )


def test_branch_and_push_cleans_up_local_branch_ref(repo_pair):
    """After a successful push the local <new_branch> ref should be gone."""
    repo, _ = repo_pair
    git_ops.branch_and_push(
        repo_root=repo,
        base_branch="main",
        new_branch="rae-local-agent/test-cleanup",
        diff_text=_HELLO_DIFF,
        commit_message="s",
    )
    result = _run(["git", "branch", "--list", "rae-local-agent/test-cleanup"], repo)
    assert result.stdout.strip() == ""


# ---------- test gate (B4) ----------------------------------------------


def test_branch_and_push_runs_test_cmd_and_pushes_on_zero_exit(repo_pair):
    """A test_cmd that exits 0 lets the push proceed."""
    repo, remote = repo_pair
    git_ops.branch_and_push(
        repo_root=repo,
        base_branch="main",
        new_branch="rae-local-agent/gate-pass",
        diff_text=_HELLO_DIFF,
        commit_message="s",
        test_cmd=["true"],
    )
    result = _run(
        ["git", "--git-dir", str(remote), "branch", "--list", "rae-local-agent/gate-pass"],
        repo,
    )
    assert "rae-local-agent/gate-pass" in result.stdout


def test_branch_and_push_raises_tests_failed_on_nonzero_exit(repo_pair):
    """A test_cmd that exits non-zero → TestsFailedError, no push."""
    repo, remote = repo_pair
    with pytest.raises(git_ops.TestsFailedError):
        git_ops.branch_and_push(
            repo_root=repo,
            base_branch="main",
            new_branch="rae-local-agent/gate-fail",
            diff_text=_HELLO_DIFF,
            commit_message="s",
            test_cmd=["false"],
        )
    # Nothing was pushed.
    result = _run(
        ["git", "--git-dir", str(remote), "branch", "--list", "rae-local-agent/gate-fail"],
        repo,
    )
    assert result.stdout.strip() == ""
    # Local branch ref also cleaned up.
    local = _run(["git", "branch", "--list", "rae-local-agent/gate-fail"], repo)
    assert local.stdout.strip() == ""


def test_tests_failed_error_captures_test_output(repo_pair):
    """TestsFailedError.log holds combined stdout+stderr of the test cmd."""
    repo, _ = repo_pair
    # Script writes to both stdout and stderr; order is preserved because
    # branch_and_push merges stderr into stdout at the pipe level.
    script = (
        "python -c \"import sys; print('OUT_LINE'); "
        "print('ERR_LINE', file=sys.stderr); sys.exit(2)\""
    )
    with pytest.raises(git_ops.TestsFailedError) as excinfo:
        git_ops.branch_and_push(
            repo_root=repo,
            base_branch="main",
            new_branch="rae-local-agent/gate-log",
            diff_text=_HELLO_DIFF,
            commit_message="s",
            test_cmd=["sh", "-c", script],
        )
    assert "OUT_LINE" in excinfo.value.log
    assert "ERR_LINE" in excinfo.value.log


def test_test_cmd_runs_in_worktree_with_applied_diff(repo_pair):
    """The test command sees the applied change, not the pristine checkout."""
    repo, _ = repo_pair
    # Test script asserts the diff landed: hello.py should contain `-> str`.
    with pytest.raises(git_ops.TestsFailedError):
        git_ops.branch_and_push(
            repo_root=repo,
            base_branch="main",
            new_branch="rae-local-agent/gate-cwd",
            diff_text=_HELLO_DIFF,
            commit_message="s",
            # grep -q returns 0 if found, 1 if not; invert with ! for failure
            # when it IS found — this test wants to prove the diff is visible,
            # so we expect the inverted grep to exit non-zero → TestsFailedError.
            test_cmd=["sh", "-c", "! grep -q 'name) -> str' hello.py"],
        )
