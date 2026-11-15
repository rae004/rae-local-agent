#!/usr/bin/env python3
"""Repository settings as code: diff or apply `.github/repo/*.json` against GitHub.

    python scripts/repo_settings.py diff    # exit 1 on drift
    python scripts/repo_settings.py apply   # needs repo admin

Talks to the API through `gh api`, so it uses your gh login locally and GH_TOKEN in CI.
`{owner}/{repo}` is resolved by gh from the current git remote. Stdlib only.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1] / ".github" / "repo"
REPO = "repos/{owner}/{repo}"


@dataclass(frozen=True)
class Target:
    file: str
    get: str
    method: str
    put: str


TARGETS = [
    Target("repository.json", REPO, "PATCH", REPO),
    Target(
        "actions-permissions.json",
        f"{REPO}/actions/permissions",
        "PUT",
        f"{REPO}/actions/permissions",
    ),
    Target(
        "actions-workflow.json",
        f"{REPO}/actions/permissions/workflow",
        "PUT",
        f"{REPO}/actions/permissions/workflow",
    ),
]


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def gh(method: str, path: str, body: Any = None) -> Any:
    cmd = ["gh", "api", "-X", method, path]
    stdin = None
    if body is not None:
        cmd += ["--input", "-"]
        stdin = json.dumps(body)
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        try:
            err = json.loads(proc.stdout or "{}")
            raise ApiError(int(err.get("status", 0)), str(err.get("message", proc.stderr.strip())))
        except ValueError:
            raise ApiError(0, (proc.stdout + proc.stderr).strip()) from None
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def normalize(value: Any) -> Any:
    """Sort lists of objects so ordering differences aren't drift."""
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [normalize(v) for v in value]
        if items and all(isinstance(i, dict) for i in items):
            return sorted(items, key=lambda i: json.dumps(i, sort_keys=True))
        return items
    return value


MISSING = object()

Change = tuple[str, Any, Any]


def drift(desired: Any, live: Any, path: str = "") -> tuple[list[Change], list[str]]:
    """(changed keys, unverifiable keys). Extra live keys are ignored.

    GitHub omits admin-only fields (merge options, `bypass_actors`, …) when the
    token lacks admin, so a key missing from the live object is "can't tell",
    not drift.
    """
    if live is MISSING:
        return [], [path]
    if isinstance(desired, dict) and isinstance(live, dict):
        changed: list[Change] = []
        unverified: list[str] = []
        for key, want in desired.items():
            c, u = drift(want, live.get(key, MISSING), f"{path}.{key}" if path else key)
            changed += c
            unverified += u
        return changed, unverified
    if normalize(desired) != normalize(live):
        return [(path, desired, live)], []
    return [], []


def load(file: Path) -> Any:
    return json.loads(file.read_text())


def report(name: str, changes: list[Change], unverified: list[str]) -> None:
    if not changes:
        note = f" ({len(unverified)} admin-only keys unverified)" if unverified else ""
        print(f"  ok       {name}{note}")
    else:
        print(f"  DRIFT    {name}")
        for path, want, have in changes:
            print(f"           {path}: live={json.dumps(have)} -> want={json.dumps(want)}")
    if unverified:
        print(f"           unverified with this token: {', '.join(unverified)}")


def check_targets(apply: bool) -> int:
    drifted = 0
    for t in TARGETS:
        desired = load(ROOT / t.file)
        try:
            live = gh("GET", t.get)
        except ApiError as e:
            if e.status in (403, 404):
                print(f"  skipped  {t.file} (needs admin token: {e})")
                continue
            raise
        changes, unverified = drift(desired, live)
        report(t.file, changes, unverified)
        if changes:
            drifted += 1
            if apply:
                gh(t.method, t.put, desired)
                print(f"  applied  {t.file}")
    return drifted


def check_rulesets(apply: bool) -> int:
    drifted = 0
    live_list = {r["name"]: r["id"] for r in gh("GET", f"{REPO}/rulesets") or []}
    for file in sorted((ROOT / "rulesets").glob("*.json")):
        desired = load(file)
        name = f"rulesets/{file.name}"
        ruleset_id = live_list.get(desired["name"])
        if ruleset_id is None:
            print(f"  DRIFT    {name} (not on GitHub)")
            drifted += 1
            if apply:
                gh("POST", f"{REPO}/rulesets", desired)
                print(f"  created  {name}")
            continue
        live = gh("GET", f"{REPO}/rulesets/{ruleset_id}")
        changes, unverified = drift(desired, live)
        report(name, changes, unverified)
        if changes:
            drifted += 1
            if apply:
                gh("PUT", f"{REPO}/rulesets/{ruleset_id}", desired)
                print(f"  applied  {name}")
    return drifted


def check_labels(apply: bool) -> int:
    """Issue labels from `labels.json` (a list of create-label bodies).

    Matched to live labels by exact name; extra live labels are
    ignored, same as extra keys elsewhere. The file is optional so the
    script stays drop-in portable to repos that don't manage labels.
    """
    file = ROOT / "labels.json"
    if not file.exists():
        return 0
    drifted = 0
    live = {label["name"]: label for label in gh("GET", f"{REPO}/labels?per_page=100") or []}
    for want in load(file):
        name = f"labels/{want['name']}"
        have = live.get(want["name"])
        if have is None:
            print(f"  DRIFT    {name} (not on GitHub)")
            drifted += 1
            if apply:
                gh("POST", f"{REPO}/labels", want)
                print(f"  created  {name}")
            continue
        changes, _ = drift(want, have)
        report(name, changes, [])
        if changes:
            drifted += 1
            if apply:
                gh("PATCH", f"{REPO}/labels/{want['name']}", want)
                print(f"  applied  {name}")
    return drifted


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"diff", "apply"}:
        print(__doc__)
        return 2
    apply = argv[1] == "apply"
    print(f"{'Applying' if apply else 'Comparing'} .github/repo against GitHub")
    try:
        drifted = check_targets(apply) + check_rulesets(apply) + check_labels(apply)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if drifted and not apply:
        print(
            f"\n{drifted} target(s) drifted. "
            "Run `python3 scripts/repo_settings.py apply` to push the files."
        )
        return 1
    print("\nAll settings match." if not drifted else f"\n{drifted} target(s) applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
