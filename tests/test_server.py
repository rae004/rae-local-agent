"""Smoke tests for the pure functions in server.py.

Covers the parsing + diffing layer. The httpx call and the MCP tool
decorator are not exercised here — the first needs a live Ollama, the
second is a decorator from the SDK that just registers the function.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

import job
from server import _diff, _make_job_record, _strip_fences

# ---------- _strip_fences -------------------------------------------------


def test_strip_fences_passes_plain_content_through() -> None:
    assert _strip_fences("def foo(): pass") == "def foo(): pass"


def test_strip_fences_strips_language_fence() -> None:
    assert _strip_fences("```python\ndef foo(): pass\n```") == "def foo(): pass"


def test_strip_fences_strips_bare_fence() -> None:
    assert _strip_fences("```\ndef foo(): pass\n```") == "def foo(): pass"


def test_strip_fences_strips_file_label_prefix() -> None:
    assert _strip_fences("FILE: foo.py\ndef foo(): pass") == "def foo(): pass"


def test_strip_fences_strips_outer_whitespace() -> None:
    assert _strip_fences("   def foo(): pass   ") == "def foo(): pass"


# ---------- _diff ---------------------------------------------------------


def test_diff_identical_input_returns_empty() -> None:
    assert _diff("foo.py", "a\nb\n", "a\nb\n") == ""


def test_diff_real_change_produces_unified_diff() -> None:
    d = _diff("foo.py", "a\nb\nc\n", "a\nB\nc\n")
    assert d.startswith("--- a/foo.py")
    assert "+++ b/foo.py" in d
    assert "-b" in d and "+B" in d


def test_diff_normalises_missing_trailing_newline() -> None:
    """Model may drop the trailing newline; difflib must not emit
    `\\ No newline at end of file` markers as a result."""
    d = _diff("foo.py", "a\nb\nc", "a\nB\nc")
    assert "No newline" not in d


def test_diff_output_applies_via_git_apply(tmp_path: pathlib.Path) -> None:
    """The reason the whole rewrite protocol exists: diffs must apply."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    original = "line one\nline two\nline three\nline four\nline five\n"
    rewritten = "line one\nLINE TWO\nline three\nLINE FOUR\nline five\n"

    (tmp_path / "foo.py").write_text(original)
    subprocess.run(["git", "add", "foo.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    diff = _diff("foo.py", original, rewritten)
    (tmp_path / "p.diff").write_text(diff)

    r = subprocess.run(
        ["git", "apply", "--check", "p.diff"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"git apply --check failed: {r.stderr}"

    subprocess.run(["git", "apply", "p.diff"], cwd=tmp_path, check=True)
    assert (tmp_path / "foo.py").read_text() == rewritten


# ---------- _make_job_record (Phase B enqueue_job plumbing) ---------------


def test_make_job_record_shape(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rec = _make_job_record("do X", ["f.py", "g.py"], str(repo))
    assert rec["spec"] == "do X"
    assert rec["scope"] == ["f.py", "g.py"]
    assert rec["repo_root"] == str(repo.resolve())
    assert "T" in rec["created_at"]  # ISO 8601
    ms, _, suffix = rec["id"].partition("-")
    assert ms.isdigit() and len(ms) == 13 and len(suffix) == 6


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
