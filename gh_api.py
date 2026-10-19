"""Thin shim around the `gh` CLI for the issue poller.

Keeps subprocess calls isolated so the poller's `tick` logic can be
unit-tested with a mocked `list_pollable_issues` / `comment_on_issue`
/ `add_label`. The `Issue` dataclass is intentionally smaller than a
full GitHub issue — only the fields the poller reads.

C1 added listing. C3 adds the two write-path calls (comment, label)
the poller needs to adopt and reject issues.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger("rae-local-agent.gh_api")

# Issues the poller cares about: labeled `local-agent`, not yet
# enqueued or rejected, and still open. GitHub search syntax; `-label:`
# negates. Kept as a module constant so tests can import it rather than
# duplicate the string.
POLL_SEARCH = "is:open label:local-agent -label:agent-queued -label:agent-invalid"

# Ceiling on how long we'll wait for `gh` on a single call. Beyond this
# the network or gh itself is clearly stuck; the caller logs + skips.
_GH_TIMEOUT_SECONDS = 30


class GhCliError(RuntimeError):
    """`gh` call failed or returned something we couldn't parse.

    Caller logs the message and moves on. Message is human-readable
    and names the repo + the cause.
    """


@dataclass(frozen=True)
class Issue:
    """One row of a `gh issue list --json number,title,url,body` response.

    `body` can be an empty string if the author left the issue body
    blank — that's the parser's problem, not this layer's.
    """

    number: int
    title: str
    url: str
    body: str


def _run_gh(cmd: list[str], *, context: str) -> subprocess.CompletedProcess[str]:
    """Common path: run `gh`, map every failure shape onto `GhCliError`.

    `context` is prepended to any error message so the operator knows
    which operation blew up (listing, commenting, labeling, etc.).
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as e:
        raise GhCliError(f"`gh` CLI not found on PATH: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise GhCliError(f"{context} timed out after {_GH_TIMEOUT_SECONDS}s") from e


def list_pollable_issues(repo_slug: str) -> list[Issue]:
    """Return open issues matching the poller filter for `repo_slug`.

    Raises `GhCliError` on any failure: non-zero exit, timeout, bad
    JSON, or `gh` not on PATH.
    """
    cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo_slug,
        "--search",
        POLL_SEARCH,
        "--json",
        "number,title,url,body",
    ]
    result = _run_gh(cmd, context=f"gh issue list for {repo_slug}")

    if result.returncode != 0:
        stderr = result.stderr.strip() or "(no stderr)"
        raise GhCliError(f"gh issue list for {repo_slug} failed (rc={result.returncode}): {stderr}")

    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as e:
        raise GhCliError(f"gh issue list for {repo_slug} returned invalid JSON: {e}") from e

    if not isinstance(data, list):
        raise GhCliError(
            f"gh issue list for {repo_slug} returned {type(data).__name__}, expected list"
        )

    return [
        Issue(
            number=d["number"],
            title=d["title"],
            url=d["url"],
            # `gh` sometimes omits `body` when it's empty; default to ''.
            body=d.get("body") or "",
        )
        for d in data
    ]


def comment_on_issue(repo_slug: str, issue_number: int, body: str) -> None:
    """Post `body` as a new comment on the issue. Raises `GhCliError` on failure.

    Uses `gh issue comment --body-file -` with stdin so the body text
    doesn't collide with shell quoting (parse errors can contain
    arbitrary punctuation, HTML comments, newlines).
    """
    cmd = [
        "gh",
        "issue",
        "comment",
        str(issue_number),
        "--repo",
        repo_slug,
        "--body-file",
        "-",
    ]
    context = f"gh issue comment on {repo_slug}#{issue_number}"
    try:
        result = subprocess.run(
            cmd,
            input=body,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as e:
        raise GhCliError(f"`gh` CLI not found on PATH: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise GhCliError(f"{context} timed out after {_GH_TIMEOUT_SECONDS}s") from e

    if result.returncode != 0:
        stderr = result.stderr.strip() or "(no stderr)"
        raise GhCliError(f"{context} failed (rc={result.returncode}): {stderr}")


def add_label(repo_slug: str, issue_number: int, label: str) -> None:
    """Add `label` to the issue. Raises `GhCliError` on failure.

    `gh issue edit --add-label` is idempotent: adding a label the
    issue already has is a no-op (returns 0). The poller relies on
    that for recovery from a partial write.
    """
    cmd = [
        "gh",
        "issue",
        "edit",
        str(issue_number),
        "--repo",
        repo_slug,
        "--add-label",
        label,
    ]
    context = f"gh issue edit --add-label {label!r} on {repo_slug}#{issue_number}"
    result = _run_gh(cmd, context=context)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "(no stderr)"
        raise GhCliError(f"{context} failed (rc={result.returncode}): {stderr}")
