"""Phase B worker (B3 — git branch + commit + push).

Watches `jobs/pending/`, picks jobs up, runs the model handoff, and
on a non-empty diff pushes a `rae-local-agent/<job-id>` branch to the
target repo's origin. Each move between queue directories is an atomic
`os.rename`, so a crash leaves the job in exactly one state. On every
tick, anything already in `jobs/running/` is reconciled first — treated
as a recovery of a prior worker that died mid-process, finished to
`done/`.

Shape of a done job after B3:

    jobs/done/<id>/
        job.json       # the original record (moved from running/)
        diff.patch     # the model-produced unified diff (possibly empty)
        status.txt     # one line: `ok | model_declined | error:<msg>`
        branch.txt     # pushed branch name (only when status is `ok`)

Pre-flight: before any model call, the worker checks the target repo
is clean (`git status --porcelain` empty). A dirty tree aborts the job
with `error:dirty_tree` — don't interfere with the user's in-flight
work there.

Partial-crash semantics: artifacts are written before the final rename
of `running/<id>.json` → `done/<id>/job.json`. If the worker dies
between the two, the next tick's recovery pass sees the record still
in running/ and re-runs the transformation, overwriting the stale
artifacts. The job is "complete" iff the record is in done/<id>/.

Not yet in scope: test gate (B4), draft PR (B5).
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


def _handle_job(record: dict, jid: str) -> tuple[str, str, str]:
    """Run the full pipeline for one job and return (diff, status, branch).

    `branch` is empty unless we pushed one. Any exception becomes an
    `error:<class>: <msg>` status — the queue invariant is that the
    worker never gets stuck on a bad job.
    """
    repo_root = Path(record["repo_root"])
    try:
        if not git_ops.is_clean(repo_root):
            return "", "error:dirty_tree", ""

        diff = _run_transformation_sync(
            record["spec"],
            record["scope"],
            record["repo_root"],
        )
        if not diff:
            return "", "model_declined", ""

        branch = f"{BRANCH_PREFIX}/{jid}"
        git_ops.branch_and_push(
            repo_root=repo_root,
            base_branch=record.get("base_branch", "main"),
            new_branch=branch,
            diff_text=diff,
            commit_message=record["spec"],
        )
        return diff, "ok", branch
    except Exception as e:
        return "", f"error:{type(e).__name__}: {e}", ""


def process(path: Path) -> None:
    """Run the job at `path` and write artifacts into its done/<id>/ dir.

    See `_handle_job` for the pipeline. The outputs land as diff.patch,
    status.txt, and (when `ok`) branch.txt.
    """
    jid = path.stem
    out_dir = job.done_job_dir(jid)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        diff, status, branch = _handle_job(record, jid)
    except Exception as e:
        diff, status, branch = "", f"error:{type(e).__name__}: {e}", ""
        log.exception("job %s failed to load record", jid)
    (out_dir / "diff.patch").write_text(diff, encoding="utf-8")
    (out_dir / "status.txt").write_text(status + "\n", encoding="utf-8")
    if branch:
        (out_dir / "branch.txt").write_text(branch + "\n", encoding="utf-8")
    log.info("job %s %s", jid, status)


def tick() -> int:
    """One pass of the loop: recoveries first, then new pending. Returns count."""
    n = 0
    for p in job.iter_jobs(job.running_dir()):
        log.info("recovering %s", p.stem)
        process(p)
        job.finalize_done(p)
        n += 1
    for p in job.iter_jobs(job.pending_dir()):
        log.info("picked up %s", p.stem)
        running_path = job.move(p, job.running_dir())
        process(running_path)
        job.finalize_done(running_path)
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
