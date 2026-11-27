#!/usr/bin/env python3
"""Ensure the poller's issue labels exist on every repo in `repos.yaml`.

    uv run python scripts/polled_repo_labels.py diff    # exit 1 on drift
    uv run python scripts/polled_repo_labels.py apply   # create/patch labels

The poller needs its four labels on every repo it polls, and `repos.yaml`
is machine-local config, so CI can't check this — run it locally. Desired
labels are the poller's own entries (`local-agent` and `agent-*`) in the
`labels` list in `.github/repo-settings.json`, the list the fleet baseline in
`rae004/.github` keeps in place on THIS repo. That list also holds labels
that belong to this repo alone, such as Dependabot's; those are not synced.
Matching is by exact name, extra live labels are ignored, and a repo this
token can't read is skipped rather than failing the run (mirroring the
poller's own per-repo resilience). Talks to the API through `gh api`.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import DEFAULT_CONFIG_PATH, InvalidConfig, load_config  # noqa: E402

SETTINGS_FILE = ROOT / ".github" / "repo-settings.json"


def is_poller_label(name: str) -> bool:
    """True for the labels the poller reads and writes on a polled repo."""
    return name == "local-agent" or name.startswith("agent-")


class ApiError(Exception):
    pass


def gh(method: str, path: str, body: Any = None) -> Any:
    cmd = ["gh", "api", "-X", method, path]
    stdin = None
    if body is not None:
        cmd += ["--input", "-"]
        stdin = json.dumps(body)
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ApiError((proc.stderr or proc.stdout).strip())
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def check_repo(slug: str, desired: list[dict[str, Any]], apply: bool) -> int:
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
        changes = {k: (have.get(k), v) for k, v in want.items() if have.get(k) != v}
        if not changes:
            print(f"  ok       {name}")
            continue
        print(f"  DRIFT    {name}")
        for key, (old, new) in changes.items():
            print(f"           {key}: live={json.dumps(old)} -> want={json.dumps(new)}")
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
    desired = [
        label
        for label in json.loads(SETTINGS_FILE.read_text())["labels"]
        if is_poller_label(label["name"])
    ]
    verb = "Applying" if apply else "Comparing"
    print(f"{verb} poller labels against {len(cfg.repos)} polled repo(s)")
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
