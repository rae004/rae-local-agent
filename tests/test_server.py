"""Smoke tests for the MCP server surface.

The httpx call and the MCP tool decorator are not exercised here — the
first needs a live Ollama, the second is a decorator from the SDK that
just registers the function. See `test_transform.py` for the model-handoff
helpers and `test_worker.py` for the queue + output shape.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

import job
import server
from server import _make_job_record


def test_make_job_record_shape(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rec = _make_job_record("do X", ["f.py", "g.py"], str(repo))
    assert rec["spec"] == "do X"
    assert rec["scope"] == ["f.py", "g.py"]
    assert rec["repo_root"] == str(repo.resolve())
    assert rec["base_branch"] == "main"  # default
    assert "T" in rec["created_at"]  # ISO 8601
    ms, _, suffix = rec["id"].partition("-")
    assert ms.isdigit() and len(ms) == 13 and len(suffix) == 6


def test_make_job_record_accepts_custom_base_branch(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rec = _make_job_record("do X", ["f.py"], str(repo), base_branch="develop")
    assert rec["base_branch"] == "develop"


def test_make_job_record_default_test_cmd_and_skip_tests(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rec = _make_job_record("do X", ["f.py"], str(repo))
    assert rec["test_cmd"] == ["uv", "run", "pytest", "-q"]
    assert rec["skip_tests"] is False


def test_make_job_record_custom_test_cmd(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rec = _make_job_record("do X", ["f.py"], str(repo), test_cmd=["make", "test"])
    assert rec["test_cmd"] == ["make", "test"]


def test_make_job_record_skip_tests_override(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rec = _make_job_record("do X", ["f.py"], str(repo), skip_tests=True)
    assert rec["skip_tests"] is True


# ---------- B6 status tools ---------------------------------------------
#
# The MCP tool decorators wrap the inner coroutine. We call them via
# `asyncio.run` to exercise the actual function body; the decorator is
# the SDK's registration shim and not what we need to test.


def _get_status(job_id: str) -> dict:
    return asyncio.run(server.get_job_status(job_id))


def _list_jobs(state: str | None = None) -> list[dict]:
    return asyncio.run(server.list_jobs(state))


@pytest.fixture
def jobs_root(tmp_path, monkeypatch):
    for sub in ("pending", "running", "done", "failed"):
        (tmp_path / sub).mkdir()
    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path)
    return tmp_path


def _write_record(path: pathlib.Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


# ---------- get_job_status ----------------------------------------------


def test_get_job_status_pending(jobs_root):
    rec = {"id": "p", "spec": "do thing", "created_at": "2026-10-18T01:00:00Z"}
    _write_record(jobs_root / "pending" / "p.json", rec)

    resp = _get_status("p")
    assert resp["id"] == "p"
    assert resp["state"] == "pending"
    assert resp["spec"] == "do thing"
    assert resp["status"] == ""
    assert resp["diff"] == ""


def test_get_job_status_running(jobs_root):
    rec = {"id": "r", "spec": "s", "created_at": "t"}
    _write_record(jobs_root / "running" / "r.json", rec)
    assert _get_status("r")["state"] == "running"


def test_get_job_status_done_with_full_artifacts(jobs_root):
    rec = {"id": "d", "spec": "add type hints", "created_at": "t"}
    base = jobs_root / "done" / "d"
    _write_record(base / "job.json", rec)
    (base / "status.txt").write_text("ok\n")
    (base / "diff.patch").write_text("--- a/foo\n+++ b/foo\n")
    (base / "branch.txt").write_text("rae-local-agent/d\n")
    (base / "pr_url.txt").write_text("https://github.com/u/r/pull/9\n")

    resp = _get_status("d")
    assert resp["state"] == "done"
    assert resp["status"] == "ok"
    assert resp["diff"] == "--- a/foo\n+++ b/foo\n"
    assert resp["branch"] == "rae-local-agent/d"
    assert resp["pr_url"] == "https://github.com/u/r/pull/9"
    assert resp["log"] == ""


def test_get_job_status_failed_includes_log(jobs_root):
    rec = {"id": "f", "spec": "s", "created_at": "t"}
    base = jobs_root / "failed" / "f"
    _write_record(base / "job.json", rec)
    (base / "status.txt").write_text("tests_failed\n")
    (base / "diff.patch").write_text("DIFF")
    (base / "log.txt").write_text("FAIL: assertion failed\n")

    resp = _get_status("f")
    assert resp["state"] == "failed"
    assert resp["status"] == "tests_failed"
    assert resp["diff"] == "DIFF"
    assert resp["log"] == "FAIL: assertion failed\n"
    assert resp["branch"] == ""
    assert resp["pr_url"] == ""


def test_get_job_status_not_found_raises(jobs_root):
    with pytest.raises(ValueError, match="job not found: nope"):
        _get_status("nope")


# ---------- list_jobs ---------------------------------------------------


def test_list_jobs_returns_jobs_from_all_dirs(jobs_root):
    _write_record(
        jobs_root / "pending" / "a.json",
        {"id": "a", "spec": "spec-a", "created_at": "2026-10-18T01:00:00Z"},
    )
    _write_record(
        jobs_root / "done" / "b" / "job.json",
        {"id": "b", "spec": "spec-b", "created_at": "2026-10-18T02:00:00Z"},
    )
    (jobs_root / "done" / "b" / "status.txt").write_text("ok\n")

    result = _list_jobs()
    ids = {r["id"] for r in result}
    assert ids == {"a", "b"}


def test_list_jobs_newest_first(jobs_root):
    _write_record(
        jobs_root / "pending" / "old.json",
        {"id": "old", "spec": "s", "created_at": "2026-10-18T01:00:00Z"},
    )
    _write_record(
        jobs_root / "pending" / "new.json",
        {"id": "new", "spec": "s", "created_at": "2026-10-18T05:00:00Z"},
    )
    result = _list_jobs()
    assert [r["id"] for r in result] == ["new", "old"]


def test_list_jobs_populates_status_for_done_only(jobs_root):
    _write_record(
        jobs_root / "pending" / "p.json",
        {"id": "p", "spec": "s", "created_at": "t"},
    )
    _write_record(
        jobs_root / "done" / "d" / "job.json",
        {"id": "d", "spec": "s", "created_at": "t"},
    )
    (jobs_root / "done" / "d" / "status.txt").write_text("ok\n")

    by_id = {r["id"]: r for r in _list_jobs()}
    assert by_id["p"]["status"] == ""
    assert by_id["d"]["status"] == "ok"


def test_list_jobs_spec_summary_is_first_line(jobs_root):
    _write_record(
        jobs_root / "pending" / "a.json",
        {
            "id": "a",
            "spec": "rename greet to say_hello\n\nalso touch call sites",
            "created_at": "t",
        },
    )
    [row] = _list_jobs()
    assert row["spec_summary"] == "rename greet to say_hello"


def test_list_jobs_state_filter(jobs_root):
    _write_record(
        jobs_root / "pending" / "a.json",
        {"id": "a", "spec": "s", "created_at": "t"},
    )
    _write_record(
        jobs_root / "done" / "b" / "job.json",
        {"id": "b", "spec": "s", "created_at": "t"},
    )
    result = _list_jobs(state="done")
    assert [r["id"] for r in result] == ["b"]


def test_list_jobs_rejects_invalid_state(jobs_root):
    with pytest.raises(ValueError, match="state must be one of"):
        _list_jobs(state="archived")


def test_list_jobs_empty_queues(jobs_root):
    assert _list_jobs() == []


def test_make_job_record_rejects_missing_repo() -> None:
    with pytest.raises(ValueError, match="repo_root is not a directory"):
        _make_job_record("s", ["f"], "/this/path/does/not/exist")


def test_enqueue_round_trip_writes_pending_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: _make_job_record + job.write_pending land a valid file."""
    for sub in ("pending", "running", "done", "failed"):
        (tmp_path / sub).mkdir()
    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path)

    repo = tmp_path / "repo"
    repo.mkdir()
    rec = _make_job_record("rename foo", ["a.py"], str(repo))
    jid = job.write_pending(rec)

    on_disk = json.loads((tmp_path / "pending" / f"{jid}.json").read_text())
    assert on_disk == rec
