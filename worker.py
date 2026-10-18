"""Phase B worker (B2 — model handoff).

Watches `jobs/pending/`, picks jobs up, runs the model handoff, writes
the diff and a one-line status into a per-job directory under
`jobs/done/<id>/`. Each move between queue directories is an atomic
`os.rename`, so a crash leaves the job in exactly one state. On every
tick, anything already in `jobs/running/` is reconciled first — treated
as a recovery of a prior worker that died mid-process, finished to
`done/`.

Shape of a done job after B2:

    jobs/done/<id>/
        job.json       # the original record (moved from running/)
        diff.patch     # the model-produced unified diff (possibly empty)
        status.txt     # one line: `ok | model_declined | error:<msg>`

Partial-crash semantics: artifacts are written before the final rename
of `running/<id>.json` → `done/<id>/job.json`. If the worker dies
between the two, the next tick's recovery pass sees the record still
in running/ and re-runs the transformation, overwriting the stale
artifacts. The job is "complete" iff the record is in done/<id>/.

Not yet in scope: git branch, test gate, draft PR (B3–B5).
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from pathlib import Path

import job
from transform import run_transformation

log = logging.getLogger("rae-local-agent.worker")

POLL_SECONDS = 1.0


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


def _status_for(diff: str) -> str:
    """ok iff the model actually changed something; model_declined otherwise."""
    return "ok" if diff else "model_declined"


def process(path: Path) -> None:
    """Run the transformation described by the job at `path` and write
    diff.patch + status.txt into its done/<id>/ output dir.

    Any exception is caught and recorded as `error:<msg>` in status.txt
    rather than propagated — the queue invariant is "the worker never
    gets stuck on a bad job." Failures still move to done/ in B2; the
    failed/ bucket is reserved for B4's test gate.
    """
    jid = path.stem
    out_dir = job.done_job_dir(jid)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        diff = _run_transformation_sync(
            record["spec"],
            record["scope"],
            record["repo_root"],
        )
        status = _status_for(diff)
    except Exception as e:
        diff = ""
        status = f"error:{type(e).__name__}: {e}"
        log.exception("job %s failed", jid)
    (out_dir / "diff.patch").write_text(diff, encoding="utf-8")
    (out_dir / "status.txt").write_text(status + "\n", encoding="utf-8")
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
