"""Phase C config loader for the issue poller.

Reads `~/.config/rae-local-agent/repos.yaml` into a frozen `Config`
dataclass and validates every field with a clear error on the first
problem found. Missing file, YAML parse errors, missing `repos`, bad
repo names, missing local paths, non-directory paths, malformed
gate_cmds — each surfaces as `InvalidConfig(<message that names the
offending field>)`.

Separate from `issue_poller.py` so future milestones can import
`Config` + `RepoConfig` without pulling the main loop in.

See `PHASE_C_DESIGN.md` § "Configuration" for the file format.

Each repo may be given either as a bare path string (back-compat
shape; no gate commands beyond the default) or as a mapping with
`path` and an optional `gate_cmds` list-of-lists. See `_validate_repo`
for the full set of accepted shapes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "rae-local-agent" / "repos.yaml"
DEFAULT_POLL_INTERVAL_SECONDS = 300

# GitHub repo slug: `owner/name`. Both parts allow alnum, dot, underscore,
# hyphen; exactly one slash separates them. Not the full set of what
# GitHub accepts (that's deliberately looser than useful), but covers
# every realistic value and rejects anything with path separators or
# whitespace.
_REPO_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")

# Fields a `repos.<slug>` mapping may carry.
_ALLOWED_REPO_KEYS: frozenset[str] = frozenset({"path", "gate_cmds"})


class InvalidConfig(ValueError):
    """The config file is missing, unparseable, or semantically invalid.

    Message names the field (or file) the loader rejected. Caller surfaces
    it verbatim; no need to wrap or re-format.
    """


@dataclass(frozen=True)
class RepoConfig:
    """One row of the `repos` map.

    - `path`: absolute, existing directory on this machine.
    - `gate_cmds`: list of commands run between `git apply` and
      `git commit` in the worker's worktree. First non-zero exit
      fails the gate and prevents push. `None` means "use the
      hardcoded default from `_make_job_record`" — currently
      `[["uv", "run", "pytest", "-q"]]`. An issue's frontmatter
      overrides this; see `issue_body.parse_issue_body`.
    """

    path: Path
    gate_cmds: tuple[tuple[str, ...], ...] | None = None


@dataclass(frozen=True)
class Config:
    """Loaded + validated poller config.

    - `poll_interval_seconds`: seconds between polling ticks. Positive int.
    - `repos`: mapping from `owner/name` to a `RepoConfig`.
    """

    poll_interval_seconds: int
    repos: dict[str, RepoConfig] = field(default_factory=dict)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load and validate the config at `path`. Raises `InvalidConfig`."""
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


def _validate_poll_interval(value: Any) -> int:
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


def _validate_repos(value: Any) -> dict[str, RepoConfig]:
    if value is None:
        raise InvalidConfig("`repos` field is required and must be a non-empty mapping")
    if not isinstance(value, dict):
        raise InvalidConfig(
            f"`repos` must be a mapping of 'owner/name' → path, got {type(value).__name__}"
        )
    if not value:
        raise InvalidConfig("`repos` must be a non-empty mapping (nothing to poll)")

    out: dict[str, RepoConfig] = {}
    for slug, raw in value.items():
        if not isinstance(slug, str) or not _REPO_SLUG_PATTERN.match(slug):
            raise InvalidConfig(f"`repos` key must look like 'owner/name', got {slug!r}")
        out[slug] = _validate_repo(slug, raw)
    return out


def _validate_repo(slug: str, raw: Any) -> RepoConfig:
    """Accepts either a bare path string (back-compat) or a mapping.

    Mapping shape: `{path: <abs string>, gate_cmds: [[cmd], [cmd], ...]}`.
    Unknown keys are rejected to catch typos — the field is small, a
    silent accept would hide bugs.
    """
    if isinstance(raw, str):
        return RepoConfig(path=_validate_repo_path(slug, raw))

    if not isinstance(raw, dict):
        raise InvalidConfig(
            f"`repos.{slug}` must be either an absolute path string or a mapping "
            f"with `path` + optional `gate_cmds`, got {type(raw).__name__}"
        )

    unknown = set(raw) - _ALLOWED_REPO_KEYS
    if unknown:
        raise InvalidConfig(
            f"`repos.{slug}` has unknown key(s): {sorted(unknown)}; "
            f"allowed: {sorted(_ALLOWED_REPO_KEYS)}"
        )

    raw_path = raw.get("path")
    if raw_path is None:
        raise InvalidConfig(f"`repos.{slug}.path` is required")
    if not isinstance(raw_path, str):
        raise InvalidConfig(
            f"`repos.{slug}.path` must be an absolute path string, got {raw_path!r} "
            f"({type(raw_path).__name__})"
        )
    path = _validate_repo_path(slug, raw_path)
    gate_cmds = _validate_gate_cmds(f"repos.{slug}.gate_cmds", raw.get("gate_cmds"))
    return RepoConfig(path=path, gate_cmds=gate_cmds)


def _validate_repo_path(slug: str, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise InvalidConfig(f"`repos.{slug}` must be an absolute path, got {raw_path!r}")
    if not path.exists():
        raise InvalidConfig(f"`repos.{slug}` path does not exist: {raw_path}")
    if not path.is_dir():
        raise InvalidConfig(f"`repos.{slug}` path is not a directory: {raw_path}")
    return path


def _validate_gate_cmds(field_name: str, value: Any) -> tuple[tuple[str, ...], ...] | None:
    """Validate a `gate_cmds` value anywhere it appears.

    Shared with `issue_body.parse_issue_body` logic — gate_cmds in
    frontmatter has the same shape as gate_cmds in repos.yaml.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise InvalidConfig(
            f"`{field_name}` must be a non-empty list of commands (list of lists of strings), "
            f"got {value!r}"
        )
    out: list[tuple[str, ...]] = []
    for i, cmd in enumerate(value):
        if not isinstance(cmd, list) or not cmd:
            raise InvalidConfig(
                f"`{field_name}[{i}]` must be a non-empty list of strings, got {cmd!r}"
            )
        for j, token in enumerate(cmd):
            if not isinstance(token, str):
                raise InvalidConfig(
                    f"`{field_name}[{i}][{j}]` must be a string, got {token!r} "
                    f"({type(token).__name__})"
                )
        out.append(tuple(cmd))
    return tuple(out)
