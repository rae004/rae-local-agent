"""Tests for the thin `gh` CLI wrapper (C1 + C3).

The whole point of `gh_api` is to isolate subprocess calls behind a
small typed surface. Tests here monkeypatch `subprocess.run` so the
unit under test is exactly the shape translation + error surfacing,
not the external CLI.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import pytest

import gh_api


@dataclass
class _FakeCompleted:
    """Minimal stand-in for `subprocess.CompletedProcess`."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def _ok_json(issues: list[dict]) -> _FakeCompleted:
    return _FakeCompleted(returncode=0, stdout=json.dumps(issues))


# ---------- list_pollable_issues: happy path ----------------------------


def test_list_pollable_issues_parses_rows(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _ok_json(
            [
                {
                    "number": 7,
                    "title": "do a thing",
                    "url": "https://github.com/x/y/issues/7",
                    "body": "the body",
                },
                {
                    "number": 9,
                    "title": "do another",
                    "url": "https://github.com/x/y/issues/9",
                    "body": "another body",
                },
            ]
        ),
    )

    out = gh_api.list_pollable_issues("x/y")

    assert out == [
        gh_api.Issue(
            number=7, title="do a thing", url="https://github.com/x/y/issues/7", body="the body"
        ),
        gh_api.Issue(
            number=9, title="do another", url="https://github.com/x/y/issues/9", body="another body"
        ),
    ]


def test_list_pollable_issues_empty_response(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _ok_json([]))
    assert gh_api.list_pollable_issues("x/y") == []


def test_list_pollable_issues_empty_stdout_is_empty_list(monkeypatch):
    """`gh` sometimes prints nothing for an empty match; treat as `[]`."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(0, stdout=""))
    assert gh_api.list_pollable_issues("x/y") == []


def test_list_pollable_issues_missing_body_defaults_to_empty_string(monkeypatch):
    """`gh` omits `body` on issues with blank body text; the dataclass should still land."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _ok_json(
            [{"number": 1, "title": "t", "url": "u"}],  # no `body` key
        ),
    )
    out = gh_api.list_pollable_issues("x/y")
    assert out == [gh_api.Issue(number=1, title="t", url="u", body="")]


def test_list_pollable_issues_builds_expected_command(monkeypatch):
    """The whole search string lives in one place; this pins the call shape."""
    seen = {}

    def _capture(cmd, *a, **k):
        seen["cmd"] = cmd
        seen["timeout"] = k.get("timeout")
        return _ok_json([])

    monkeypatch.setattr(subprocess, "run", _capture)
    gh_api.list_pollable_issues("owner/repo")

    assert seen["cmd"] == [
        "gh",
        "issue",
        "list",
        "--repo",
        "owner/repo",
        "--search",
        gh_api.POLL_SEARCH,
        "--json",
        "number,title,url,body",
    ]
    assert seen["timeout"] is not None  # we always pass one — don't hang forever


# ---------- list_pollable_issues: failures ------------------------------


def test_list_pollable_issues_non_zero_rc_raises(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stderr="auth required"),
    )
    with pytest.raises(gh_api.GhCliError, match="auth required"):
        gh_api.list_pollable_issues("x/y")


def test_list_pollable_issues_non_zero_rc_mentions_repo(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=4, stderr="nope"),
    )
    with pytest.raises(gh_api.GhCliError, match="x/y"):
        gh_api.list_pollable_issues("x/y")


def test_list_pollable_issues_non_zero_rc_blank_stderr_shows_placeholder(monkeypatch):
    """Blank stderr still produces a useful message."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=2, stderr=""),
    )
    with pytest.raises(gh_api.GhCliError, match="no stderr"):
        gh_api.list_pollable_issues("x/y")


def test_list_pollable_issues_bad_json_raises(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="not json at all"),
    )
    with pytest.raises(gh_api.GhCliError, match="invalid JSON"):
        gh_api.list_pollable_issues("x/y")


def test_list_pollable_issues_non_list_json_raises(monkeypatch):
    """`gh` is contract-bound to return a list; a dict is a surprise worth erroring on."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout='{"not": "a list"}'),
    )
    with pytest.raises(gh_api.GhCliError, match="expected list"):
        gh_api.list_pollable_issues("x/y")


def test_list_pollable_issues_gh_missing_raises(monkeypatch):
    def _no_gh(*a, **k):
        raise FileNotFoundError("No such file or directory: 'gh'")

    monkeypatch.setattr(subprocess, "run", _no_gh)
    with pytest.raises(gh_api.GhCliError, match="`gh` CLI not found"):
        gh_api.list_pollable_issues("x/y")


def test_list_pollable_issues_timeout_raises(monkeypatch):
    def _slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", _slow)
    with pytest.raises(gh_api.GhCliError, match="timed out"):
        gh_api.list_pollable_issues("x/y")


# ---------- comment_on_issue -------------------------------------------


def test_comment_on_issue_happy_path(monkeypatch):
    seen = {}

    def _capture(cmd, *a, **k):
        seen["cmd"] = cmd
        seen["input"] = k.get("input")
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(subprocess, "run", _capture)
    gh_api.comment_on_issue("x/y", 42, "hello\nmulti\nline")

    assert seen["cmd"] == [
        "gh",
        "issue",
        "comment",
        "42",
        "--repo",
        "x/y",
        "--body-file",
        "-",
    ]
    # Body goes over stdin so shell quoting / punctuation can't bite.
    assert seen["input"] == "hello\nmulti\nline"


def test_comment_on_issue_non_zero_rc_raises(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stderr="forbidden"),
    )
    with pytest.raises(gh_api.GhCliError, match="comment on x/y#5.*forbidden"):
        gh_api.comment_on_issue("x/y", 5, "body")


def test_comment_on_issue_timeout_raises(monkeypatch):
    def _slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", _slow)
    with pytest.raises(gh_api.GhCliError, match="timed out"):
        gh_api.comment_on_issue("x/y", 5, "body")


def test_comment_on_issue_gh_missing_raises(monkeypatch):
    def _no_gh(*a, **k):
        raise FileNotFoundError("No such file or directory: 'gh'")

    monkeypatch.setattr(subprocess, "run", _no_gh)
    with pytest.raises(gh_api.GhCliError, match="`gh` CLI not found"):
        gh_api.comment_on_issue("x/y", 5, "body")


# ---------- add_label --------------------------------------------------


def test_add_label_happy_path(monkeypatch):
    seen = {}

    def _capture(cmd, *a, **k):
        seen["cmd"] = cmd
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(subprocess, "run", _capture)
    gh_api.add_label("x/y", 42, "agent-queued")

    assert seen["cmd"] == [
        "gh",
        "issue",
        "edit",
        "42",
        "--repo",
        "x/y",
        "--add-label",
        "agent-queued",
    ]


def test_add_label_non_zero_rc_raises(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stderr="label not found"),
    )
    with pytest.raises(gh_api.GhCliError, match="add-label.*label not found"):
        gh_api.add_label("x/y", 5, "nope")


def test_add_label_timeout_raises(monkeypatch):
    def _slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", _slow)
    with pytest.raises(gh_api.GhCliError, match="timed out"):
        gh_api.add_label("x/y", 5, "agent-queued")
