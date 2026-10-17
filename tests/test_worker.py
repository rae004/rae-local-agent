"""Tests for the Phase B worker loop (B1)."""

from __future__ import annotations

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


def test_tick_empty_queue(jobs_root):
    assert worker.tick() == 0


def test_tick_picks_up_pending(jobs_root):
    (jobs_root / "pending" / "001.json").write_text('{"id":"001"}')
    assert worker.tick() == 1
    assert (jobs_root / "done" / "001.json").exists()
    assert list((jobs_root / "pending").glob("*.json")) == []
    assert list((jobs_root / "running").glob("*.json")) == []


def test_tick_processes_in_sorted_order(jobs_root, caplog):
    caplog.set_level(logging.INFO, logger="rae-local-agent.worker")
    (jobs_root / "pending" / "003.json").write_text("{}")
    (jobs_root / "pending" / "001.json").write_text("{}")
    (jobs_root / "pending" / "002.json").write_text("{}")
    assert worker.tick() == 3
    picked = [r.message for r in caplog.records if r.message.startswith("picked up ")]
    assert picked == ["picked up 001", "picked up 002", "picked up 003"]


def test_tick_recovers_orphaned_running(jobs_root):
    """A job left in running/ by a crashed worker gets finished to done/."""
    (jobs_root / "running" / "crashed.json").write_text("{}")
    assert worker.tick() == 1
    assert not (jobs_root / "running" / "crashed.json").exists()
    assert (jobs_root / "done" / "crashed.json").exists()


def test_recovery_precedes_pending(jobs_root, caplog):
    caplog.set_level(logging.INFO, logger="rae-local-agent.worker")
    (jobs_root / "running" / "orphan.json").write_text("{}")
    (jobs_root / "pending" / "new.json").write_text("{}")
    worker.tick()
    msgs = [r.message for r in caplog.records]
    assert "recovering orphan" in msgs
    assert "picked up new" in msgs
    assert msgs.index("recovering orphan") < msgs.index("picked up new")
