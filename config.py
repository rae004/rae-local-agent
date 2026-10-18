"""Phase C config loader for the issue poller.

Reads `~/.config/rae-local-agent/repos.yaml` into a frozen `Config`
dataclass and validates every field with a clear error on the first
problem found. Missing file, YAML parse errors, missing `repos`, bad
repo names, missing local paths, non-directory paths — each surfaces
as `InvalidConfig(<human-readable message that names the offending
field>)`.

Separate from `issue_poller.py` so future milestones (C1+) can import
`Config` without pulling the main loop in.

See `PHASE_C_DESIGN.md` § "Configuration" for the file format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "rae-local-agent" / "repos.yaml"
DEFAULT_POLL_INTERVAL_SECONDS = 300

# GitHub repo slug: `owner/name`. Both parts allow alnum, dot, underscore,
# hyphen; exactly one slash separates them. Not the full set of what
# GitHub accepts (that's deliberately looser than useful), but covers
# every realistic value and rejects anything with path separators or
# whitespace.
_REPO_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")


class InvalidConfig(ValueError):
    """The config file is missing, unparseable, or semantically invalid.

    Message names the field (or file) the loader rejected. Caller surfaces
    it verbatim; no need to wrap or re-format.
    """


@dataclass(frozen=True)
class Config:
    """Loaded + validated poller config.

    - `poll_interval_seconds`: seconds between polling ticks. Positive int.
    - `repos`: mapping from `owner/name` to the absolute local path on
      this machine. Every path is an existing directory at load time.
    """

    poll_interval_seconds: int
    repos: dict[str, Path]


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load and validate the config at `path`.

    Default path is `~/.config/rae-local-agent/repos.yaml`. Raises
    `InvalidConfig` on any failure with a message that names the
    offending field or file.
    """
    if not path.is_file():
        raise InvalidConfig(f"config file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise InvalidConfig(f"config at {path} is not valid YAML: {e}") from e

    if raw is None:
        raise InvalidConfig(f"config at {path} is empty")
    if not isinstance(raw, dict):
        raise InvalidConfig(
            f"config at {path} must be a mapping at the top level, got {type(raw).__name__}"
        )

    poll_interval = _validate_poll_interval(raw.get("poll_interval_seconds"))
    repos = _validate_repos(raw.get("repos"))
    return Config(poll_interval_seconds=poll_interval, repos=repos)


def _validate_poll_interval(value: object) -> int:
    if value is None:
        return DEFAULT_POLL_INTERVAL_SECONDS
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidConfig(
            f"poll_interval_seconds must be a positive integer, got {value!r} "
            f"({type(value).__name__})"
        )
    if value <= 0:
        raise InvalidConfig(f"poll_interval_seconds must be positive, got {value}")
    return value


def _validate_repos(value: object) -> dict[str, Path]:
    if value is None:
        raise InvalidConfig("`repos` field is required and must be a non-empty mapping")
    if not isinstance(value, dict):
        raise InvalidConfig(
            f"`repos` must be a mapping of 'owner/name' → absolute path, got {type(value).__name__}"
        )
    if not value:
        raise InvalidConfig("`repos` must be a non-empty mapping (nothing to poll)")

    out: dict[str, Path] = {}
    for slug, raw_path in value.items():
        if not isinstance(slug, str) or not _REPO_SLUG_PATTERN.match(slug):
            raise InvalidConfig(f"`repos` key must look like 'owner/name', got {slug!r}")
        if not isinstance(raw_path, str):
            raise InvalidConfig(
                f"`repos.{slug}` must be an absolute path string, got {raw_path!r} "
                f"({type(raw_path).__name__})"
            )
        path = Path(raw_path)
        if not path.is_absolute():
            raise InvalidConfig(f"`repos.{slug}` must be an absolute path, got {raw_path!r}")
        if not path.exists():
            raise InvalidConfig(f"`repos.{slug}` path does not exist: {raw_path}")
        if not path.is_dir():
            raise InvalidConfig(f"`repos.{slug}` path is not a directory: {raw_path}")
        out[slug] = path
    return out
