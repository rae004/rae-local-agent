"""Phase B worker (B1 walking skeleton).

Watches `jobs/pending/`, picks up jobs by moving them to `jobs/running/`,
logs them, and moves them to `jobs/done/`. Each move is an atomic
`os.rename`, so a crash leaves the job in exactly one directory. On every
tick, anything already in `jobs/running/` is reconciled first — treated
as a recovery of a prior worker that died mid-process and finished to
`done/`.

No model handoff, no git, no PR yet — those arrive in B2+. This milestone
proves the queue mechanic in isolation.
"""

from __future__ import annotations

import logging
import signal
import time

import job

log = logging.getLogger("rae-local-agent.worker")

POLL_SECONDS = 1.0


class _StopFlag:
    stopped = False


_flag = _StopFlag()


def _on_signal(_signum, _frame) -> None:
    _flag.stopped = True
    log.info("stop requested")


def process(path) -> None:
    """Per-job work. B1 is a no-op; B2 fills this in with the model handoff."""
    _ = path


def tick() -> int:
    """One pass of the loop: recoveries first, then new pending. Returns count."""
    n = 0
    for p in job.iter_jobs(job.running_dir()):
        log.info("recovering %s", p.stem)
        process(p)
        job.move(p, job.done_dir())
        n += 1
    for p in job.iter_jobs(job.pending_dir()):
        log.info("picked up %s", p.stem)
        running_path = job.move(p, job.running_dir())
        process(running_path)
        job.move(running_path, job.done_dir())
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
