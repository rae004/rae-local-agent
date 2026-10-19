"""Phase C — standing feed from GitHub issues.

See `PHASE_C_DESIGN.md` for the full design (loop shape, issue body
format, label state model, deployment).

**C1 scope** — polling loop (read-only). On each tick, lists open
issues on every configured repo whose labels match
`label:local-agent -label:agent-queued -label:agent-invalid` and logs
them. Nothing is enqueued, no labels or comments written, no local
state touched. C2 layers the body parser on top; C3 adds the enqueue
step.

Signal handling matches `worker.py`: SIGINT or SIGTERM flips a shared
`_StopFlag`, the loop finishes its current tick and the sleep wakes
within a second. systemd `systemctl --user stop` is a clean exit, so
`Restart=on-failure` won't restart it.
"""

from __future__ import annotations

import logging
import signal
import sys
import time

import gh_api
from config import DEFAULT_CONFIG_PATH, Config, InvalidConfig, load_config

log = logging.getLogger("rae-local-agent.issue_poller")

# Granularity of signal observation during the inter-tick sleep. One
# second keeps SIGTERM latency bounded even when the configured
# poll_interval is minutes.
_SLEEP_CHUNK_SECONDS = 1.0


class _StopFlag:
    stopped = False


_flag = _StopFlag()


def _on_signal(_signum, _frame) -> None:
    _flag.stopped = True
    log.info("stop requested")


def _interruptible_sleep(seconds: int) -> None:
    """Sleep in `_SLEEP_CHUNK_SECONDS` chunks so a signal breaks it quickly.

    `time.sleep(300)` on its own would hold the process for the full
    interval regardless of the signal. Breaking it up means SIGTERM is
    observed within ~1s even at the default 5-minute cadence.
    """
    chunks = int(seconds / _SLEEP_CHUNK_SECONDS)
    for _ in range(chunks):
        if _flag.stopped:
            return
        time.sleep(_SLEEP_CHUNK_SECONDS)


def tick(cfg: Config) -> int:
    """One pass: list pollable issues per repo, log them. Returns the total.

    A `gh` failure on one repo is logged and skipped — it must not take
    out the whole tick, because repo B shouldn't go dark just because
    repo A's token lapsed.
    """
    total = 0
    for slug in cfg.repos:
        try:
            issues = gh_api.list_pollable_issues(slug)
        except gh_api.GhCliError as e:
            log.warning("skipping %s this tick: %s", slug, e)
            continue
        for issue in issues:
            log.info("%s #%d: %s", slug, issue.number, issue.title)
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
