"""Tests for the Phase C issue poller (C1).

Covers:

- `main()` exit codes (valid / missing / invalid config).
- `tick()` shape: lists per repo, logs each issue, keeps going when one
  repo's `gh` call fails.
- Signal handling: the handler flips the shared flag.
- Interruptible sleep: wakes up when the flag flips mid-sleep.

Each `main()` test monkeypatches `signal.signal` to a no-op so we don't
leave global SIGINT/SIGTERM handlers behind for the rest of the suite.
"""

from __future__ import annotations

import logging
import signal
from pathlib import Path

import pytest

import config
import gh_api
import issue_poller


@pytest.fixture(autouse=True)
def _reset_stop_flag():
    """Keep the module-level `_flag` from leaking state between tests."""
    issue_poller._flag.stopped = False
    yield
    issue_poller._flag.stopped = False


def _write_valid_config(tmp_path: Path, poll_interval: int = 60) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    cfg_path = tmp_path / "repos.yaml"
    cfg_path.write_text(
        f"poll_interval_seconds: {poll_interval}\nrepos:\n  owner/r: {repo}\n",
        encoding="utf-8",
    )
    return cfg_path


# ---------- main() exit codes -------------------------------------------


def test_main_returns_zero_on_valid_config(tmp_path, monkeypatch, caplog):
    cfg_path = _write_valid_config(tmp_path)
    monkeypatch.setattr(issue_poller, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(issue_poller.signal, "signal", lambda *a, **k: None)

    # Make the first tick flip the stop flag so main() exits cleanly after
    # one pass instead of looping forever.
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
    """A config that parses as YAML but fails validation surfaces the field name."""
    cfg_path = tmp_path / "repos.yaml"
    cfg_path.write_text("poll_interval_seconds: 60\n", encoding="utf-8")  # no `repos`
    monkeypatch.setattr(issue_poller, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    caplog.set_level(logging.ERROR, logger="rae-local-agent.issue_poller")

    assert issue_poller.main() == 1
    assert any("`repos` field is required" in r.message for r in caplog.records)


def test_main_installs_signal_handlers_only_after_config_is_valid(tmp_path, monkeypatch):
    """A bad config exits before touching signal handlers (so systemd can log + retry
    without the installed handler masking a real interrupt during the next attempt)."""
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


def test_tick_logs_one_line_per_issue(monkeypatch, caplog):
    monkeypatch.setattr(
        gh_api,
        "list_pollable_issues",
        lambda slug: [
            gh_api.Issue(number=1, title="first", url="u1"),
            gh_api.Issue(number=2, title="second", url="u2"),
        ],
    )
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    n = issue_poller.tick(_cfg_with_repos("owner/repo"))

    assert n == 2
    msgs = [r.message for r in caplog.records]
    assert any("owner/repo #1: first" in m for m in msgs)
    assert any("owner/repo #2: second" in m for m in msgs)


def test_tick_iterates_all_repos(monkeypatch, caplog):
    seen: list[str] = []

    def _list(slug):
        seen.append(slug)
        return []

    monkeypatch.setattr(gh_api, "list_pollable_issues", _list)
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    issue_poller.tick(_cfg_with_repos("owner/a", "owner/b", "owner/c"))

    assert seen == ["owner/a", "owner/b", "owner/c"]


def test_tick_counts_zero_when_no_issues(monkeypatch):
    monkeypatch.setattr(gh_api, "list_pollable_issues", lambda slug: [])
    assert issue_poller.tick(_cfg_with_repos("owner/a", "owner/b")) == 0


def test_tick_skips_repo_on_gh_error_and_continues(monkeypatch, caplog):
    """Repo B shouldn't go dark just because repo A's token lapsed."""

    def _list(slug):
        if slug == "owner/a":
            raise gh_api.GhCliError("auth required")
        return [gh_api.Issue(number=3, title="third", url="u3")]

    monkeypatch.setattr(gh_api, "list_pollable_issues", _list)
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    n = issue_poller.tick(_cfg_with_repos("owner/a", "owner/b"))

    assert n == 1
    msgs = [r.message for r in caplog.records]
    assert any("skipping owner/a" in m for m in msgs)
    assert any("owner/b #3: third" in m for m in msgs)


# ---------- signal + sleep ---------------------------------------------


def test_on_signal_sets_stop_flag():
    assert issue_poller._flag.stopped is False
    issue_poller._on_signal(signal.SIGTERM, None)
    assert issue_poller._flag.stopped is True


def test_interruptible_sleep_wakes_promptly_when_flag_is_already_set(monkeypatch):
    """Nothing should sleep at all once the stop flag is up."""
    calls = []
    monkeypatch.setattr(issue_poller.time, "sleep", lambda s: calls.append(s))
    issue_poller._flag.stopped = True

    issue_poller._interruptible_sleep(300)

    assert calls == []


def test_interruptible_sleep_sleeps_in_chunks(monkeypatch):
    """Full-interval sleep breaks into N 1-second chunks so a signal is seen quickly."""
    calls = []
    monkeypatch.setattr(issue_poller.time, "sleep", lambda s: calls.append(s))

    issue_poller._interruptible_sleep(5)

    assert calls == [1.0, 1.0, 1.0, 1.0, 1.0]


def test_interruptible_sleep_breaks_when_flag_flips_mid_sleep(monkeypatch):
    """Flipping the flag during one chunk stops the next chunk from running."""
    calls = []

    def _sleep(s):
        calls.append(s)
        if len(calls) == 2:
            issue_poller._flag.stopped = True

    monkeypatch.setattr(issue_poller.time, "sleep", _sleep)

    issue_poller._interruptible_sleep(10)

    # Flag flipped on chunk 2; chunk 3's check short-circuits before sleep.
    assert calls == [1.0, 1.0]
