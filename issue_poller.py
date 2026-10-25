"""Phase C — standing feed from GitHub issues.

See `PHASE_C_DESIGN.md` for the full design (loop shape, issue body
format, label state model, deployment).

**Current scope (C1-C4)** — each tick runs two passes:

1. **Adopt pass** — list issues matching
   `label:local-agent -label:agent-queued -label:agent-invalid`.
   For each: parse the YAML frontmatter + markdown prose, build a
   job record, add `agent-queued` (idempotency gate), write the job
   to `jobs/pending/`, post a comment carrying the job id in an HTML
   tag. On parse / enqueue failure: comment the error + add
   `agent-invalid`.

2. **Completion pass** — list issues matching
   `label:agent-queued -label:agent-complete`. For each: pull the
   job id from the earlier adopt-pass comment, look up the on-disk
   status, and if the job is terminal (`done` or `failed`), post a
   terminal comment (shape per status, see `_completion_comment`)
   and add `agent-complete`.

Order on the adopt-pass accept-path puts every local-only failure
before any external write — see `_accept`. On the completion pass,
the comment is posted before the label so an HTML tag in the comment
is the idempotency marker even if the label write fails and we revisit
the issue on the next tick.

C5 adds error recovery for stuck jobs; C6 is the end-to-end validation
pass.
"""

from __future__ import annotations

import logging
import re
import signal
import sys
import time

import gh_api
import issue_body
import job
import server
from config import DEFAULT_CONFIG_PATH, Config, InvalidConfig, RepoConfig, load_config

log = logging.getLogger("rae-local-agent.issue_poller")

# Granularity of signal observation during the inter-tick sleep. One
# second keeps SIGTERM latency bounded even when the configured
# poll_interval is minutes.
_SLEEP_CHUNK_SECONDS = 1.0

# Label strings. Kept here (not in gh_api) because they're part of the
# poller's state-machine vocabulary, not generic GitHub plumbing.
LABEL_QUEUED = "agent-queued"
LABEL_INVALID = "agent-invalid"
LABEL_COMPLETE = "agent-complete"

# HTML comment tags we stamp into our own posted comments. Both the
# adopt tag and the completion tag carry the job id: the adopt tag so
# the completion pass can look up the right job, the completion tag so
# idempotency is scoped per-job rather than per-issue. A fresh adopt
# cycle (user removes the agent-* labels to retry) gets a new job id
# and so a new, distinct completion comment — the stale completion
# comment from the prior cycle no longer suppresses it.
_JOB_ID_TAG_RE = re.compile(r"<!-- rae-local-agent:job_id=([A-Za-z0-9-]+) -->")


def _completion_tag(job_id: str) -> str:
    return f"<!-- rae-local-agent:completion:job_id={job_id} -->"


class _StopFlag:
    stopped = False


_flag = _StopFlag()


def _on_signal(_signum, _frame) -> None:
    _flag.stopped = True
    log.info("stop requested")


def _interruptible_sleep(seconds: int) -> None:
    """Sleep in `_SLEEP_CHUNK_SECONDS` chunks so a signal breaks it quickly."""
    chunks = int(seconds / _SLEEP_CHUNK_SECONDS)
    for _ in range(chunks):
        if _flag.stopped:
            return
        time.sleep(_SLEEP_CHUNK_SECONDS)


# ---------- adopt pass -------------------------------------------------


def _accept_comment(job_id: str) -> str:
    """The comment body posted after a successful enqueue.

    The HTML tag is machine-readable; the completion pass extracts the
    job id from it when looking up the status.
    """
    return f"Enqueued as `{job_id}`.\n\n<!-- rae-local-agent:job_id={job_id} -->"


def _reject_comment(reason: str) -> str:
    """Comment body posted when parse or enqueue fails."""
    return (
        f"Could not enqueue this issue: {reason}\n\n"
        f"Fix the issue body, then remove the `{LABEL_INVALID}` label "
        f"to retry."
    )


def _reject(repo_slug: str, issue: gh_api.Issue, reason: str) -> None:
    """Reject-path: comment + `agent-invalid` label. Both best-effort."""
    log.info("rejecting %s#%d: %s", repo_slug, issue.number, reason)
    try:
        gh_api.comment_on_issue(repo_slug, issue.number, _reject_comment(reason))
    except gh_api.GhCliError as e:
        log.warning("reject comment on %s#%d failed: %s", repo_slug, issue.number, e)
    try:
        gh_api.add_label(repo_slug, issue.number, LABEL_INVALID)
    except gh_api.GhCliError as e:
        log.warning("reject label on %s#%d failed: %s", repo_slug, issue.number, e)


def _accept(
    repo_slug: str,
    issue: gh_api.Issue,
    parsed: dict,
    repo_cfg: RepoConfig,
) -> None:
    """Build record (can fail) → label (idempotency gate) → write pending → comment.

    Local-only failures (bad `repo_root`, bad `group_id`) propagate up
    to the caller without any gh write. Label failure skips the
    enqueue so the next tick retries cleanly. Comment failure after
    the job is written is logged but non-fatal — the worker still
    produces a PR.

    `repo_cfg.gate_cmds` is used as the gate when the issue frontmatter
    didn't specify its own. Issue-frontmatter `gate_cmds` or `test_cmd`
    always wins — the parser raises on both, so passing `parsed` through
    unchanged is safe.
    """
    kwargs = dict(parsed)
    kwargs["repo_root"] = str(repo_cfg.path)
    if repo_cfg.gate_cmds is not None and "gate_cmds" not in kwargs and "test_cmd" not in kwargs:
        kwargs["gate_cmds"] = [list(cmd) for cmd in repo_cfg.gate_cmds]
    record = server._make_job_record(**kwargs)
    try:
        gh_api.add_label(repo_slug, issue.number, LABEL_QUEUED)
    except gh_api.GhCliError as e:
        log.warning("adopt label on %s#%d failed, skipping: %s", repo_slug, issue.number, e)
        return

    job_id = job.write_pending(record)
    log.info("enqueued %s#%d as %s", repo_slug, issue.number, job_id)

    try:
        gh_api.comment_on_issue(repo_slug, issue.number, _accept_comment(job_id))
    except gh_api.GhCliError as e:
        log.warning(
            "enqueue comment on %s#%d (job %s) failed: %s",
            repo_slug,
            issue.number,
            job_id,
            e,
        )


def _handle_issue(repo_slug: str, issue: gh_api.Issue, repo_cfg: RepoConfig) -> None:
    """Parse → enqueue, or route to reject on any failure."""
    try:
        parsed = issue_body.parse_issue_body(issue.body)
    except issue_body.InvalidIssue as e:
        _reject(repo_slug, issue, str(e))
        return

    try:
        _accept(repo_slug, issue, parsed, repo_cfg)
    except Exception as e:
        _reject(repo_slug, issue, f"enqueue failed: {type(e).__name__}: {e}")


def _adopt_pass(repo_slug: str, repo_cfg: RepoConfig) -> int:
    """List pollable issues on `repo_slug` and route each through `_handle_issue`.

    A `gh list` failure here is logged + skipped so sibling repos
    aren't taken down by one repo's auth lapse.
    """
    try:
        issues = gh_api.list_pollable_issues(repo_slug)
    except gh_api.GhCliError as e:
        log.warning("skipping %s this tick (adopt): %s", repo_slug, e)
        return 0
    for issue in issues:
        _handle_issue(repo_slug, issue, repo_cfg)
    return len(issues)


# ---------- completion pass --------------------------------------------


def _extract_job_id(comments: list[str]) -> str | None:
    """First `rae-local-agent:job_id=X` tag in any comment, or None.

    Comments are scanned in posted order; the adopt pass drops exactly
    one tag per adoption, so "first match" is "the one we care about".
    """
    for body in comments:
        m = _JOB_ID_TAG_RE.search(body or "")
        if m is not None:
            return m.group(1)
    return None


def _lookup_job_status(job_id: str) -> dict | None:
    """Pull the terminal-state artifacts from disk for `job_id`.

    Returns None if the job doesn't exist. Returns a dict with just
    `state` if the job is still pending/running. For terminal states
    (done/failed) also returns `status`, `pr_url`, `diff`, `log` so
    the comment-formatter has everything it needs.
    """
    found = job.find_record(job_id)
    if found is None:
        return None
    state, _record = found
    if state not in ("done", "failed"):
        return {"state": state}
    return {
        "state": state,
        "status": job.read_artifact(job_id, state, "status.txt").strip(),
        "pr_url": job.read_artifact(job_id, state, "pr_url.txt").strip(),
        "diff": job.read_artifact(job_id, state, "diff.patch"),
        "log": job.read_artifact(job_id, state, "log.txt"),
    }


def _completion_comment(job_id: str, status: dict) -> str:
    """Format the terminal comment per `PHASE_C_DESIGN.md` § "The comment-back".

    Four shapes: `ok` (PR URL + 5-line diff preview + collapsible full
    diff), `tests_failed` (first 20 log lines in a code fence),
    `model_declined` (short note), `error:...` (verbatim). Every
    output ends with the per-job completion tag so later ticks skip it
    for *this* job (a later cycle with a new job id still posts).
    """
    txt = status["status"]
    tag = _completion_tag(job_id)

    if txt == "ok":
        pr_url = status["pr_url"] or "(no pr_url recorded)"
        diff = status["diff"]
        if diff:
            preview = "\n".join(diff.splitlines()[:5])
            body = (
                f"Done — draft PR: {pr_url}\n\n"
                f"First lines of the diff:\n\n"
                f"```diff\n{preview}\n```\n\n"
                f"<details><summary>Full diff</summary>\n\n"
                f"```diff\n{diff}\n```\n\n"
                f"</details>"
            )
        else:
            body = f"Done — draft PR: {pr_url}"
        return f"{body}\n\n{tag}"

    if txt == "tests_failed":
        log_text = status["log"]
        if log_text:
            head = "\n".join(log_text.splitlines()[:20])
            tail_note = (
                f"\n\nFull log in the worker's artifacts under `jobs/failed/{job_id}/log.txt`."
            )
        else:
            # Not "no log file" — the test tool ran and emitted zero bytes
            # (e.g. plain `false`). Wording picked to match that.
            head = "(test command emitted no output)"
            tail_note = ""
        return (
            f"Tests failed — the diff was applied in a worktree but the "
            f"test command returned non-zero, so no branch was pushed. "
            f"First 20 lines of the log:\n\n"
            f"```\n{head}\n```"
            f"{tail_note}\n\n"
            f"{tag}"
        )

    if txt == "model_declined":
        return (
            "The model returned no diff. This usually means the spec was "
            "too open-ended for a mechanical rewrite; try tightening it "
            "to a specific before/after.\n\n"
            f"{tag}"
        )

    if txt.startswith("error:"):
        return f"Job errored: `{txt}`\n\n{tag}"

    # Catch-all — unexpected status strings aren't an invariant we
    # should crash on; just report them verbatim.
    return f"Job completed with unknown status: `{txt}`\n\n{tag}"


def _handle_completion(repo_slug: str, issue: gh_api.Issue) -> None:
    """One issue of the completion pass: look up the job, post + label if terminal.

    Skips (no writes) if: the job id tag isn't in any comment, the job
    doesn't exist on disk, or the job is still pending/running. The
    per-job HTML completion tag on an existing comment is checked
    before posting so a prior comment + failed-label-write doesn't
    double-post on the next tick. The check is scoped to *this*
    job_id: a retry cycle (new adopt → new job id) still posts even
    though an older cycle's completion comment is still on the issue.
    """
    try:
        comments = gh_api.get_issue_comments(repo_slug, issue.number)
    except gh_api.GhCliError as e:
        log.warning("fetching comments for %s#%d failed: %s", repo_slug, issue.number, e)
        return

    job_id = _extract_job_id(comments)
    if job_id is None:
        log.warning(
            "%s#%d has `%s` but no job_id tag in its comments; skipping",
            repo_slug,
            issue.number,
            LABEL_QUEUED,
        )
        return

    status = _lookup_job_status(job_id)
    if status is None:
        log.warning(
            "job %s not found on disk; skipping completion for %s#%d",
            job_id,
            repo_slug,
            issue.number,
        )
        return
    if status["state"] not in ("done", "failed"):
        log.info(
            "%s#%d still waiting on job %s (state=%s)",
            repo_slug,
            issue.number,
            job_id,
            status["state"],
        )
        return

    # Already posted a completion comment for *this* job on a prior
    # tick? Just add the label (the only reason we'd be here is that
    # the label write failed after the comment went out). A stale
    # completion comment from a previous cycle with a different job id
    # does not match this check and so does not suppress the new
    # cycle's comment.
    per_job_tag = _completion_tag(job_id)
    if any(per_job_tag in (c or "") for c in comments):
        try:
            gh_api.add_label(repo_slug, issue.number, LABEL_COMPLETE)
        except gh_api.GhCliError as e:
            log.warning(
                "complete label (retry) on %s#%d failed: %s",
                repo_slug,
                issue.number,
                e,
            )
        return

    try:
        gh_api.comment_on_issue(
            repo_slug,
            issue.number,
            _completion_comment(job_id, status),
        )
    except gh_api.GhCliError as e:
        log.warning("complete comment on %s#%d failed: %s", repo_slug, issue.number, e)
        return
    try:
        gh_api.add_label(repo_slug, issue.number, LABEL_COMPLETE)
    except gh_api.GhCliError as e:
        log.warning("complete label on %s#%d failed: %s", repo_slug, issue.number, e)
    else:
        log.info(
            "completed %s#%d (job %s, status=%s)",
            repo_slug,
            issue.number,
            job_id,
            status["status"],
        )


def _completion_pass(repo_slug: str) -> int:
    """List issues awaiting completion on `repo_slug` and process each.

    Returns the number of issues seen (not the number closed out — a
    completion comment is skipped if the job is still running).
    """
    try:
        issues = gh_api.list_issues_awaiting_completion(repo_slug)
    except gh_api.GhCliError as e:
        log.warning("skipping %s this tick (completion): %s", repo_slug, e)
        return 0
    for issue in issues:
        _handle_completion(repo_slug, issue)
    return len(issues)


# ---------- loop -------------------------------------------------------


def tick(cfg: Config) -> int:
    """Two passes per repo: adopt new issues, then close out queued ones.

    A `gh list` failure in either pass of one repo is logged and the
    tick moves on — one repo's auth lapse must not take down the
    siblings. Returns the total adopt-count across repos (a soft
    indicator for logging; not a correctness contract).
    """
    total = 0
    for slug, repo_cfg in cfg.repos.items():
        total += _adopt_pass(slug, repo_cfg)
        _completion_pass(slug)
    return total


def main() -> int:
    """Entry point. Returns a process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(DEFAULT_CONFIG_PATH)
    except InvalidConfig as e:
        log.error("invalid config at %s: %s", DEFAULT_CONFIG_PATH, e)
        return 1

    log.info(
        "poller up with %d repo(s), poll_interval=%ds",
        len(cfg.repos),
        cfg.poll_interval_seconds,
    )
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    while not _flag.stopped:
        tick(cfg)
        if _flag.stopped:
            break
        _interruptible_sleep(cfg.poll_interval_seconds)
    log.info("poller down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
