"""Smoke tests for the pure functions in transform.py.

Covers the parsing + diffing layer. The httpx call is not exercised here —
it needs a live Ollama. The MCP tool and worker both call
`run_transformation`, which is tested end-to-end via `test_worker.py`'s
stubbed invocation.
"""

from __future__ import annotations

import pathlib
import subprocess

from transform import _diff, _strip_fences

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
