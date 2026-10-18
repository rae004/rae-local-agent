"""Tests for the Phase B worker loop.

Covers both the queue mechanic (B1 — pickup, recovery, sorted order) and
the model-handoff output shape (B2 — diff.patch + status.txt in
jobs/done/<id>/ with status `ok | model_declined | error:<msg>`).

The real model call is stubbed via `_run_transformation_sync` so the
tests don't need a live Ollama.
"""

from __future__ import annotations

import json
import logging

import pytest

import job
import worker


@pytest.fixture
def jobs_root(tmp_path, monkeypatch):
    for sub in ("pending", "running", "done", "failed"):
        (tmp_path / sub).mkdir()
    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path)
    return tmp_path


def _write_pending(jobs_root, jid: str, spec: str = "noop", scope=None, repo_root=None):
    """Drop a well-formed job record into pending/."""
    rec = {
        "id": jid,
        "spec": spec,
        "scope": scope if scope is not None else [],
        "repo_root": repo_root or str(jobs_root),
    }
    (jobs_root / "pending" / f"{jid}.json").write_text(json.dumps(rec))
    return rec


def _stub_transformation(monkeypatch, result: str = "") -> None:
    """Replace the model handoff with a canned return value."""
    monkeypatch.setattr(
        worker,
        "_run_transformation_sync",
        lambda spec, scope, repo_root: result,
    )


# ---------- B1 queue mechanic (updated for the B2 done shape) ------------


def test_tick_empty_queue(jobs_root):
    assert worker.tick() == 0


def test_tick_picks_up_pending(jobs_root, monkeypatch):
    _stub_transformation(monkeypatch)
    _write_pending(jobs_root, "001")
    assert worker.tick() == 1
    assert (jobs_root / "done" / "001" / "job.json").exists()
    assert list((jobs_root / "pending").glob("*.json")) == []
    assert list((jobs_root / "running").glob("*.json")) == []


def test_tick_processes_in_sorted_order(jobs_root, monkeypatch, caplog):
    _stub_transformation(monkeypatch)
    caplog.set_level(logging.INFO, logger="rae-local-agent.worker")
    _write_pending(jobs_root, "003")
    _write_pending(jobs_root, "001")
    _write_pending(jobs_root, "002")
    assert worker.tick() == 3
    picked = [r.message for r in caplog.records if r.message.startswith("picked up ")]
    assert picked == ["picked up 001", "picked up 002", "picked up 003"]


def test_tick_recovers_orphaned_running(jobs_root, monkeypatch):
    """A job left in running/ by a crashed worker gets finished to done/<id>/."""
    _stub_transformation(monkeypatch)
    rec = {"id": "crashed", "spec": "s", "scope": [], "repo_root": str(jobs_root)}
    (jobs_root / "running" / "crashed.json").write_text(json.dumps(rec))
    assert worker.tick() == 1
    assert not (jobs_root / "running" / "crashed.json").exists()
    assert (jobs_root / "done" / "crashed" / "job.json").exists()


def test_recovery_precedes_pending(jobs_root, monkeypatch, caplog):
    _stub_transformation(monkeypatch)
    caplog.set_level(logging.INFO, logger="rae-local-agent.worker")
    orphan = {"id": "orphan", "spec": "s", "scope": [], "repo_root": str(jobs_root)}
    new = {"id": "new", "spec": "s", "scope": [], "repo_root": str(jobs_root)}
    (jobs_root / "running" / "orphan.json").write_text(json.dumps(orphan))
    (jobs_root / "pending" / "new.json").write_text(json.dumps(new))
    worker.tick()
    msgs = [r.message for r in caplog.records]
    assert "recovering orphan" in msgs
    assert "picked up new" in msgs
    assert msgs.index("recovering orphan") < msgs.index("picked up new")


# ---------- B2 artifact shape --------------------------------------------


def test_process_writes_ok_status_and_diff(jobs_root, monkeypatch):
    """A non-empty diff from the model → status `ok`."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    _stub_transformation(monkeypatch, result=canned)
    _write_pending(jobs_root, "job-ok")
    worker.tick()

    out = jobs_root / "done" / "job-ok"
    assert (out / "job.json").exists()
    assert (out / "diff.patch").read_text() == canned
    assert (out / "status.txt").read_text() == "ok\n"


def test_process_writes_model_declined_on_empty_diff(jobs_root, monkeypatch):
    """An empty diff from the model → status `model_declined`."""
    _stub_transformation(monkeypatch, result="")
    _write_pending(jobs_root, "job-decline")
    worker.tick()

    out = jobs_root / "done" / "job-decline"
    assert (out / "diff.patch").read_text() == ""
    assert (out / "status.txt").read_text() == "model_declined\n"


def test_process_writes_error_status_when_transformation_raises(jobs_root, monkeypatch):
    """Any exception during the model handoff → status `error:<class>: <msg>`."""

    def boom(spec, scope, repo_root):
        raise ValueError("repo_root is not a directory: /nope")

    monkeypatch.setattr(worker, "_run_transformation_sync", boom)
    _write_pending(jobs_root, "job-err")
    worker.tick()

    out = jobs_root / "done" / "job-err"
    status = (out / "status.txt").read_text()
    assert status.startswith("error:ValueError:")
    assert "repo_root is not a directory" in status
    # The record still finalizes into done/ — the queue invariant is
    # "the worker never gets stuck on a bad job."
    assert (out / "job.json").exists()
    assert (out / "diff.patch").read_text() == ""
