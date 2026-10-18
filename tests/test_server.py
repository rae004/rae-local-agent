"""Smoke tests for the MCP server surface.

The httpx call and the MCP tool decorator are not exercised here — the
first needs a live Ollama, the second is a decorator from the SDK that
just registers the function. See `test_transform.py` for the model-handoff
helpers and `test_worker.py` for the queue + output shape.
"""

from __future__ import annotations

import json
import pathlib

import pytest

import job
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
