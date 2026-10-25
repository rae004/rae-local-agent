"""Tests for the B3 git plumbing.

All tests use a scratch repo + a scratch bare "remote" on the local
filesystem so no network is involved. The scratch repo is created with
an initial commit on `main`.
"""

from __future__ import annotations

import os
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
        gate_cmds=[["true"]],
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
            gate_cmds=[["false"]],
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
    import sys as _sys

    repo, _ = repo_pair
    # Script writes to both stdout and stderr; order is preserved because
    # branch_and_push merges stderr into stdout at the pipe level. Uses
    # `sys.executable` so the subprocess doesn't rely on `python` being
    # on PATH — the test gate scrubs the worker's venv from PATH per #22.
    script = (
        f"{_sys.executable} -c \"import sys; print('OUT_LINE'); "
        "print('ERR_LINE', file=sys.stderr); sys.exit(2)\""
    )
    with pytest.raises(git_ops.TestsFailedError) as excinfo:
        git_ops.branch_and_push(
            repo_root=repo,
            base_branch="main",
            new_branch="rae-local-agent/gate-log",
            diff_text=_HELLO_DIFF,
            commit_message="s",
            gate_cmds=[["sh", "-c", script]],
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
            gate_cmds=[["sh", "-c", "! grep -q 'name) -> str' hello.py"]],
        )


# ---------- venv isolation (#22) ----------------------------------------


def test_scrubbed_test_env_removes_uv_vars(monkeypatch):
    """VIRTUAL_ENV / UV_PROJECT_ENVIRONMENT / UV_CACHE_DIR are dropped."""
    monkeypatch.setenv("VIRTUAL_ENV", "/home/x/projects/rae-local-agent/.venv")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/home/x/projects/rae-local-agent/.venv")
    monkeypatch.setenv("UV_CACHE_DIR", "/home/x/.cache/uv")
    monkeypatch.setenv("UNRELATED_VAR", "keep-me")

    env = git_ops._scrubbed_test_env()

    assert "VIRTUAL_ENV" not in env
    assert "UV_PROJECT_ENVIRONMENT" not in env
    assert "UV_CACHE_DIR" not in env
    assert env.get("UNRELATED_VAR") == "keep-me"


def test_scrubbed_test_env_filters_worker_venv_from_path(monkeypatch):
    """rae-local-agent's `.venv/bin` is stripped from PATH; others survive."""
    monkeypatch.setenv(
        "PATH",
        ":".join(
            [
                "/home/x/projects/rae-local-agent/.venv/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            ]
        ),
    )

    env = git_ops._scrubbed_test_env()
    parts = env["PATH"].split(":")

    assert "/home/x/projects/rae-local-agent/.venv/bin" not in parts
    assert "/usr/local/bin" in parts
    assert "/usr/bin" in parts
    assert "/bin" in parts


def test_link_target_venv_symlinks_when_src_exists(tmp_path):
    """A real `.venv` in repo_root → symlink in the worktree pointing at it."""
    repo = tmp_path / "repo"
    (repo / ".venv").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()

    git_ops._link_target_venv(repo, wt)

    link = wt / ".venv"
    assert link.is_symlink()
    assert link.resolve() == (repo / ".venv").resolve()


def test_link_target_venv_noop_when_src_missing(tmp_path):
    """No `.venv` in repo_root → no symlink attempted, no error."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()

    git_ops._link_target_venv(repo, wt)

    assert not (wt / ".venv").exists()


def test_link_target_venv_idempotent(tmp_path):
    """A pre-existing `.venv` entry in the worktree is left alone."""
    repo = tmp_path / "repo"
    (repo / ".venv").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    # Simulate a previous link (or an unrelated .venv dropped by uv).
    pre_existing = wt / ".venv"
    pre_existing.mkdir()

    git_ops._link_target_venv(repo, wt)

    # Still a directory, not replaced with a symlink.
    assert pre_existing.is_dir()
    assert not pre_existing.is_symlink()


def test_test_cmd_runs_with_scrubbed_env(repo_pair, monkeypatch):
    """The test subprocess does NOT see the worker's VIRTUAL_ENV."""
    repo, _ = repo_pair
    monkeypatch.setenv("VIRTUAL_ENV", "/home/x/projects/rae-local-agent/.venv")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/home/x/projects/rae-local-agent/.venv")

    # Script fails iff VIRTUAL_ENV or UV_PROJECT_ENVIRONMENT is set.
    # If they aren't scrubbed, the test gate "passes" against this
    # test_cmd and the branch gets pushed; we assert the opposite.
    script = (
        'test -z "$VIRTUAL_ENV" && test -z "$UV_PROJECT_ENVIRONMENT" '
        '|| { echo "LEAKED VIRTUAL_ENV=$VIRTUAL_ENV UV_PROJECT_ENVIRONMENT='
        '$UV_PROJECT_ENVIRONMENT"; exit 1; }'
    )
    # Should pass (zero exit) because the scrubbed env hides both vars.
    git_ops.branch_and_push(
        repo_root=repo,
        base_branch="main",
        new_branch="rae-local-agent/gate-env-scrubbed",
        diff_text=_HELLO_DIFF,
        commit_message="s",
        gate_cmds=[["sh", "-c", script]],
    )


def test_test_cmd_runs_with_filtered_path(repo_pair, monkeypatch):
    """rae-local-agent's venv bin is not on the subprocess PATH."""
    repo, _ = repo_pair
    # Original PATH is preserved by scrubbing except for the fragment match.
    monkeypatch.setenv(
        "PATH",
        "/home/x/projects/rae-local-agent/.venv/bin:" + os.environ.get("PATH", ""),
    )

    script = (
        'case ":$PATH:" in '
        '*":/home/x/projects/rae-local-agent/.venv/bin:"*) '
        'echo "LEAK: $PATH"; exit 1 ;; esac'
    )
    git_ops.branch_and_push(
        repo_root=repo,
        base_branch="main",
        new_branch="rae-local-agent/gate-path-filtered",
        diff_text=_HELLO_DIFF,
        commit_message="s",
        gate_cmds=[["sh", "-c", script]],
    )


# ---------- draft PR creation (B5) --------------------------------------


def test_summarize_takes_first_nonempty_line():
    assert git_ops._summarize("add type hints\nto files foo and bar") == "add type hints"


def test_summarize_skips_leading_whitespace_lines():
    assert git_ops._summarize("\n\n  rename greet to say_hello\n") == "rename greet to say_hello"


def test_summarize_falls_back_when_spec_is_blank():
    assert git_ops._summarize("   \n\n   ") == "rae-local-agent transformation"


def test_summarize_passes_through_short_lines_unchanged():
    """A line at exactly MAX_TITLE_LEN is returned verbatim."""
    s = "x" * git_ops.MAX_TITLE_LEN
    assert git_ops._summarize(s) == s


def test_summarize_truncates_long_line_at_word_boundary_with_ellipsis():
    long = (
        "Add a return type annotation `-> ResponseReturnValue` to every function "
        "in this file decorated with the categories blueprint route."
    )
    out = git_ops._summarize(long)
    assert len(out) <= git_ops.MAX_TITLE_LEN
    assert out.endswith("…")
    # The remainder before the ellipsis is a whole-word prefix of the input.
    assert long.startswith(out[:-1])
    assert not out[:-1].endswith(" ")  # no trailing space before the ellipsis


def test_summarize_hard_cuts_when_no_space_in_window():
    """A long token with no spaces falls back to a hard cut + ellipsis."""
    s = "a" * (git_ops.MAX_TITLE_LEN + 20)
    out = git_ops._summarize(s)
    assert len(out) == git_ops.MAX_TITLE_LEN
    assert out.endswith("…")
    assert out[:-1] == "a" * (git_ops.MAX_TITLE_LEN - 1)


def test_create_draft_pr_invokes_gh_with_expected_args(tmp_path, monkeypatch):
    """gh pr create is invoked with --draft, --head, --title, --body, --assignee."""
    calls: list[tuple] = []

    def fake_run(cmd, cwd, capture_output, text):
        calls.append((cmd, cwd))
        return subprocess.CompletedProcess(cmd, 0, "https://github.com/x/y/pull/42\n", "")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    url = git_ops.create_draft_pr(
        repo_root=tmp_path,
        branch="rae-local-agent/abc",
        title="chore: add type hints to foo",
        body="longer context paragraph",
    )

    assert url == "https://github.com/x/y/pull/42"
    assert len(calls) == 1
    cmd, cwd = calls[0]
    assert cmd[:3] == ["gh", "pr", "create"]
    assert "--draft" in cmd
    assert cmd[cmd.index("--head") + 1] == "rae-local-agent/abc"
    assert cmd[cmd.index("--title") + 1] == "chore: add type hints to foo"
    assert cmd[cmd.index("--body") + 1] == "longer context paragraph"
    assert cmd[cmd.index("--assignee") + 1] == "@me"
    assert cwd == tmp_path


def test_create_draft_pr_uses_title_and_body_verbatim(tmp_path, monkeypatch):
    """create_draft_pr does no summarization — caller is authoritative."""
    captured = {}

    def fake_run(cmd, cwd, capture_output, text):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "https://example/pull/1\n", "")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    body = "rename greet to say_hello\n\nalso update all call sites across the repo"
    git_ops.create_draft_pr(
        repo_root=tmp_path,
        branch="rae-local-agent/x",
        title="refactor: rename greet to say_hello",
        body=body,
    )

    cmd = captured["cmd"]
    assert cmd[cmd.index("--title") + 1] == "refactor: rename greet to say_hello"
    assert cmd[cmd.index("--body") + 1] == body


def test_create_draft_pr_raises_gh_error_on_nonzero_exit(tmp_path, monkeypatch):
    """Non-zero gh exit → GhError carrying stderr."""

    def fake_run(cmd, cwd, capture_output, text):
        return subprocess.CompletedProcess(cmd, 1, "", "gh: not authenticated\n")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    with pytest.raises(git_ops.GhError, match="gh pr create failed.*not authenticated"):
        git_ops.create_draft_pr(repo_root=tmp_path, branch="x", title="t", body="b")


# ---------- group routing probes (B9) -----------------------------------


def test_branch_exists_on_origin_false_when_absent(repo_pair):
    """No such branch on origin → False (ls-remote prints nothing)."""
    repo, _ = repo_pair
    assert git_ops.branch_exists_on_origin(repo, "rae-local-agent/nope") is False


def test_branch_exists_on_origin_true_after_push(repo_pair):
    """Push a branch, then the probe should see it."""
    repo, _ = repo_pair
    _run(["git", "checkout", "-q", "-b", "rae-local-agent/present"], repo)
    _run(["git", "commit", "-q", "--allow-empty", "-m", "x"], repo)
    _run(["git", "push", "-q", "-u", "origin", "rae-local-agent/present"], repo)
    _run(["git", "checkout", "-q", "main"], repo)

    assert git_ops.branch_exists_on_origin(repo, "rae-local-agent/present") is True
    assert git_ops.branch_exists_on_origin(repo, "rae-local-agent/other") is False


def test_branch_exists_on_origin_false_on_non_zero_exit(tmp_path, monkeypatch):
    """A git ls-remote failure (no network, no origin) → False, no raise."""

    def fake_run(cmd, cwd, capture_output, text):
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: ...")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
    assert git_ops.branch_exists_on_origin(tmp_path, "x") is False


def test_pr_url_for_branch_returns_url_on_success(tmp_path, monkeypatch):
    """gh prints the PR URL to stdout → we return it stripped."""

    def fake_run(cmd, cwd, capture_output, text):
        assert cmd[:3] == ["gh", "pr", "view"]
        return subprocess.CompletedProcess(cmd, 0, "https://github.com/u/r/pull/42\n", "")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
    assert (
        git_ops.pr_url_for_branch(tmp_path, "rae-local-agent/g") == "https://github.com/u/r/pull/42"
    )


def test_pr_url_for_branch_returns_none_on_no_pr(tmp_path, monkeypatch):
    """gh returns non-zero when no PR is associated → None."""

    def fake_run(cmd, cwd, capture_output, text):
        return subprocess.CompletedProcess(cmd, 1, "", "no pull requests found\n")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
    assert git_ops.pr_url_for_branch(tmp_path, "rae-local-agent/g") is None


def test_pr_url_for_branch_returns_none_on_empty_stdout(tmp_path, monkeypatch):
    """gh succeeded but printed nothing — treat as no PR."""

    def fake_run(cmd, cwd, capture_output, text):
        return subprocess.CompletedProcess(cmd, 0, "\n", "")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
    assert git_ops.pr_url_for_branch(tmp_path, "rae-local-agent/g") is None


def test_branch_and_push_appends_commit_to_existing_group_branch(repo_pair):
    """Later-in-group path: new_branch == base_branch → stack a commit."""
    repo, remote = repo_pair
    # Seed origin with an existing group branch carrying one commit.
    _run(["git", "checkout", "-q", "-b", "rae-local-agent/group-a"], repo)
    (repo / "seed.py").write_text("# seed\n")
    _run(["git", "add", "seed.py"], repo)
    _run(["git", "commit", "-q", "-m", "seed"], repo)
    _run(["git", "push", "-q", "-u", "origin", "rae-local-agent/group-a"], repo)
    _run(["git", "checkout", "-q", "main"], repo)
    _run(["git", "branch", "-D", "rae-local-agent/group-a"], repo)

    # Add a second commit by basing off the group branch itself (what
    # the worker does for a later-in-group job).
    git_ops.branch_and_push(
        repo_root=repo,
        base_branch="rae-local-agent/group-a",  # same ref as new_branch
        new_branch="rae-local-agent/group-a",
        diff_text=_HELLO_DIFF,
        commit_message="second in group",
    )

    # Fetch into a scratch clone and verify the branch now has two
    # commits with `seed.py` still present (not clobbered by a reset).
    scratch = repo.parent / "scratch"
    _run(["git", "clone", "-q", "-b", "rae-local-agent/group-a", str(remote), str(scratch)], repo)
    log = _run(["git", "log", "--format=%s", "-n", "5"], scratch).stdout.splitlines()
    assert "second in group" in log
    assert "seed" in log
    assert (scratch / "seed.py").exists()  # earlier commit preserved
