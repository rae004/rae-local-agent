"""Phase C — standing feed from GitHub issues.

See `PHASE_C_DESIGN.md` for the full design (loop shape, issue body
format, label state model, deployment).

**C3 scope** — enqueue side of the loop goes live. On each tick, for
every pollable issue:

1. Parse the body with `issue_body.parse_issue_body`.
2. On parse error: comment the error, add `agent-invalid`. No job
   written; the issue drops out of future ticks until the human
   fixes the body and removes the label.
3. On parse success: adopt the issue by writing `agent-queued`
   FIRST (idempotency gate — subsequent ticks won't see it), then
   write the job to `jobs/pending/`, then post a comment carrying
   the job id in an HTML tag for C4's lookup.

Order matters. If the label write fails, nothing is enqueued — next
tick retries cleanly. If the label succeeds but the write-pending
fails (filesystem issue), the issue sits in `agent-queued` with no
job and no comment; the operator sees that in the logs and manually
removes labels to retry. If the comment fails, the job runs anyway
and the operator can grep the log for the id — the completion
comment-back (C4) will still find the issue via the agent-queued
label query.

C4 adds the completion comment-back; C5 adds error recovery for
stuck jobs.
"""

from __future__ import annotations

import logging
import signal
import sys
import time

import gh_api
import issue_body
import job
import server
from config import DEFAULT_CONFIG_PATH, Config, InvalidConfig, load_config

log = logging.getLogger("rae-local-agent.issue_poller")

# Granularity of signal observation during the inter-tick sleep. One
# second keeps SIGTERM latency bounded even when the configured
# poll_interval is minutes.
_SLEEP_CHUNK_SECONDS = 1.0

# Label strings. Kept here (not in gh_api) because they're part of the
# poller's state-machine vocabulary, not generic GitHub plumbing.
LABEL_QUEUED = "agent-queued"
LABEL_INVALID = "agent-invalid"


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


def _accept_comment(job_id: str) -> str:
    """The comment body posted after a successful enqueue.

    The HTML tag is machine-readable; C4's completion comment-back
    extracts the job id from it when looking up the status.
    """
    return f"Enqueued as `{job_id}`.\n\n<!-- rae-local-agent:job_id={job_id} -->"


def _reject_comment(reason: str) -> str:
    """The comment body posted when parse or enqueue fails.

    The human reads this directly, so it names the problem and tells
    them how to retry.
    """
    return (
        f"Could not enqueue this issue: {reason}\n\n"
        f"Fix the issue body, then remove the `{LABEL_INVALID}` label "
        f"to retry."
    )


def _reject(repo_slug: str, issue: gh_api.Issue, reason: str) -> None:
    """Reject-path: comment + `agent-invalid` label.

    Writes are best-effort — a failure to label after commenting
    (unlikely in practice) leaves the issue unlabeled; next tick will
    see it again and re-run the parse (which will fail the same way)
    and re-post a comment. The duplicate comment is tolerable; the
    alternative (not surfacing the error) is worse.
    """
    log.info("rejecting %s#%d: %s", repo_slug, issue.number, reason)
    try:
        gh_api.comment_on_issue(repo_slug, issue.number, _reject_comment(reason))
    except gh_api.GhCliError as e:
        log.warning("reject comment on %s#%d failed: %s", repo_slug, issue.number, e)
    try:
        gh_api.add_label(repo_slug, issue.number, LABEL_INVALID)
    except gh_api.GhCliError as e:
        log.warning("reject label on %s#%d failed: %s", repo_slug, issue.number, e)


def _accept(repo_slug: str, issue: gh_api.Issue, parsed: dict, repo_root: str) -> None:
    """Accept-path: build record (can fail), label (idempotency gate),
    write pending, comment.

    Order chosen to put every local-only failure before any external
    write. `_make_job_record` validates `repo_root` and `group_id` —
    if that raises, we haven't touched GitHub yet and the caller can
    re-route to `_reject` cleanly. Only once the record is in hand
    do we add the `agent-queued` label (which is what keeps the next
    tick from re-adopting the same issue). If the label write fails,
    nothing's enqueued and the next tick retries. The window between
    label write and write-pending is sub-ms, so a crash there is
    effectively a non-concern.
    """
    record = server._make_job_record(**parsed, repo_root=repo_root)

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
        # Job already queued and labeled; the worker will still produce a
        # draft PR. The operator can grep the warning for the job id to
        # repost the comment by hand if they care about traceability.
        log.warning(
            "enqueue comment on %s#%d (job %s) failed: %s",
            repo_slug,
            issue.number,
            job_id,
            e,
        )


def _handle_issue(repo_slug: str, issue: gh_api.Issue, repo_root: str) -> None:
    """Parse → enqueue, or parse-error → reject. One issue end-to-end.

    All exceptions below `InvalidIssue` are reclassified as rejection
    reasons too: a surprise failure in `_make_job_record` (e.g.
    `repo_root` isn't a directory anymore) is surfaced to the issue
    author rather than silently crashing the tick.
    """
    try:
        parsed = issue_body.parse_issue_body(issue.body)
    except issue_body.InvalidIssue as e:
        _reject(repo_slug, issue, str(e))
        return

    try:
        _accept(repo_slug, issue, parsed, repo_root)
    except Exception as e:
        _reject(repo_slug, issue, f"enqueue failed: {type(e).__name__}: {e}")


def tick(cfg: Config) -> int:
    """One pass: list pollable issues per repo, handle each. Returns count seen.

    A `gh list` failure on one repo is logged and skipped — one repo's
    auth lapse must not take down the sibling repos.
    """
    total = 0
    for slug, repo_root in cfg.repos.items():
        try:
            issues = gh_api.list_pollable_issues(slug)
        except gh_api.GhCliError as e:
            log.warning("skipping %s this tick: %s", slug, e)
            continue
        for issue in issues:
            _handle_issue(slug, issue, str(repo_root))
            total += 1
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
