"""Smoke tests for the Phase C issue poller stub (C0).

C0 scope is a config-validation entry point, nothing more — these
tests just confirm `main()` returns the right exit codes and logs
something sensible. C1 adds the actual loop + its tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

import config
import issue_poller


def _write_valid_config(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    cfg_path = tmp_path / "repos.yaml"
    cfg_path.write_text(
        f"poll_interval_seconds: 60\nrepos:\n  owner/r: {repo}\n",
        encoding="utf-8",
    )
    return cfg_path


def test_main_returns_zero_on_valid_config(tmp_path, monkeypatch, caplog):
    cfg_path = _write_valid_config(tmp_path)
    monkeypatch.setattr(issue_poller, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    assert issue_poller.main() == 0
    # Reports what it loaded so the operator can confirm from journald.
    assert any("poller up" in r.message for r in caplog.records)


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
