"""Tests for the Phase B worker loop.

Covers the queue mechanic (B1 — pickup, recovery, sorted order), the
model-handoff output shape (B2 — diff.patch + status.txt), and the git
push step (B3 — dirty-tree pre-flight + branch.txt). The real model
call and real git subprocesses are both stubbed so the tests don't
need a live Ollama or a git working tree. See `test_git_ops.py` for
the actual git plumbing coverage.
"""

from __future__ import annotations

import json
import logging

import pytest

import git_ops
import job
import worker


@pytest.fixture
def jobs_root(tmp_path, monkeypatch):
    for sub in ("pending", "running", "done", "failed"):
        (tmp_path / sub).mkdir()
    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path)
    # Stub git by default — the queue+model tests don't exercise it.
    monkeypatch.setattr(git_ops, "is_clean", lambda _repo_root: True)
    monkeypatch.setattr(
        git_ops,
        "branch_and_push",
        lambda **_: None,
    )
    return tmp_path


def _write_pending(jobs_root, jid: str, spec: str = "noop", scope=None, repo_root=None):
    """Drop a well-formed job record into pending/."""
    rec = {
        "id": jid,
        "spec": spec,
        "scope": scope if scope is not None else [],
        "repo_root": repo_root or str(jobs_root),
        "base_branch": "main",
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


# ---------- B1 queue mechanic (shape updated for B2 done, B3 git stub) ---


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
    rec = {
        "id": "crashed",
        "spec": "s",
        "scope": [],
        "repo_root": str(jobs_root),
        "base_branch": "main",
    }
    (jobs_root / "running" / "crashed.json").write_text(json.dumps(rec))
    assert worker.tick() == 1
    assert not (jobs_root / "running" / "crashed.json").exists()
    assert (jobs_root / "done" / "crashed" / "job.json").exists()


def test_recovery_precedes_pending(jobs_root, monkeypatch, caplog):
    _stub_transformation(monkeypatch)
    caplog.set_level(logging.INFO, logger="rae-local-agent.worker")
    orphan = {
        "id": "orphan",
        "spec": "s",
        "scope": [],
        "repo_root": str(jobs_root),
        "base_branch": "main",
    }
    new = {
        "id": "new",
        "spec": "s",
        "scope": [],
        "repo_root": str(jobs_root),
        "base_branch": "main",
    }
    (jobs_root / "running" / "orphan.json").write_text(json.dumps(orphan))
    (jobs_root / "pending" / "new.json").write_text(json.dumps(new))
    worker.tick()
    msgs = [r.message for r in caplog.records]
    assert "recovering orphan" in msgs
    assert "picked up new" in msgs
    assert msgs.index("recovering orphan") < msgs.index("picked up new")


# ---------- B2 outcome shape (status.txt + diff.patch) -------------------


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
    assert not (out / "branch.txt").exists()


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
    assert (out / "job.json").exists()
    assert (out / "diff.patch").read_text() == ""
    assert not (out / "branch.txt").exists()


# ---------- B3 git push step --------------------------------------------


def test_process_writes_branch_txt_on_successful_push(jobs_root, monkeypatch):
    """Successful push → branch.txt holds `rae-local-agent/<id>`."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    _stub_transformation(monkeypatch, result=canned)
    _write_pending(jobs_root, "job-push")
    worker.tick()

    out = jobs_root / "done" / "job-push"
    assert (out / "status.txt").read_text() == "ok\n"
    assert (out / "branch.txt").read_text() == "rae-local-agent/job-push\n"


def test_process_calls_git_with_record_fields(jobs_root, monkeypatch):
    """The record's base_branch, repo_root, and spec reach git_ops."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    seen: dict = {}
    monkeypatch.setattr(git_ops, "branch_and_push", lambda **kw: seen.update(kw))

    _write_pending(
        jobs_root,
        "job-fields",
        spec="rename foo",
        repo_root=str(jobs_root),
    )
    worker.tick()

    assert seen["base_branch"] == "main"
    assert str(seen["repo_root"]) == str(jobs_root)
    assert seen["new_branch"] == "rae-local-agent/job-fields"
    assert seen["commit_message"] == "rename foo"
    assert seen["diff_text"] == canned


def test_process_refuses_dirty_tree_without_calling_model(jobs_root, monkeypatch):
    """Dirty target repo → `error:dirty_tree`, no model call, no branch."""
    monkeypatch.setattr(git_ops, "is_clean", lambda _repo_root: False)

    calls = []

    def record_call(spec, scope, repo_root):
        calls.append((spec, scope, repo_root))
        return "doesnt matter"

    monkeypatch.setattr(worker, "_run_transformation_sync", record_call)
    _write_pending(jobs_root, "job-dirty")
    worker.tick()

    out = jobs_root / "done" / "job-dirty"
    assert (out / "status.txt").read_text() == "error:dirty_tree\n"
    assert (out / "diff.patch").read_text() == ""
    assert not (out / "branch.txt").exists()
    assert calls == []  # model was NOT called


def test_process_writes_error_status_on_push_failure(jobs_root, monkeypatch):
    """git_ops failures surface as error:GitError in status.txt."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)

    def boom(**_):
        raise git_ops.GitError("git push failed: remote hung up")

    monkeypatch.setattr(git_ops, "branch_and_push", boom)
    _write_pending(jobs_root, "job-pushfail")
    worker.tick()

    out = jobs_root / "done" / "job-pushfail"
    status = (out / "status.txt").read_text()
    assert status.startswith("error:GitError:")
    assert "remote hung up" in status
    assert not (out / "branch.txt").exists()
    # diff.patch is NOT written when the pipeline bails out — the
    # branch wasn't landed, so the on-disk diff would be misleading.
    assert (out / "diff.patch").read_text() == ""
