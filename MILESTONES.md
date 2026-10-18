# Milestones

Walking-skeleton approach: each milestone leaves the project in a
working, demonstrable state. The MCP server and the worker evolve
together one slice at a time — never a half-wired state across the two.

## Design decisions baked into this plan

Decided before writing a line of Phase B code; captured here so the plan
is legible from first principles.

- **Worker runs as a systemd user service on `command`.** Phase B's
  whole point is "keeps running while I'm not watching". A worker tied
  to a terminal or to the MCP subprocess defeats that.
- **The worker opens a draft PR, not a real one, and never merges.**
  Everything the reviewer needs is on GitHub; nothing merges without a
  human. The PR body is the job spec, so the audit trail is built in.
- **Test gate runs by default.** After applying the diff, the worker
  runs the repo's test command; failures mark the job failed and the
  branch does not push. The job spec can set `skip_tests: true` for
  intentional-behavior-change jobs.

Everything beyond this list — queue persistence, systemd unit shape,
MCP tool surface — is implementation detail, not design, and can
change without an ADR.

## Milestones

### B0 — Scaffold

Lay down the directory shapes and dependencies. No logic yet.

**Done means:**
- `jobs/{pending,running,done,failed}/` exist with `.gitkeep`
- `worker.py` is a stub `main()` that logs "worker up" and exits
- any new deps are in `pyproject.toml` (defer additions until a
  milestone actually needs one)

### B1 — Walking skeleton: enqueue + pickup

Minimum viable loop. No git, no PR, no model call yet.

- `enqueue_job(spec, scope, repo_root)` MCP tool writes
  `jobs/pending/<ulid>.json` and returns the id.
- Worker main loop watches `jobs/pending/`; on a new file, moves it
  to `jobs/running/`, logs "picked up <id>", moves it to `jobs/done/`.

**Done means:** calling `enqueue_job` results in a job file flowing
pending → running → done within 5 s. Worker survives being started,
stopped, and restarted mid-job without losing or double-processing it.

### B2 — Model handoff

Worker actually produces a diff.

- Worker calls the existing `apply_transformation` logic from
  `server.py` (factor into a shared module if needed).
- Writes the diff to `jobs/done/<id>/diff.patch`.
- Writes a one-line status to `jobs/done/<id>/status.txt`
  (`ok | model_declined | error:<msg>`).

**Done means:** running a real transformation spec through
`enqueue_job` lands a usable `diff.patch` in `done/<id>/`. A human can
`git apply` it manually and get the expected result.

### B3 — Git branch + commit + push

Worker takes the diff from B2 and makes it a real branch.

- Before anything: refuse if the target repo's working tree is dirty
  (`git status --porcelain` is non-empty).
- Create branch `rae-local-agent/<job-id>` from current HEAD of the
  configured base branch (default `main`).
- `git apply` the diff, commit with the job spec as the message, push.

**Done means:** a real transformation ends with a pushed branch visible
on GitHub. The branch applies cleanly on top of `main`.

### B4 — Test gate

Verify the change doesn't break the repo before pushing.

- After `git apply`, worker runs the repo's test command. The job spec
  declares `test_cmd` (default `uv run pytest -q`).
- Tests fail → branch is **not** pushed, working tree is reset to HEAD,
  job moves to `failed/<id>/` with test output in `log.txt`.
- `skip_tests: true` in the spec bypasses the gate.

**Done means:** a deliberately-broken job fails cleanly with output
captured, a passing job pushes as before, and the override works.

### B5 — Draft PR creation

The worker completes the handoff.

- After push: `gh pr create --draft --title "<spec summary>" --body
  "<full spec>" --assignee @me`.
- PR URL written to `jobs/done/<id>/pr_url.txt`.

**Done means:** finished jobs appear in the user's GitHub as draft PRs
assigned to them, with the spec visible in the PR description.

### B6 — Status tool

The MCP side of the loop.

- `get_job_status(job_id) -> {status, diff, log, pr_url?}` reads from
  any of pending/running/done/failed.
- `list_jobs(filter?) -> [{id, status, created_at, spec_summary}]` for
  queue health.

**Done means:** from a Claude Code session, I can enqueue a job, work
on something else, and later ask "what's the status of job X" and get
a correct, timely answer.

### B7 — Systemd user service

Package the worker so it survives reboots.

- `rae-local-agent-worker.service` unit file in the repo
- Install instructions in the README for
  `systemctl --user enable --now rae-local-agent-worker`
- Worker uses stdlib `logging`; journald captures it

**Done means:** `systemctl --user status rae-local-agent-worker` shows
a healthy service that picks up jobs enqueued by any Claude Code
session on `command`, and keeps picking them up across reboots.

### B8 — End-to-end integration

One real job through the full pipeline, on a real repo.

**Done means:** a documented first-run against the trainlab repo (or
another target) results in a draft PR with a passing test suite, zero
human intervention between `enqueue_job` and the PR URL landing in
`get_job_status`.

**Status: done**, with one honest caveat. Three draft PRs on
[rae-budget](https://github.com/rae004/rae-budget):

- [#236](https://github.com/rae004/rae-budget/pull/236): first real
  run on `categories.py`. The verbose PR title prompted the
  `title`/`body` override feature
  ([rae-local-agent#20](https://github.com/rae004/rae-local-agent/pull/20)).
- [#237](https://github.com/rae004/rae-budget/pull/237): `bills.py`,
  shaped with the new title/body params.
- [#238](https://github.com/rae004/rae-budget/pull/238):
  `bill_templates.py`, peer to #237.

#237 and #238 shipped with `skip_tests=true` because the test gate
was running under rae-local-agent's venv instead of the target
repo's. Fixed in
[#22](https://github.com/rae004/rae-local-agent/issues/22); the gate
now scrubs `VIRTUAL_ENV`/`UV_PROJECT_ENVIRONMENT` and symlinks the
target repo's `.venv` into the worktree so `uv run` resolves against
the target.

See the "First real run" section of the README for the readable
version.

### B9 — Group jobs into one PR

Phase B assumed one PR per job. For a mechanical sweep across N files
that is a nuisance: N PRs clog the review queue and lose the "these
belong together" signal. B9 adds an explicit `group_id` on
`enqueue_job` so a batch of related jobs lands as sequential commits
on one `rae-local-agent/<group_id>` branch + one draft PR.

Routing per job:

- No `group_id` → classic shape (per-job branch, per-job PR).
- `group_id` set, branch not on origin → **first job of the group**:
  branches off `base_branch`, opens the draft PR with this job's
  `title`/`body`.
- `group_id` set, branch on origin → **later job of the group**: bases
  the new commit on the existing branch, pushes, surfaces the
  already-open PR's URL. First-job-wins on PR title/body; later jobs
  are commits only.

`group_id` format: `^[a-z0-9][a-z0-9-]{0,62}$`. Enforced at the MCP
tool boundary with a clear error. Readable in GitHub's branch list,
safe as a git ref component.

**Done means:** a batch of jobs sharing a `group_id` from a single
Claude Code session produces exactly one draft PR with one commit
per job, and the status tool surfaces the shared PR URL for every
job in the group.

**Status: done.** First grouped run on rae-budget:
[#242](https://github.com/rae004/rae-budget/pull/242) — three jobs
(`data_management.py`, `pay_periods.py`, `spending.py`) sharing
`group_id="rae-budget-routes"`, one draft PR with three commits.

---

Beyond Phase B is Phase C: a standing feed of jobs sourced from GitHub
issues. Deliberately out of scope here.
