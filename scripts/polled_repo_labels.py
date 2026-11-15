#!/usr/bin/env python3
"""Ensure the poller's issue labels exist on every repo in `repos.yaml`.

    uv run python scripts/polled_repo_labels.py diff    # exit 1 on drift
    uv run python scripts/polled_repo_labels.py apply   # create/patch labels

Companion to `repo_settings.py`, which manages THIS repo's settings from
`.github/repo/`. The poller needs its four labels on every repo it
polls, and `repos.yaml` is machine-local config, so CI can't check this
— run it locally, like `apply`. Desired labels come from
`.github/repo/labels.json`; matching is by exact name, extra live
labels are ignored, and a repo this token can't read is skipped rather
than failing the run (mirroring the poller's own per-repo resilience).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repo_settings import ROOT, ApiError, drift, gh, load, report  # noqa: E402

from config import DEFAULT_CONFIG_PATH, InvalidConfig, load_config  # noqa: E402

LABELS_FILE = ROOT / "labels.json"


def check_repo(slug: str, desired: list, apply: bool) -> int:
    print(f"{slug}:")
    try:
        live = {
            label["name"]: label for label in gh("GET", f"repos/{slug}/labels?per_page=100") or []
        }
    except ApiError as e:
        print(f"  skipped  (cannot read labels: {e})")
        return 0
    drifted = 0
    for want in desired:
        name = f"labels/{want['name']}"
        have = live.get(want["name"])
        if have is None:
            print(f"  DRIFT    {name} (not on GitHub)")
            drifted += 1
            if apply:
                gh("POST", f"repos/{slug}/labels", want)
                print(f"  created  {name}")
            continue
        changes, _ = drift(want, have)
        report(name, changes, [])
        if changes:
            drifted += 1
            if apply:
                gh("PATCH", f"repos/{slug}/labels/{want['name']}", want)
                print(f"  applied  {name}")
    return drifted


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"diff", "apply"}:
        print(__doc__)
        return 2
    apply = argv[1] == "apply"
    try:
        cfg = load_config(DEFAULT_CONFIG_PATH)
    except InvalidConfig as e:
        print(f"error: invalid config at {DEFAULT_CONFIG_PATH}: {e}", file=sys.stderr)
        return 1
    desired = load(LABELS_FILE)
    verb = "Applying" if apply else "Comparing"
    print(f"{verb} labels.json against {len(cfg.repos)} polled repo(s)")
    drifted = 0
    try:
        for slug in cfg.repos:
            drifted += check_repo(slug, desired, apply)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if drifted and not apply:
        print(f"\n{drifted} label(s) drifted. Run `uv run python {argv[0]} apply`.")
        return 1
    print("\nAll labels match." if not drifted else f"\n{drifted} label(s) applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
