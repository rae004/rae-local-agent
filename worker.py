"""Phase B worker (B4 — test gate).

Watches `jobs/pending/`, picks jobs up, runs the model handoff, applies
the diff in an isolated worktree, runs the target repo's tests, and
only on a passing suite pushes a `rae-local-agent/<job-id>` branch to
origin. Each move between queue directories is an atomic `os.rename`,
so a crash leaves the job in exactly one state.

Shape of a completed job after B4:

    jobs/done/<id>/
        job.json       # the original record (moved from running/)
        diff.patch     # the model-produced unified diff (possibly empty)
        status.txt     # one line: `ok | model_declined | error:<msg>`
        branch.txt     # pushed branch name (only when status is `ok`)

    jobs/failed/<id>/
        job.json       # the original record
        diff.patch     # the diff that was applied but didn't pass tests
        status.txt     # `tests_failed`
        log.txt        # combined stdout+stderr of the test command

Pre-flight: before any model call, the worker checks the target repo
is clean (`git status --porcelain` empty). A dirty tree aborts the job
with `error:dirty_tree` and lands in done/.

Test gate (B4): after `git apply` in the worktree, the worker runs the
target repo's test command (default `uv run pytest -q`, overridable
per-job). Non-zero exit → job to failed/, no branch pushed. The spec
can set `skip_tests: true` to bypass the gate for intentional-behavior-
change jobs.

Partial-crash semantics: artifacts are written before the final rename
of `running/<id>.json` → `done|failed/<id>/job.json`. If the worker
dies between the two, the next tick's recovery pass sees the record
still in running/ and re-runs the transformation, overwriting the
stale artifacts. The job is "complete" iff the record has been
finalized into either done/ or failed/.

Not yet in scope: draft PR creation (B5), status tool (B6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from pathlib import Path

import git_ops
import job
from transform import run_transformation

log = logging.getLogger("rae-local-agent.worker")

POLL_SECONDS = 1.0
BRANCH_PREFIX = "rae-local-agent"
DEFAULT_TEST_CMD = ["uv", "run", "pytest", "-q"]


class _StopFlag:
    stopped = False


_flag = _StopFlag()


def _on_signal(_signum, _frame) -> None:
    _flag.stopped = True
    log.info("stop requested")


def _run_transformation_sync(spec: str, scope: list[str], repo_root: str) -> str:
    """Sync wrapper so `process` doesn't have to be async. Walking-skeleton
    style; if latency becomes a problem the whole loop can go async later."""
    return asyncio.run(run_transformation(spec, scope, repo_root))


def _test_cmd_for(record: dict) -> list[str] | None:
    """None iff tests are skipped, else the record's test_cmd or the default."""
    if record.get("skip_tests"):
        return None
    return record.get("test_cmd") or DEFAULT_TEST_CMD


def _handle_job(record: dict, jid: str) -> tuple[str, str, str, str]:
    """Run the full pipeline for one job.

    Returns (diff, status, branch, test_log):

    - diff: unified diff produced by the model (empty on dirty_tree or
      early failure; non-empty on model-produced output, retained on
      tests_failed so the user can see what was tried).
    - status: `ok | model_declined | tests_failed | error:<msg>`.
    - branch: pushed branch name (only when status is `ok`).
    - test_log: test-command output (only when status is `tests_failed`).
    """
    repo_root = Path(record["repo_root"])
    try:
        if not git_ops.is_clean(repo_root):
            return "", "error:dirty_tree", "", ""

        diff = _run_transformation_sync(
            record["spec"],
            record["scope"],
            record["repo_root"],
        )
        if not diff:
            return "", "model_declined", "", ""

        branch = f"{BRANCH_PREFIX}/{jid}"
        try:
            git_ops.branch_and_push(
                repo_root=repo_root,
                base_branch=record.get("base_branch", "main"),
                new_branch=branch,
                diff_text=diff,
                commit_message=record["spec"],
                test_cmd=_test_cmd_for(record),
            )
        except git_ops.TestsFailedError as e:
            return diff, "tests_failed", "", e.log
        return diff, "ok", branch, ""
    except Exception as e:
        return "", f"error:{type(e).__name__}: {e}", "", ""


def process(path: Path) -> str:
    """Run the job at `path` and write artifacts into the right output dir.

    Returns the status string so `tick` can route the record to done/ or
    failed/. See `_handle_job` for the pipeline.
    """
    jid = path.stem
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        diff, status, branch, test_log = _handle_job(record, jid)
    except Exception as e:
        diff, status, branch, test_log = "", f"error:{type(e).__name__}: {e}", "", ""
        log.exception("job %s failed to load record", jid)

    out_dir = job.failed_job_dir(jid) if status == "tests_failed" else job.done_job_dir(jid)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "diff.patch").write_text(diff, encoding="utf-8")
    (out_dir / "status.txt").write_text(status + "\n", encoding="utf-8")
    if branch:
        (out_dir / "branch.txt").write_text(branch + "\n", encoding="utf-8")
    if test_log:
        (out_dir / "log.txt").write_text(test_log, encoding="utf-8")
    log.info("job %s %s", jid, status)
    return status


def _finalize_for(status: str):
    """Pick the right queue-dir finalize based on the terminal status."""
    return job.finalize_failed if status == "tests_failed" else job.finalize_done


def tick() -> int:
    """One pass of the loop: recoveries first, then new pending. Returns count."""
    n = 0
    for p in job.iter_jobs(job.running_dir()):
        log.info("recovering %s", p.stem)
        status = process(p)
        _finalize_for(status)(p)
        n += 1
    for p in job.iter_jobs(job.pending_dir()):
        log.info("picked up %s", p.stem)
        running_path = job.move(p, job.running_dir())
        status = process(running_path)
        _finalize_for(status)(running_path)
        n += 1
    return n


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("worker up")
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    while not _flag.stopped:
        tick()
        if _flag.stopped:
            break
        time.sleep(POLL_SECONDS)
    log.info("worker down")


if __name__ == "__main__":
    main()
