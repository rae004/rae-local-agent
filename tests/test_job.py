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


# ---------- B6 read-side helpers ----------------------------------------


def _write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


def test_find_record_in_pending(jobs_root):
    rec = {"id": "p1", "spec": "s", "created_at": "2026-10-18T00:00:00Z"}
    _write_record(jobs_root / "pending" / "p1.json", rec)
    found = job.find_record("p1")
    assert found == ("pending", rec)


def test_find_record_in_running(jobs_root):
    rec = {"id": "r1", "spec": "s", "created_at": "2026-10-18T00:00:00Z"}
    _write_record(jobs_root / "running" / "r1.json", rec)
    found = job.find_record("r1")
    assert found == ("running", rec)


def test_find_record_in_done(jobs_root):
    rec = {"id": "d1", "spec": "s", "created_at": "2026-10-18T00:00:00Z"}
    _write_record(jobs_root / "done" / "d1" / "job.json", rec)
    found = job.find_record("d1")
    assert found == ("done", rec)


def test_find_record_in_failed(jobs_root):
    rec = {"id": "f1", "spec": "s", "created_at": "2026-10-18T00:00:00Z"}
    _write_record(jobs_root / "failed" / "f1" / "job.json", rec)
    found = job.find_record("f1")
    assert found == ("failed", rec)


def test_find_record_not_found_returns_none(jobs_root):
    assert job.find_record("nope") is None


def test_read_artifact_returns_content_when_present(jobs_root):
    (jobs_root / "done" / "x").mkdir()
    (jobs_root / "done" / "x" / "diff.patch").write_text("DIFF")
    assert job.read_artifact("x", "done", "diff.patch") == "DIFF"


def test_read_artifact_returns_empty_when_file_missing(jobs_root):
    (jobs_root / "done" / "y").mkdir()
    assert job.read_artifact("y", "done", "diff.patch") == ""


def test_read_artifact_returns_empty_for_pending_or_running(jobs_root):
    """Pending/running jobs have no output dir yet; read_artifact is a no-op."""
    assert job.read_artifact("whatever", "pending", "diff.patch") == ""
    assert job.read_artifact("whatever", "running", "diff.patch") == ""


def test_list_records_across_all_dirs(jobs_root):
    _write_record(
        jobs_root / "pending" / "a.json",
        {"id": "a", "spec": "s", "created_at": "2026-10-18T01:00:00Z"},
    )
    _write_record(
        jobs_root / "running" / "b.json",
        {"id": "b", "spec": "s", "created_at": "2026-10-18T02:00:00Z"},
    )
    _write_record(
        jobs_root / "done" / "c" / "job.json",
        {"id": "c", "spec": "s", "created_at": "2026-10-18T03:00:00Z"},
    )
    _write_record(
        jobs_root / "failed" / "d" / "job.json",
        {"id": "d", "spec": "s", "created_at": "2026-10-18T04:00:00Z"},
    )
    results = job.list_records()
    ids_states = [(s, r["id"]) for s, r in results]
    assert ("pending", "a") in ids_states
    assert ("running", "b") in ids_states
    assert ("done", "c") in ids_states
    assert ("failed", "d") in ids_states
    assert len(results) == 4


def test_list_records_filters_by_state(jobs_root):
    _write_record(jobs_root / "pending" / "a.json", {"id": "a", "spec": "s", "created_at": "t"})
    _write_record(
        jobs_root / "done" / "c" / "job.json", {"id": "c", "spec": "s", "created_at": "t"}
    )
    results = job.list_records(state="done")
    assert [r["id"] for _, r in results] == ["c"]


def test_list_records_rejects_unknown_state(jobs_root):
    with pytest.raises(ValueError, match="unknown state"):
        job.list_records(state="archived")


def test_list_records_skips_dirs_without_job_json(jobs_root):
    """A directory under done/ that lacks job.json (crash mid-finalize)
    should not blow up list_records — it's just absent from the listing."""
    (jobs_root / "done" / "stray").mkdir()
    (jobs_root / "done" / "stray" / "diff.patch").write_text("...")
    results = job.list_records(state="done")
    assert results == []
