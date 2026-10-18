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


def failed_job_dir(jid: str) -> Path:
    """Per-job output directory inside failed/. B4+ test-gate failures land here."""
    return failed_dir() / jid


def _finalize_into(path: Path, dest_dir: Path) -> Path:
    """Move a running job's record into its per-job output dir atomically."""
    jid = path.stem
    new = dest_dir / jid / "job.json"
    path.rename(new)
    return new


def finalize_done(path: Path) -> Path:
    """Move a running job's record into its done/<id>/ output dir.

    The output dir is expected to already exist with any artifacts the
    worker wrote (diff.patch, status.txt, …). This is the atomic step
    that signals "job complete": until this rename lands, a crash leaves
    the record in running/ and the next tick re-runs the job, overwriting
    partial artifacts.
    """
    return _finalize_into(path, done_dir())


def finalize_failed(path: Path) -> Path:
    """Move a running job's record into its failed/<id>/ output dir.

    Same atomic semantics as `finalize_done`; the output dir holds the
    diff that would have been pushed plus a `log.txt` with the test
    output. B4+ test-gate failures land here.
    """
    return _finalize_into(path, failed_dir())


def iter_jobs(d: Path) -> list[Path]:
    """Job files in a queue dir, sorted by name (= creation order for ms ids)."""
    return sorted(p for p in d.iterdir() if p.suffix == ".json")


# --- read-side helpers (B6) -----------------------------------------------
# Used by the MCP status tools. The queue's four directories are the single
# source of truth for a job's state — these functions just scan them.


_STATES: tuple[str, ...] = ("pending", "running", "done", "failed")


def _record_path(jid: str, state: str) -> Path:
    """Where the record file lives for a given state."""
    if state == "pending":
        return pending_dir() / f"{jid}.json"
    if state == "running":
        return running_dir() / f"{jid}.json"
    if state == "done":
        return done_job_dir(jid) / "job.json"
    if state == "failed":
        return failed_job_dir(jid) / "job.json"
    raise ValueError(f"unknown state: {state!r}")


def find_record(jid: str) -> tuple[str, dict] | None:
    """Locate a job across all four dirs. Returns (state, record) or None."""
    for state in _STATES:
        p = _record_path(jid, state)
        if p.exists():
            return state, json.loads(p.read_text(encoding="utf-8"))
    return None


def read_artifact(jid: str, state: str, name: str) -> str:
    """Read one artifact file from the per-job output dir.

    Returns the content as text, or empty string if the file (or the
    output dir) does not exist. Only applicable to `done` and `failed`;
    pending/running jobs have no artifacts yet.
    """
    if state == "done":
        d = done_job_dir(jid)
    elif state == "failed":
        d = failed_job_dir(jid)
    else:
        return ""
    p = d / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def list_records(state: str | None = None) -> list[tuple[str, dict]]:
    """Enumerate records across queue dirs. Returns [(state, record), ...].

    When `state` is given, restricts to that one dir; otherwise walks all
    four. Order within each dir is by filename (= creation order for the
    ms-timestamp ids). Callers that want a global sort should re-sort.
    """
    states = [state] if state else list(_STATES)
    for s in states:
        if s not in _STATES:
            raise ValueError(f"unknown state: {s!r}")

    results: list[tuple[str, dict]] = []
    for s in states:
        if s in ("pending", "running"):
            d = pending_dir() if s == "pending" else running_dir()
            for p in iter_jobs(d):
                results.append((s, json.loads(p.read_text(encoding="utf-8"))))
        else:
            d = done_dir() if s == "done" else failed_dir()
            for sub in sorted(d.iterdir()):
                if not sub.is_dir():
                    continue
                rec_path = sub / "job.json"
                if rec_path.exists():
                    results.append((s, json.loads(rec_path.read_text(encoding="utf-8"))))
    return results
