"""Shared helpers for the Phase B job queue.

Jobs are JSON files that flow through four directories:

    jobs/pending/   just-enqueued, waiting for the worker
    jobs/running/   claimed by the worker, mid-process
    jobs/done/      finished successfully
    jobs/failed/    finished with an error (added in B4)

Moves between directories use `os.rename`, which is atomic within a
filesystem — the worker can be killed at any point and the on-disk state
remains consistent: a job is in exactly one directory, never zero or two.

Directory lookups go through small functions (not module constants) so
tests can monkeypatch `JOBS_ROOT` and have the rest follow.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

JOBS_ROOT = Path(__file__).parent / "jobs"


def pending_dir() -> Path:
    return JOBS_ROOT / "pending"


def running_dir() -> Path:
    return JOBS_ROOT / "running"


def done_dir() -> Path:
    return JOBS_ROOT / "done"


def failed_dir() -> Path:
    return JOBS_ROOT / "failed"


def new_id() -> str:
    """Short, sortable job id: 13-digit ms epoch + 6 hex random."""
    return f"{int(time.time() * 1000):013d}-{secrets.token_hex(3)}"


def write_pending(record: dict) -> str:
    """Write a job record to pending/ atomically. Returns the id.

    Writes to a `.json.tmp` sibling and renames into place so a reader
    (the worker) never sees a half-written file.
    """
    jid = record["id"]
    dest = pending_dir() / f"{jid}.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.rename(dest)
    return jid


def move(path: Path, dest_dir: Path) -> Path:
    """Atomic move of a job file into dest_dir. Returns the new path."""
    new = dest_dir / path.name
    path.rename(new)
    return new


def done_job_dir(jid: str) -> Path:
    """Per-job output directory inside done/. B2+ artifacts land here."""
    return done_dir() / jid


def finalize_done(path: Path) -> Path:
    """Move a running job's record into its done/<id>/ output dir.

    The output dir is expected to already exist with any artifacts the
    worker wrote (diff.patch, status.txt). This is the atomic step that
    signals "job complete": until this rename lands, a crash leaves the
    record in running/ and the next tick re-runs the job, overwriting
    partial artifacts.
    """
    jid = path.stem
    new = done_job_dir(jid) / "job.json"
    path.rename(new)
    return new


def iter_jobs(d: Path) -> list[Path]:
    """Job files in a queue dir, sorted by name (= creation order for ms ids)."""
    return sorted(p for p in d.iterdir() if p.suffix == ".json")
