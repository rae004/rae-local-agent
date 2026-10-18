"""Phase C — standing feed from GitHub issues.

See `PHASE_C_DESIGN.md` for the full design (the loop shape, the
issue body format, the label state model, deployment).

**C0 scope** — this is a stub: load + validate the config, log that
the poller is up, exit cleanly. No GitHub calls, no polling loop,
no state writes.

The deliberately-tiny `main` returns an exit code so systemd's
`Restart=on-failure` can distinguish a config problem (non-zero,
don't restart indefinitely) from a clean exit (zero).
"""

from __future__ import annotations

import logging
import sys

from config import DEFAULT_CONFIG_PATH, InvalidConfig, load_config

log = logging.getLogger("rae-local-agent.issue_poller")


def main() -> int:
    """Entry point. Returns a process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Resolve DEFAULT_CONFIG_PATH fresh at call time so tests can monkeypatch
    # it on the module without worrying about default-arg binding.
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
