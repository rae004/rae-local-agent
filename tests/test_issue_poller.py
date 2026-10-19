"""Tests for the Phase C issue poller.

Covers:

- `main()` exit codes (valid / missing / invalid config).
- `tick()` shape: lists per repo, handles each issue, keeps going when
  one repo's `gh` call fails.
- `_handle_issue` paths: accept (label-first, write pending, comment),
  reject on parse error (comment + agent-invalid label), reject on
  enqueue error.
- Signal handling: the handler flips the shared flag.
- Interruptible sleep: wakes up when the flag flips mid-sleep.

Each `main()` test monkeypatches `signal.signal` to a no-op so we
don't leave global SIGINT/SIGTERM handlers behind for the rest of the
suite.
"""

from __future__ import annotations

import logging
import signal
from pathlib import Path
from textwrap import dedent

import pytest

import config
import gh_api
import issue_poller
import job


@pytest.fixture(autouse=True)
def _reset_stop_flag():
    """Keep the module-level `_flag` from leaking state between tests."""
    issue_poller._flag.stopped = False
    yield
    issue_poller._flag.stopped = False


@pytest.fixture
def _jobs_dir(tmp_path, monkeypatch):
    """Point the `job` module at a tmp root so write_pending doesn't touch the real queue."""
    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path / "jobs")
    (tmp_path / "jobs" / "pending").mkdir(parents=True)
    return tmp_path / "jobs"


def _write_valid_config(tmp_path: Path, poll_interval: int = 60) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    cfg_path = tmp_path / "repos.yaml"
    cfg_path.write_text(
        f"poll_interval_seconds: {poll_interval}\nrepos:\n  owner/r: {repo}\n",
        encoding="utf-8",
    )
    return cfg_path


def _good_body(tmp_path: Path) -> str:
    """A representative issue body that parses + validates cleanly."""
    return dedent(
        """
        ---
        scope:
          - a.py
        title: "chore: do it"
        ---

        Do the thing.
        """
    ).lstrip()


# ---------- main() exit codes -------------------------------------------


def test_main_returns_zero_on_valid_config(tmp_path, monkeypatch, caplog):
    cfg_path = _write_valid_config(tmp_path)
    monkeypatch.setattr(issue_poller, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(issue_poller.signal, "signal", lambda *a, **k: None)

    def _mock_tick(_cfg):
        issue_poller._flag.stopped = True
        return 0

    monkeypatch.setattr(issue_poller, "tick", _mock_tick)
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    assert issue_poller.main() == 0
    assert any("poller up" in r.message for r in caplog.records)
    assert any("poller down" in r.message for r in caplog.records)


def test_main_returns_one_on_missing_config(tmp_path, monkeypatch, caplog):
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr(issue_poller, "DEFAULT_CONFIG_PATH", missing)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", missing)
    caplog.set_level(logging.ERROR, logger="rae-local-agent.issue_poller")

    assert issue_poller.main() == 1
    assert any("config file not found" in r.message for r in caplog.records)


def test_main_returns_one_on_invalid_config(tmp_path, monkeypatch, caplog):
    cfg_path = tmp_path / "repos.yaml"
    cfg_path.write_text("poll_interval_seconds: 60\n", encoding="utf-8")
    monkeypatch.setattr(issue_poller, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    caplog.set_level(logging.ERROR, logger="rae-local-agent.issue_poller")

    assert issue_poller.main() == 1
    assert any("`repos` field is required" in r.message for r in caplog.records)


def test_main_installs_signal_handlers_only_after_config_is_valid(tmp_path, monkeypatch):
    installed: list = []

    def _record(signum, handler):
        installed.append((signum, handler))

    monkeypatch.setattr(issue_poller, "DEFAULT_CONFIG_PATH", tmp_path / "nope.yaml")
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", tmp_path / "nope.yaml")
    monkeypatch.setattr(issue_poller.signal, "signal", _record)

    assert issue_poller.main() == 1
    assert installed == []


# ---------- tick() shape ------------------------------------------------


def _cfg_with_repos(*slugs: str) -> config.Config:
    return config.Config(
        poll_interval_seconds=60,
        repos={slug: Path(f"/tmp/{slug.replace('/', '_')}") for slug in slugs},
    )


def test_tick_calls_handle_issue_for_each(monkeypatch, caplog):
    """One issue in; `_handle_issue` fired exactly once with the right args."""
    issues = [
        gh_api.Issue(number=1, title="first", url="u1", body="b1"),
        gh_api.Issue(number=2, title="second", url="u2", body="b2"),
    ]
    monkeypatch.setattr(gh_api, "list_pollable_issues", lambda slug: issues)
    seen: list = []
    monkeypatch.setattr(
        issue_poller,
        "_handle_issue",
        lambda slug, issue, repo_root: seen.append((slug, issue.number, repo_root)),
    )

    n = issue_poller.tick(_cfg_with_repos("owner/repo"))

    assert n == 2
    assert seen == [
        ("owner/repo", 1, "/tmp/owner_repo"),
        ("owner/repo", 2, "/tmp/owner_repo"),
    ]


def test_tick_iterates_all_repos(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        gh_api,
        "list_pollable_issues",
        lambda slug: (seen.append(slug), [])[1],
    )
    monkeypatch.setattr(issue_poller, "_handle_issue", lambda *a: None)

    issue_poller.tick(_cfg_with_repos("owner/a", "owner/b", "owner/c"))

    assert seen == ["owner/a", "owner/b", "owner/c"]


def test_tick_skips_repo_on_gh_error_and_continues(monkeypatch, caplog):
    """Repo B shouldn't go dark just because repo A's token lapsed."""

    def _list(slug):
        if slug == "owner/a":
            raise gh_api.GhCliError("auth required")
        return [gh_api.Issue(number=3, title="third", url="u3", body="body")]

    monkeypatch.setattr(gh_api, "list_pollable_issues", _list)
    monkeypatch.setattr(issue_poller, "_handle_issue", lambda *a: None)
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    issue_poller.tick(_cfg_with_repos("owner/a", "owner/b"))

    msgs = [r.message for r in caplog.records]
    assert any("skipping owner/a" in m for m in msgs)


# ---------- _handle_issue: accept path ---------------------------------


def test_handle_issue_accept_writes_label_before_comment(tmp_path, monkeypatch, _jobs_dir, caplog):
    """Label must land before the comment. (`_make_job_record` runs first but
    is local-only — only the two gh writes have observable ordering.)"""
    body = _good_body(tmp_path)
    issue = gh_api.Issue(number=7, title="t", url="u", body=body)
    calls: list[tuple] = []

    def _rec_label(slug, number, label):
        calls.append(("label", slug, number, label))

    def _rec_comment(slug, number, body):
        calls.append(("comment", slug, number, body))

    monkeypatch.setattr(gh_api, "add_label", _rec_label)
    monkeypatch.setattr(gh_api, "comment_on_issue", _rec_comment)
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    issue_poller._handle_issue("owner/repo", issue, str(tmp_path))

    kinds = [c[0] for c in calls]
    assert kinds == ["label", "comment"]
    _, _, _, applied_label = calls[0]
    assert applied_label == issue_poller.LABEL_QUEUED

    # A pending job landed on disk — one and only one.
    pending = list((_jobs_dir / "pending").iterdir())
    assert len(pending) == 1
    import json as _json

    rec = _json.loads(pending[0].read_text())
    assert rec["spec"] == "Do the thing."
    assert rec["scope"] == ["a.py"]
    assert rec["title"] == "chore: do it"
    assert str(tmp_path) in rec["repo_root"]

    # The comment body names the job id and carries the HTML tag C4 reads.
    _, _, _, comment_body = calls[1]
    assert rec["id"] in comment_body
    assert "rae-local-agent:job_id=" in comment_body


def test_handle_issue_accept_label_failure_does_not_enqueue(
    tmp_path, monkeypatch, _jobs_dir, caplog
):
    """Label is the idempotency gate — if it fails, nothing else happens."""
    body = _good_body(tmp_path)
    issue = gh_api.Issue(number=7, title="t", url="u", body=body)
    monkeypatch.setattr(
        gh_api,
        "add_label",
        lambda *a: (_ for _ in ()).throw(gh_api.GhCliError("boom")),
    )
    comments: list = []
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda *a: comments.append(a))
    caplog.set_level(logging.WARNING, logger="rae-local-agent.issue_poller")

    issue_poller._handle_issue("owner/repo", issue, str(tmp_path))

    # Nothing written to pending.
    assert list((_jobs_dir / "pending").iterdir()) == []
    # No comment either (the "Enqueued as X" comment would be a lie).
    assert comments == []
    assert any("adopt label on owner/repo#7 failed" in r.message for r in caplog.records)


def test_handle_issue_accept_comment_failure_logs_but_leaves_job_queued(
    tmp_path, monkeypatch, _jobs_dir, caplog
):
    """Label + job already on disk; comment failing is a recoverable loss, not a crash."""
    body = _good_body(tmp_path)
    issue = gh_api.Issue(number=7, title="t", url="u", body=body)
    monkeypatch.setattr(gh_api, "add_label", lambda *a: None)
    monkeypatch.setattr(
        gh_api,
        "comment_on_issue",
        lambda *a: (_ for _ in ()).throw(gh_api.GhCliError("rate limited")),
    )
    caplog.set_level(logging.WARNING, logger="rae-local-agent.issue_poller")

    issue_poller._handle_issue("owner/repo", issue, str(tmp_path))

    # Job is on disk — worker will still produce a PR.
    assert len(list((_jobs_dir / "pending").iterdir())) == 1
    # Warning names the job id so the operator can grep for it.
    msgs = [r.message for r in caplog.records]
    assert any("enqueue comment on owner/repo#7" in m and "job" in m for m in msgs)


# ---------- _handle_issue: reject paths --------------------------------


def test_handle_issue_rejects_on_parse_error(tmp_path, monkeypatch, _jobs_dir, caplog):
    issue = gh_api.Issue(number=9, title="t", url="u", body="just prose, no frontmatter")
    posted: list = []
    labeled: list = []
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda s, n, b: posted.append((s, n, b)))
    monkeypatch.setattr(gh_api, "add_label", lambda s, n, label: labeled.append((s, n, label)))
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    issue_poller._handle_issue("owner/repo", issue, str(tmp_path))

    # Nothing enqueued.
    assert list((_jobs_dir / "pending").iterdir()) == []
    # Comment names the parse error; label is `agent-invalid`.
    assert len(posted) == 1
    assert "YAML frontmatter" in posted[0][2]
    assert labeled == [("owner/repo", 9, issue_poller.LABEL_INVALID)]


def test_handle_issue_rejects_on_enqueue_error(tmp_path, monkeypatch, _jobs_dir):
    """Parsed OK but repo_root isn't a directory — surface it to the issue.

    _make_job_record runs BEFORE the label write (see docstring), so the
    reject path only applies `agent-invalid`, not both labels.
    """
    body = _good_body(tmp_path)
    issue = gh_api.Issue(number=11, title="t", url="u", body=body)
    posted: list = []
    labeled: list = []
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda s, n, b: posted.append((s, n, b)))
    monkeypatch.setattr(gh_api, "add_label", lambda s, n, label: labeled.append((s, n, label)))

    # repo_root pointing at a file, not a directory, trips _make_job_record.
    bogus_root = tmp_path / "a-file"
    bogus_root.write_text("i am a file")

    issue_poller._handle_issue("owner/repo", issue, str(bogus_root))

    assert list((_jobs_dir / "pending").iterdir()) == []
    assert len(posted) == 1
    assert "enqueue failed" in posted[0][2]
    assert labeled == [("owner/repo", 11, issue_poller.LABEL_INVALID)]


def test_reject_tolerates_comment_failure(tmp_path, monkeypatch, _jobs_dir, caplog):
    """Comment write failing on reject doesn't crash — try the label anyway."""
    issue = gh_api.Issue(number=12, title="t", url="u", body="no fm")
    monkeypatch.setattr(
        gh_api,
        "comment_on_issue",
        lambda *a: (_ for _ in ()).throw(gh_api.GhCliError("flake")),
    )
    labeled: list = []
    monkeypatch.setattr(gh_api, "add_label", lambda s, n, label: labeled.append((s, n, label)))
    caplog.set_level(logging.WARNING, logger="rae-local-agent.issue_poller")

    issue_poller._handle_issue("owner/repo", issue, str(tmp_path))

    assert labeled == [("owner/repo", 12, issue_poller.LABEL_INVALID)]
    assert any("reject comment on owner/repo#12 failed" in r.message for r in caplog.records)


# ---------- signal + sleep ---------------------------------------------


def test_on_signal_sets_stop_flag():
    assert issue_poller._flag.stopped is False
    issue_poller._on_signal(signal.SIGTERM, None)
    assert issue_poller._flag.stopped is True


def test_interruptible_sleep_wakes_promptly_when_flag_is_already_set(monkeypatch):
    calls = []
    monkeypatch.setattr(issue_poller.time, "sleep", lambda s: calls.append(s))
    issue_poller._flag.stopped = True

    issue_poller._interruptible_sleep(300)

    assert calls == []


def test_interruptible_sleep_sleeps_in_chunks(monkeypatch):
    calls = []
    monkeypatch.setattr(issue_poller.time, "sleep", lambda s: calls.append(s))

    issue_poller._interruptible_sleep(5)

    assert calls == [1.0, 1.0, 1.0, 1.0, 1.0]


def test_interruptible_sleep_breaks_when_flag_flips_mid_sleep(monkeypatch):
    calls = []

    def _sleep(s):
        calls.append(s)
        if len(calls) == 2:
            issue_poller._flag.stopped = True

    monkeypatch.setattr(issue_poller.time, "sleep", _sleep)

    issue_poller._interruptible_sleep(10)

    assert calls == [1.0, 1.0]
