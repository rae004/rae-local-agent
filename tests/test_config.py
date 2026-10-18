"""Tests for the Phase C config loader (C0).

Every validation branch gets explicit coverage — the config file is
the one surface humans edit for Phase C, so a vague error is worse
than most other kinds of bugs.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

import config


def _write(path: Path, body: str) -> Path:
    path.write_text(dedent(body).lstrip("\n"), encoding="utf-8")
    return path


# ---------- happy path --------------------------------------------------


def test_load_config_happy_path(tmp_path):
    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()
    cfg_path = _write(
        tmp_path / "repos.yaml",
        f"""
        poll_interval_seconds: 120
        repos:
          owner/repo-a: {repo_a}
          owner/repo-b: {repo_b}
        """,
    )

    cfg = config.load_config(cfg_path)

    assert cfg.poll_interval_seconds == 120
    assert set(cfg.repos) == {"owner/repo-a", "owner/repo-b"}
    assert cfg.repos["owner/repo-a"] == repo_a
    assert cfg.repos["owner/repo-b"] == repo_b


def test_load_config_defaults_poll_interval(tmp_path):
    """poll_interval_seconds omitted → DEFAULT_POLL_INTERVAL_SECONDS."""
    repo = tmp_path / "r"
    repo.mkdir()
    cfg_path = _write(
        tmp_path / "repos.yaml",
        f"""
        repos:
          owner/r: {repo}
        """,
    )

    cfg = config.load_config(cfg_path)

    assert cfg.poll_interval_seconds == config.DEFAULT_POLL_INTERVAL_SECONDS


# ---------- file-level errors -------------------------------------------


def test_load_config_missing_file(tmp_path):
    with pytest.raises(config.InvalidConfig, match="config file not found"):
        config.load_config(tmp_path / "nope.yaml")


def test_load_config_bad_yaml(tmp_path):
    cfg_path = _write(tmp_path / "repos.yaml", "not: valid: yaml: here")
    with pytest.raises(config.InvalidConfig, match="not valid YAML"):
        config.load_config(cfg_path)


def test_load_config_empty_file(tmp_path):
    cfg_path = _write(tmp_path / "repos.yaml", "")
    with pytest.raises(config.InvalidConfig, match="is empty"):
        config.load_config(cfg_path)


def test_load_config_top_level_not_mapping(tmp_path):
    cfg_path = _write(tmp_path / "repos.yaml", "- just\n- a\n- list\n")
    with pytest.raises(config.InvalidConfig, match="mapping at the top level"):
        config.load_config(cfg_path)


# ---------- poll_interval_seconds ---------------------------------------


_POS_INT_MSG = "poll_interval_seconds must be a positive integer"


def test_load_config_poll_interval_not_int(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    cfg_path = _write(
        tmp_path / "repos.yaml",
        f"""
        poll_interval_seconds: fast
        repos:
          owner/r: {repo}
        """,
    )
    with pytest.raises(config.InvalidConfig, match=_POS_INT_MSG):
        config.load_config(cfg_path)


def test_load_config_poll_interval_zero(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    cfg_path = _write(
        tmp_path / "repos.yaml",
        f"""
        poll_interval_seconds: 0
        repos:
          owner/r: {repo}
        """,
    )
    with pytest.raises(config.InvalidConfig, match="poll_interval_seconds must be positive"):
        config.load_config(cfg_path)


def test_load_config_poll_interval_negative(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    cfg_path = _write(
        tmp_path / "repos.yaml",
        f"""
        poll_interval_seconds: -30
        repos:
          owner/r: {repo}
        """,
    )
    with pytest.raises(config.InvalidConfig, match="poll_interval_seconds must be positive"):
        config.load_config(cfg_path)


def test_load_config_poll_interval_bool_rejected(tmp_path):
    """Python's `bool` is-a `int`; the loader must still reject it."""
    repo = tmp_path / "r"
    repo.mkdir()
    cfg_path = _write(
        tmp_path / "repos.yaml",
        f"""
        poll_interval_seconds: true
        repos:
          owner/r: {repo}
        """,
    )
    with pytest.raises(config.InvalidConfig, match=_POS_INT_MSG):
        config.load_config(cfg_path)


# ---------- repos -------------------------------------------------------


def test_load_config_repos_missing(tmp_path):
    cfg_path = _write(tmp_path / "repos.yaml", "poll_interval_seconds: 60\n")
    with pytest.raises(config.InvalidConfig, match="`repos` field is required"):
        config.load_config(cfg_path)


def test_load_config_repos_wrong_type(tmp_path):
    cfg_path = _write(
        tmp_path / "repos.yaml",
        """
        repos:
          - just
          - a
          - list
        """,
    )
    with pytest.raises(config.InvalidConfig, match="`repos` must be a mapping"):
        config.load_config(cfg_path)


def test_load_config_repos_empty(tmp_path):
    cfg_path = _write(tmp_path / "repos.yaml", "repos: {}\n")
    with pytest.raises(config.InvalidConfig, match="non-empty mapping"):
        config.load_config(cfg_path)


@pytest.mark.parametrize(
    "slug",
    [
        "noslash",
        "two/slashes/here",
        "/leading-slash",
        "trailing/",
        "has spaces/x",
        "has.dots.ok/but/this-breaks",
    ],
)
def test_load_config_repos_malformed_slug(tmp_path, slug):
    repo = tmp_path / "r"
    repo.mkdir()
    cfg_path = _write(
        tmp_path / "repos.yaml",
        f"""
        repos:
          "{slug}": {repo}
        """,
    )
    with pytest.raises(config.InvalidConfig, match="`repos` key must look like 'owner/name'"):
        config.load_config(cfg_path)


def test_load_config_repos_path_not_string(tmp_path):
    cfg_path = _write(
        tmp_path / "repos.yaml",
        """
        repos:
          owner/r:
            - path
            - is
            - a
            - list
        """,
    )
    with pytest.raises(
        config.InvalidConfig,
        match="`repos.owner/r` must be an absolute path string",
    ):
        config.load_config(cfg_path)


def test_load_config_repos_path_relative(tmp_path):
    cfg_path = _write(
        tmp_path / "repos.yaml",
        """
        repos:
          owner/r: ./relative/path
        """,
    )
    with pytest.raises(config.InvalidConfig, match="must be an absolute path"):
        config.load_config(cfg_path)


def test_load_config_repos_path_missing(tmp_path):
    cfg_path = _write(
        tmp_path / "repos.yaml",
        """
        repos:
          owner/r: /this/path/does/not/exist/anywhere
        """,
    )
    with pytest.raises(config.InvalidConfig, match="path does not exist"):
        config.load_config(cfg_path)


def test_load_config_repos_path_is_file_not_dir(tmp_path):
    not_a_dir = tmp_path / "a_file"
    not_a_dir.write_text("I am a file, not a directory")
    cfg_path = _write(
        tmp_path / "repos.yaml",
        f"""
        repos:
          owner/r: {not_a_dir}
        """,
    )
    with pytest.raises(config.InvalidConfig, match="path is not a directory"):
        config.load_config(cfg_path)
