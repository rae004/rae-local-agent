"""Git + gh plumbing used by the Phase B worker (B3+).

Three concerns:

- `is_clean(repo_root)` — pre-flight: does the user have uncommitted
  changes in the target repo? If so the worker refuses the job entirely.
  Spirit of the rule: don't interfere with in-flight work there.
- `branch_and_push(...)` — take a diff produced by `transform.py`, land
  it as a pushed branch named `rae-local-agent/<job-id>` on the target
  repo's origin. Uses `git worktree add` so the user's active checkout
  is never touched. If `test_cmd` is given (B4), runs it in the worktree
  between apply and commit; a non-zero exit raises `TestsFailedError`
  and nothing is pushed.
- `create_draft_pr(...)` — after the push, open a GitHub draft PR via
  the `gh` CLI, assigned to the authed user. Returns the PR URL.

Why subprocess over libraries: no new deps, straight-line reads, and
the error messages come back as git's / gh's own stderr — which is
exactly what we want surfaced in status.txt. If the subprocess churn
ever becomes a problem we can swap to pygit2 / an API client behind
this module's seam.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path

# The worker typically runs as a systemd user service with
# `WorkingDirectory=~/projects/rae-local-agent`, so its process env has
# `VIRTUAL_ENV` and friends pointing at rae-local-agent's own venv. When
# the test subprocess inherits that env, `uv run pytest -q` from the
# worktree resolves to rae-local-agent's venv (which doesn't have the
# target repo's deps) instead of the target repo's own project.
# These are the env vars we strip to force uv to re-resolve from cwd.
_UV_ENV_VARS_TO_STRIP = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR")

# The venv dir name on the worker side; stripped from PATH entries so
# uv doesn't pick up the worker's pytest even without `uv run`.
_WORKER_VENV_FRAGMENT = "/rae-local-agent/.venv/bin"

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


class GhError(RuntimeError):
    """A gh subprocess returned non-zero. The message carries stderr."""


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git, raise GitError on non-zero with stderr as the message."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        step = " ".join(cmd[:2])  # e.g. "git fetch", "git apply"
        raise GitError(f"{step} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def _scrubbed_test_env() -> dict[str, str]:
    """Build a subprocess env with the worker's uv context removed.

    Lets `uv run pytest -q` in the worktree re-resolve the project from
    cwd (the target repo's pyproject) instead of inheriting the worker's
    `VIRTUAL_ENV` and short-circuiting to rae-local-agent's venv. Also
    strips rae-local-agent's `.venv/bin` from PATH so a bare `pytest`
    invocation doesn't pick up the worker's binary either.
    """
    env = os.environ.copy()
    for k in _UV_ENV_VARS_TO_STRIP:
        env.pop(k, None)
    path = env.get("PATH", "")
    env["PATH"] = ":".join(p for p in path.split(":") if _WORKER_VENV_FRAGMENT not in p)
    return env


def _link_target_venv(repo_root: Path, wt: Path) -> None:
    """Reuse the target repo's venv in the worktree so uv doesn't rebuild.

    `git worktree add` doesn't copy `.venv`, so `uv run` in the worktree
    would otherwise build a fresh venv from scratch per job — correct,
    but slow (dependency resolution + install on every mechanical sweep).
    Symlinking the target repo's existing venv in makes the test gate
    reuse what's already there.

    Stale when the diff changes `pyproject.toml` or `uv.lock`, which the
    gate catches honestly (import errors on new deps, or old deps gone).
    No-op when the target has no `.venv` yet or the symlink already exists.
    """
    venv_src = repo_root / ".venv"
    if not venv_src.is_dir():
        return
    link = wt / ".venv"
    if link.exists() or link.is_symlink():
        return
    # Non-fatal: without the link, uv will build its own venv. Slower but
    # still correct, so swallow and let the gate run.
    with contextlib.suppress(OSError):
        link.symlink_to(venv_src, target_is_directory=True)


def _run_gate_cmds(gate_cmds: list[list[str]], wt: Path) -> None:
    """Run gate commands sequentially in `wt`.

    Each command's stderr is merged into stdout and emitted into a
    per-command log slice with a header, so a `tests_failed` log makes
    clear which command died. The first non-zero exit raises
    `TestsFailedError` carrying everything run up to and including the
    failure; later commands are not executed.
    """
    parts: list[str] = []
    total = len(gate_cmds)
    for i, cmd in enumerate(gate_cmds, start=1):
        header = f"=== gate {i}/{total}: {' '.join(cmd)} ===\n"
        parts.append(header)
        tr = subprocess.run(
            cmd,
            cwd=wt,
            env=_scrubbed_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        parts.append(tr.stdout)
        if not tr.stdout.endswith("\n"):
            parts.append("\n")
        if tr.returncode != 0:
            parts.append(f"=== gate {i}/{total} FAILED (rc={tr.returncode}) ===\n")
            raise TestsFailedError("".join(parts))


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
    gate_cmds: list[list[str]] | None = None,
) -> None:
    """Land a diff as a pushed branch on origin, isolated via worktree.

    Steps: fetch origin/<base>, create a worktree with a fresh branch
    rooted at origin/<base>, write the diff, `git apply`. If `gate_cmds`
    is given, run each command in sequence in the worktree with stderr
    merged into stdout — the first non-zero exit raises
    `TestsFailedError` with the concatenated per-command log so the
    operator can see which gate command died, and nothing is pushed.
    On pass, add+commit with a baked-in author, push `-u origin
    <new_branch>`.

    Test subprocess env: the worker's `VIRTUAL_ENV`/`UV_PROJECT_ENVIRONMENT`
    and rae-local-agent's `.venv/bin` on PATH are stripped so `uv run`
    resolves the project from the worktree (the target repo), not from
    the worker's own venv. The target repo's existing `.venv` is
    symlinked into the worktree so uv reuses it instead of building a
    fresh one every job. See `_scrubbed_test_env` + `_link_target_venv`.

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

            if gate_cmds:
                _link_target_venv(repo_root, wt)
                _run_gate_cmds(gate_cmds, wt)

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


MAX_TITLE_LEN = 72  # ~git subject convention; GitHub list views truncate near here.


def _summarize(spec: str) -> str:
    """First non-empty line of the spec, capped at MAX_TITLE_LEN.

    The cap trims at a word boundary when there's one in the window and
    appends `…`. Used as the fallback PR title when the caller didn't
    pass an explicit one. Specs are written for the model (dense, often
    one long sentence), so the untruncated first line was blowing past
    GitHub's title display width.
    """
    for line in spec.strip().splitlines():
        s = line.strip()
        if s:
            if len(s) <= MAX_TITLE_LEN:
                return s
            # Leave one char for the ellipsis; break at the last space.
            cut = s[: MAX_TITLE_LEN - 1].rsplit(" ", 1)[0]
            if not cut:  # no space in the window — hard cut.
                cut = s[: MAX_TITLE_LEN - 1]
            return cut + "…"
    return "rae-local-agent transformation"


def create_draft_pr(repo_root: Path, branch: str, title: str, body: str) -> str:
    """Open a GitHub draft PR for `branch` on origin. Returns the PR URL.

    Caller supplies the exact title and body — summarization + fallback
    happen upstream in the worker. Assigned to `@me` (whoever is authed
    with `gh` in the worker's env). Non-zero exit from `gh` raises
    `GhError` with stderr as the message.
    """
    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
            "--assignee",
            "@me",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GhError(f"gh pr create failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def branch_exists_on_origin(repo_root: Path, branch: str) -> bool:
    """True iff `origin` has a ref at `refs/heads/<branch>`.

    Used by the worker to tell "first job of a group" (no branch yet,
    open a PR) from "later job of a group" (branch there, add a commit).
    `git ls-remote` is a cheap read-only probe; non-zero exit is treated
    as "no network / no branch" — we return False and let the caller try
    to push the branch fresh.
    """
    result = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip() != ""


def pr_url_for_branch(repo_root: Path, branch: str) -> str | None:
    """Return the open PR URL for `branch` on origin, or None if none.

    Lets a later-in-group job report back the existing PR URL instead
    of trying to open a second PR for the same branch. `gh pr view`
    exits non-zero when no PR is associated with the ref — we treat
    that as "no PR" and return None.
    """
    result = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None
