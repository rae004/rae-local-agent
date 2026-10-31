"""Smoke tests for the pure functions in transform.py.

Covers the parsing + diffing layer. The httpx call is not exercised here —
it needs a live Ollama. The MCP tool and worker both call
`run_transformation`, which is tested end-to-end via `test_worker.py`'s
stubbed invocation.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from transform import _diff, _expand_scope, _strip_fences

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


# ---------- _expand_scope -------------------------------------------------


def test_expand_scope_passes_files_through(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.ts").write_text("")
    assert _expand_scope(tmp_path, ["a.py", "b.ts"]) == ["a.py", "b.ts"]


def test_expand_scope_file_entry_bypasses_extension_filter(
    tmp_path: pathlib.Path,
) -> None:
    """Direct file entries aren't filtered — only directory expansion is."""
    (tmp_path / "notes.md").write_text("")
    assert _expand_scope(tmp_path, ["notes.md"]) == ["notes.md"]


def test_expand_scope_directory_recurses_and_filters(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "a.ts").write_text("")
    (tmp_path / "lib" / "sub").mkdir()
    (tmp_path / "lib" / "sub" / "b.tsx").write_text("")
    (tmp_path / "lib" / "README.md").write_text("")  # filtered out

    result = _expand_scope(tmp_path, ["lib"])
    assert result == [str(pathlib.Path("lib/a.ts")), str(pathlib.Path("lib/sub/b.tsx"))]


def test_expand_scope_prunes_skip_dirs(tmp_path: pathlib.Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.ts").write_text("")
    (tmp_path / "src" / "node_modules").mkdir()
    (tmp_path / "src" / "node_modules" / "ignored.ts").write_text("")
    (tmp_path / "src" / "__pycache__").mkdir()
    (tmp_path / "src" / "__pycache__" / "cached.py").write_text("")

    assert _expand_scope(tmp_path, ["src"]) == [str(pathlib.Path("src/keep.ts"))]


def test_expand_scope_empty_directory_raises(tmp_path: pathlib.Path) -> None:
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "README.md").write_text("")  # no source files

    with pytest.raises(ValueError, match="expanded to zero source files"):
        _expand_scope(tmp_path, ["empty"])


def test_expand_scope_nonexistent_entry_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        _expand_scope(tmp_path, ["nope/"])


def test_expand_scope_path_escape_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="escapes repo_root"):
        _expand_scope(tmp_path, ["../outside"])


def test_expand_scope_mixed_file_and_directory(tmp_path: pathlib.Path) -> None:
    (tmp_path / "entry.py").write_text("")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "a.ts").write_text("")

    assert _expand_scope(tmp_path, ["entry.py", "lib"]) == [
        "entry.py",
        str(pathlib.Path("lib/a.ts")),
    ]


def test_expand_scope_directory_output_is_sorted(tmp_path: pathlib.Path) -> None:
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "z.ts").write_text("")
    (tmp_path / "lib" / "a.ts").write_text("")
    (tmp_path / "lib" / "m.ts").write_text("")

    result = _expand_scope(tmp_path, ["lib"])
    assert result == sorted(result)


def test_expand_scope_filters_untracked_files_in_git_repo(
    tmp_path: pathlib.Path,
) -> None:
    """In a git repo, directory expansion skips files git doesn't track —
    this is how a TypeScript repo's `.d.ts` / `.js` build artifacts stop
    getting swept in alongside the `.ts` sources after `npm run build`.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "source.ts").write_text("")  # tracked
    (tmp_path / "lib" / "generated.d.ts").write_text("")  # tracked for now
    (tmp_path / "lib" / "artifact.js").write_text("")  # NOT tracked

    subprocess.run(
        ["git", "add", "lib/source.ts", "lib/generated.d.ts"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    result = _expand_scope(tmp_path, ["lib"])
    assert str(pathlib.Path("lib/source.ts")) in result
    assert str(pathlib.Path("lib/generated.d.ts")) in result
    assert str(pathlib.Path("lib/artifact.js")) not in result
