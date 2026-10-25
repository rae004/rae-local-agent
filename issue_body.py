"""Phase C — issue body parser (C2).

Turns a GitHub issue body (YAML frontmatter + markdown prose) into a
dict suitable as `enqueue_job`'s kwargs. The frontmatter carries the
mechanical params (`scope`, `title`, `group_id`, …); the prose below
the closing fence becomes the `spec` the local model follows. See
`PHASE_C_DESIGN.md` § "Issue → job translation" for the full shape.

**C2 scope** — parse + validate only. No GitHub reads, no enqueue, no
writes. C3 wires `parse_issue_body` into the poller's tick with the
`agent-queued` label-write path.

Every failure raises `InvalidIssue(<message>)`. The message is
designed to be comment-back-ready: C3's error path posts it verbatim
to the issue so the human can see what's wrong without digging in
journald.
"""

from __future__ import annotations

import re
from textwrap import dedent
from typing import Any

import yaml

# Mirrors `server._GROUP_ID_PATTERN` exactly. Duplicated here (not
# imported) because importing `server` would pull in the MCP server,
# httpx, and the whole decorator surface for a leaf parser used in a
# different process. A unit test cross-checks the two patterns.
_GROUP_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# Frontmatter keys we accept. Mirrors `enqueue_job`'s kwargs minus the
# ones the poller derives: `spec` (the prose body itself) and
# `repo_root` (looked up from `repos.yaml` by the GitHub slug).
_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "scope",
        "base_branch",
        "test_cmd",
        "skip_tests",
        "title",
        "body",
        "group_id",
    }
)

# A `---` line (optionally with trailing whitespace) opens or closes
# frontmatter. We don't allow leading whitespace — that would make it
# an indented line, not a fence.
_FENCE = re.compile(r"^---\s*$", re.MULTILINE)


class InvalidIssue(ValueError):
    """Issue body is missing frontmatter, malformed, or semantically invalid.

    Message names the exact field or problem so the poller can post it
    back to the issue author verbatim.
    """


def parse_issue_body(body: str) -> dict[str, Any]:
    """Parse an issue body into `enqueue_job` kwargs.

    Expected input shape:

        ---
        scope:
          - path/one.py
          - path/two.py
        title: "chore: ..."
        group_id: null
        ---

        <markdown prose that becomes `spec`>

    Returns a dict with `scope`, `spec`, and whichever of
    `base_branch`, `test_cmd`, `skip_tests`, `title`, `body`,
    `group_id` the frontmatter carried. The returned dict is directly
    usable as `**kwargs` for `_make_job_record` once the caller
    attaches `repo_root`.

    Raises `InvalidIssue` on any failure (missing fence, bad YAML,
    unknown keys, bad scope, bad group_id, empty spec).
    """
    frontmatter_text, spec_text = _split_frontmatter(body)

    try:
        fm = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        raise InvalidIssue(f"frontmatter is not valid YAML: {e}") from e

    if fm is None:
        raise InvalidIssue("frontmatter is empty; need at least a `scope`")
    if not isinstance(fm, dict):
        raise InvalidIssue(f"frontmatter must be a mapping, got {type(fm).__name__}")

    unknown = set(fm) - _ALLOWED_KEYS
    if unknown:
        raise InvalidIssue(
            f"unknown frontmatter key(s): {sorted(unknown)}; allowed: {sorted(_ALLOWED_KEYS)}"
        )

    out: dict[str, Any] = {"scope": _validate_scope(fm.get("scope"))}
    if "base_branch" in fm:
        out["base_branch"] = _validate_base_branch(fm["base_branch"])
    if "test_cmd" in fm:
        out["test_cmd"] = _validate_test_cmd(fm["test_cmd"])
    if "skip_tests" in fm:
        out["skip_tests"] = _validate_skip_tests(fm["skip_tests"])
    if "title" in fm:
        out["title"] = _validate_optional_string("title", fm["title"])
    if "body" in fm:
        out["body"] = _validate_optional_string("body", fm["body"])
    if "group_id" in fm:
        out["group_id"] = _validate_group_id(fm["group_id"])

    spec = dedent(spec_text).strip()
    if not spec:
        raise InvalidIssue("spec (prose below the frontmatter) is empty")
    out["spec"] = spec

    return out


def _split_frontmatter(body: str) -> tuple[str, str]:
    """Return `(frontmatter_text, spec_text)` or raise `InvalidIssue`.

    Normalizes line endings to LF first so the fence regex is
    predictable regardless of where GitHub sourced the body text.
    """
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    stripped = normalized.lstrip("\n")  # tolerate blank lines before the fence

    if not stripped.startswith("---"):
        raise InvalidIssue("issue body must start with YAML frontmatter (a line reading `---`)")

    fences = list(_FENCE.finditer(stripped))
    if len(fences) < 2:
        raise InvalidIssue(
            "frontmatter is not closed; expected a second `---` line "
            "between the YAML block and the spec"
        )

    opening, closing = fences[0], fences[1]
    if opening.start() != 0:
        raise InvalidIssue("issue body must start with YAML frontmatter (a line reading `---`)")
    return stripped[opening.end() : closing.start()], stripped[closing.end() :]


# ---------- field validators -------------------------------------------


def _validate_scope(value: Any) -> list[str]:
    if value is None:
        raise InvalidIssue("`scope` is required and must be a non-empty list of strings")
    if not isinstance(value, list):
        raise InvalidIssue(
            f"`scope` must be a non-empty list of strings, got {type(value).__name__}"
        )
    if not value:
        raise InvalidIssue("`scope` must be a non-empty list of strings (got [])")
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise InvalidIssue(
                f"`scope[{i}]` must be a string, got {item!r} ({type(item).__name__})"
            )
        if not item.strip():
            raise InvalidIssue(f"`scope[{i}]` must be non-empty, got {item!r}")
    return list(value)


def _validate_base_branch(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidIssue(f"`base_branch` must be a non-empty string, got {value!r}")
    return value


def _validate_test_cmd(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise InvalidIssue(f"`test_cmd` must be null or a non-empty list of strings, got {value!r}")
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise InvalidIssue(
                f"`test_cmd[{i}]` must be a string, got {item!r} ({type(item).__name__})"
            )
    return list(value)


def _validate_skip_tests(value: Any) -> bool:
    # `bool` is-a `int` in Python; the isinstance check lands correctly
    # here only because we accept bools explicitly. We do NOT accept
    # 0/1 — a surprise coercion would hide typos.
    if not isinstance(value, bool):
        raise InvalidIssue(
            f"`skip_tests` must be a boolean, got {value!r} ({type(value).__name__})"
        )
    return value


def _validate_optional_string(key: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidIssue(f"`{key}` must be a string, got {value!r} ({type(value).__name__})")
    return value


def _validate_group_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidIssue(f"`group_id` must be a string, got {value!r} ({type(value).__name__})")
    if not _GROUP_ID_PATTERN.match(value):
        raise InvalidIssue(f"`group_id` must match {_GROUP_ID_PATTERN.pattern!r}, got {value!r}")
    return value


# runbook ok canary
