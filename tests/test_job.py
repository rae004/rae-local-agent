"""Tests for the job queue helpers."""

from __future__ import annotations

import json

import pytest

import job


@pytest.fixture
def jobs_root(tmp_path, monkeypatch):
    for sub in ("pending", "running", "done", "failed"):
        (tmp_path / sub).mkdir()
    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path)
    return tmp_path


def test_new_id_unique():
    assert len({job.new_id() for _ in range(50)}) == 50


def test_new_id_format():
    jid = job.new_id()
    ms, _, suffix = jid.partition("-")
    assert ms.isdigit() and len(ms) == 13
    assert len(suffix) == 6
    assert all(c in "0123456789abcdef" for c in suffix)


def test_write_pending_atomic(jobs_root):
    rec = {"id": "abc-xyz", "spec": "do X", "scope": ["f.py"], "repo_root": "/r"}
    jid = job.write_pending(rec)
    assert jid == "abc-xyz"

    path = jobs_root / "pending" / "abc-xyz.json"
    assert path.exists()
    assert json.loads(path.read_text()) == rec

    # Temp file should have been renamed away.
    assert not list(jobs_root.rglob("*.tmp"))


def test_move_atomic(jobs_root):
    src = jobs_root / "pending" / "x.json"
    src.write_text("{}")
    new = job.move(src, jobs_root / "running")
    assert new == jobs_root / "running" / "x.json"
    assert new.exists()
    assert not src.exists()


def test_iter_jobs_sorted_filters_non_json(jobs_root):
    d = jobs_root / "pending"
    (d / ".gitkeep").touch()
    (d / "b.json").touch()
    (d / "a.json").touch()
    (d / "halfwritten.json.tmp").touch()
    result = job.iter_jobs(d)
    assert [p.name for p in result] == ["a.json", "b.json"]


def test_done_job_dir_builds_path(jobs_root):
    assert job.done_job_dir("abc") == jobs_root / "done" / "abc"


def test_finalize_done_moves_record_into_output_dir(jobs_root):
    """The record ends up at done/<id>/job.json alongside artifacts."""
    out = jobs_root / "done" / "x"
    out.mkdir()
    (out / "diff.patch").write_text("...")

    src = jobs_root / "running" / "x.json"
    src.write_text('{"id":"x"}')

    new = job.finalize_done(src)
    assert new == out / "job.json"
    assert new.read_text() == '{"id":"x"}'
    assert not src.exists()
    assert (out / "diff.patch").read_text() == "..."


def test_failed_job_dir_builds_path(jobs_root):
    assert job.failed_job_dir("y") == jobs_root / "failed" / "y"


def test_finalize_failed_moves_record_into_failed_output_dir(jobs_root):
    """Same atomic semantics as finalize_done, but into failed/<id>/."""
    out = jobs_root / "failed" / "y"
    out.mkdir()
    (out / "log.txt").write_text("tests failed")

    src = jobs_root / "running" / "y.json"
    src.write_text('{"id":"y"}')

    new = job.finalize_failed(src)
    assert new == out / "job.json"
    assert new.read_text() == '{"id":"y"}'
    assert not src.exists()
    assert (out / "log.txt").read_text() == "tests failed"
