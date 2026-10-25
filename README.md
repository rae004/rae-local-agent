# local-agent

An MCP server that exposes a local Ollama coder model as a bounded
code-transformation tool for [Claude Code](https://claude.com/claude-code)
(or any MCP client).

Hand it a tight mechanical spec and a scope of files; it hands them to a
local coder model and returns a unified diff. The caller (Claude Code,
you) reviews the diff and applies it with its own tools. **Advisory
only**: this server does not write files, make git changes, or run
multi-step loops.

The point: offload the mechanical back-and-forth (renames, type hints,
repetitive refactors) to a model running on your own hardware, so the
planner model (Claude in Claude Code) doesn't burn API tokens on work
a 32B coder can do locally.

## Status

- **Phase A**: one tool, advisory, one-shot per file. Done.
- **Phase B**: queue, model handoff, worktree, test gate, draft PR,
  status tools, systemd packaging, a documented first real run against
  a real target repo, and
  [grouping jobs into one PR](#grouping-jobs-into-one-pr) via a shared
  `group_id`. Done. See [First real run](#first-real-run).
  See `MILESTONES.md` for the slice plan.
- **Phase C**: standing task feed from GitHub issues — labeled issues
  on a configured repo are parsed, enqueued, and closed out with a
  terminal comment. Done. See
  [First issue-driven run](#first-issue-driven-run) for the end-to-end
  validation, [`PHASE_C_DESIGN.md`](./PHASE_C_DESIGN.md) for the
  design.

## First real run

Phase B closeout was an end-to-end validation against
[rae-budget](https://github.com/rae004/rae-budget): a mechanical sweep
adding `-> ResponseReturnValue` to every Flask route handler, done in
three rounds.

### Round 1 — one PR per file

Three draft PRs, all enqueued from a Claude Code session, worker
handled sequentially while the session moved on:

- [rae-budget#236](https://github.com/rae004/rae-budget/pull/236) —
  `backend/app/routes/categories.py`. The first real run. Title was
  the full dense multi-paragraph spec, which blew past GitHub's
  title display. Prompted the `title`/`body` override feature
  ([#20](https://github.com/rae004/rae-local-agent/pull/20)).
- [rae-budget#237](https://github.com/rae004/rae-budget/pull/237) —
  `backend/app/routes/bills.py`. First PR shaped with the new params:
  title `chore(bills): annotate route returns with ResponseReturnValue`,
  body a short reviewer-facing paragraph. The dense spec goes into
  the commit message, not the title.
- [rae-budget#238](https://github.com/rae004/rae-budget/pull/238) —
  `backend/app/routes/bill_templates.py`. Peer to #237, enqueued in
  the same breath — exercises queue serialization against a real
  target.

### Round 2 — three files, one PR

Having three PRs for one conceptual sweep was clearly too much review
surface. [#27](https://github.com/rae004/rae-local-agent/pull/27)
added `group_id`; the first grouped run was:

- [rae-budget#242](https://github.com/rae004/rae-budget/pull/242) —
  `backend/app/routes/{data_management,pay_periods,spending}.py`.
  Three jobs enqueued back-to-back with `group_id="rae-budget-routes"`.
  First job opened the draft PR with a batch-level title; later jobs
  stacked their file-scoped commits on the same branch. One PR, three
  commits. End-to-end validation of [grouping](#grouping-jobs-into-one-pr).

### Historical note on #237 and #238

Both shipped with `skip_tests=true`. The B4 test gate was running
`uv run pytest -q` with the systemd service's inherited uv context,
so `uv` resolved to rae-local-agent's `.venv` instead of the target
repo's and collection errored on the target's `conftest.py`. Tracked
and fixed in
[#22](https://github.com/rae004/rae-local-agent/issues/22) — the test
subprocess now runs with `VIRTUAL_ENV`/`UV_PROJECT_ENVIRONMENT`
scrubbed and the target repo's `.venv` symlinked into the worktree,
so the gate actually resolves against the target.

## First issue-driven run

Phase C closeout — the standing-feed service enabled against
`rae004/rae-local-agent` itself, four scenarios exercising every
completion comment-shape end-to-end:

- **`ok`** —
  [rae-local-agent#46](https://github.com/rae004/rae-local-agent/issues/46)
  opened with a labeled issue, poller enqueued, worker produced
  [PR #47](https://github.com/rae004/rae-local-agent/pull/47) with
  green CI (the issue body chained `uv run ruff format` into
  `test_cmd` to pre-empt formatter red). Poller posted
  `Done — draft PR: …` with a diff preview and labeled
  `agent-complete`.
- **`tests_failed`** —
  [#48](https://github.com/rae004/rae-local-agent/issues/48) with
  `test_cmd: ["sh", "-c", "false"]`. No branch pushed, job landed in
  `jobs/failed/`, poller posted `Tests failed —` with the (empty) log
  head, labeled `agent-complete`.
- **`error:dirty_tree`** —
  [#49](https://github.com/rae004/rae-local-agent/issues/49) with
  `.runbook-dirty` sitting in the target worktree before enqueue.
  Worker aborted with `error:dirty_tree`, poller posted
  `` Job errored: `error:dirty_tree` ``, labeled `agent-complete`.
- **`agent-invalid`** —
  [#50](https://github.com/rae004/rae-local-agent/issues/50) with a
  body that had no frontmatter. Parser rejected, poller posted
  `Could not enqueue this issue: …`, labeled `agent-invalid`. No job
  on disk, no `agent-queued`, no `agent-complete`.

All four landed with zero intervention between opening the issue and
the terminal label landing. The adopt + completion passes handled
every shape of worker outcome correctly.

### Operating the service

Prereqs on each repo in `repos.yaml`:

```bash
for L in local-agent agent-queued agent-invalid agent-complete; do
  gh label create "$L" --repo <owner>/<repo>
done
```

Enable the service once labels + config are in place:

```bash
systemctl --user daemon-reload
systemctl --user enable --now rae-local-agent-issue-poller
journalctl --user -u rae-local-agent-issue-poller -f
```

To retry a rejected issue: fix the body, remove `agent-invalid`,
leave `local-agent`. The next tick re-adopts.

## Requirements

- Python ≥ 3.12
- [`uv`](https://docs.astral.sh/uv/)
- An [Ollama](https://ollama.com) server reachable over HTTP, with a
  coder model pulled. The defaults assume `qwen2.5-coder:32b` on
  `localhost:11434`. Any model that follows plain system/user prompts
  works; smaller or less capable models will skip more cases.

## Install

```bash
git clone <this repo>
cd local-agent
uv sync
```

## Configure

Environment variables, all optional:

| Variable | Default | What |
|----------|---------|------|
| `LOCAL_AGENT_OLLAMA_HOST` | `http://localhost:11434` | Base URL of the Ollama server (no path) |
| `LOCAL_AGENT_MODEL` | `qwen2.5-coder:32b` | Ollama model tag to call |

Wire into Claude Code (user-global, `~/.claude.json`):

```json
{
  "mcpServers": {
    "local-agent": {
      "command": "uv",
      "args": ["--directory", "/path/to/local-agent", "run", "python", "server.py"],
      "env": {
        "LOCAL_AGENT_OLLAMA_HOST": "http://workhorse.lan:11434",
        "LOCAL_AGENT_MODEL": "qwen2.5-coder:32b"
      }
    }
  }
}
```

Restart Claude Code to pick up the new MCP server.

## Tools

### `apply_transformation(spec, scope, repo_root) -> unified diff`

- `spec` — tight description of the mechanical operation.
- `scope` — list of repo-relative file paths to transform.
- `repo_root` — absolute path; `scope` paths resolve against this.

**Good fits:** adding type hints, renaming across files, extending a
test pattern, mechanical refactors with a clear before/after shape.

**Bad fits:** anything requiring design judgment, multi-step reasoning,
or exploration. Keep those for the planner.

### Example (from a Claude Code session)

> Use local-agent apply_transformation on `src/foo/bar.py`:
> "Rename the private attribute `_stoi` to `_char_to_id` throughout. Do
> not change the class name, public API, or any comments/docstrings."

Claude Code will call the tool, show you the returned diff, and apply
it with its own `Edit` tool if correct.

### `enqueue_job(spec, scope, repo_root, base_branch?, test_cmd?, skip_tests?, title?, body?, group_id?) -> job id`

Fire-and-forget variant. The background worker picks the job up, runs the
transformation, applies the diff in an isolated worktree, runs the target
repo's test command (default `uv run pytest -q`), and on green opens a
GitHub draft PR assigned to you. Use this when the result doesn't need to
be reviewed inline — e.g. a type-hint sweep that can come back as a draft
PR during the next review window.

- `base_branch` — branch to fork off, default `main`.
- `test_cmd` — argv run inside the worktree between apply and commit;
  non-zero exit routes the job to `jobs/failed/<id>/` with test output
  in `log.txt`, no branch pushed.
- `skip_tests` — bypass the test gate for intentional-behavior-change jobs.
- `title`, `body` — shape the commit subject and draft-PR fields
  separately from `spec`. Omit → derived from the first line of `spec`
  (truncated to 72 chars) and the full spec text respectively.
- `group_id` — optional group handle (`^[a-z0-9][a-z0-9-]{0,62}$`). See
  [Grouping jobs into one PR](#grouping-jobs-into-one-pr).

Pre-flight: if the target repo's working tree is dirty, the worker
refuses with status `error:dirty_tree` — don't interfere with in-flight
work there.

#### Grouping jobs into one PR

Passing the same `group_id` across a batch of related jobs lands them
as sequential commits on one `rae-local-agent/<group_id>` branch, with
one draft PR covering the batch instead of one PR per file.

The worker's routing per job:

- No `group_id` set → classic Phase B shape: branch is
  `rae-local-agent/<job-id>`, one PR per job.
- `group_id` set, branch not on origin yet → **first job of the group**.
  Branches off `base_branch`, opens the draft PR with this job's
  `title`/`body`.
- `group_id` set, branch already on origin → **later job of the group**.
  Bases the new commit on the existing branch, pushes, and surfaces
  the already-open PR's URL. The PR title/body is NOT re-shaped — the
  first job in a group owns the review surface; later jobs are just
  commits on it.

The individual job's `title`/`body` still shape its commit — only the
PR header is first-job-wins. Compose a `body` on the first job that
reads as a "batch description" when you intend to grow it.

Example: an N-file mechanical sweep, from one Claude Code session —

> Enqueue three jobs for me on local-agent, same `group_id` of
> `routes-sweep`:
> - `backend/app/routes/bills.py`, title `chore(bills): …`
> - `backend/app/routes/categories.py`, title `chore(categories): …`
> - `backend/app/routes/spending.py`, title `chore(spending): …`

Result: one draft PR named after the first job, three commits on it.

Artifacts land per-job under `jobs/`:

```
jobs/done/<id>/
    job.json       # the original record
    diff.patch     # the model-produced diff
    status.txt     # ok | model_declined | error:<msg>
    branch.txt     # pushed branch name (ok)
    pr_url.txt     # draft PR URL (ok)

jobs/failed/<id>/
    job.json
    diff.patch     # the diff that didn't pass tests
    status.txt     # tests_failed
    log.txt        # combined stdout+stderr of the test command
```

### `get_job_status(job_id) -> dict`

Look up a single job across `pending/running/done/failed`. Returns
`{id, state, status, spec, created_at, diff, log, branch, pr_url}`.
Artifact fields are empty strings while the job is pending/running.
Raises if the id doesn't exist.

### `list_jobs(state?) -> list[dict]`

Enumerate jobs, newest first. Each entry: `{id, state, status,
created_at, spec_summary}`. Optional `state` filter: one of `pending`,
`running`, `done`, `failed`.

## Running the worker

The worker drives the queue — `enqueue_job` only writes a record to
`jobs/pending/`. Nothing happens until a worker is running.

### Development: foreground

```bash
uv run python worker.py
```

It polls `jobs/pending/`, moves each job through `running/` into
`done/<id>/` or `failed/<id>/`, and logs via stdlib `logging`.
Ctrl+C stops it cleanly.

### Production: systemd user service

The repo ships a unit at `systemd/rae-local-agent-worker.service`.
Install with a symlink so a `git pull` on this repo also picks up unit
changes:

```bash
# Optional: config file for env vars (same shape as the MCP `env` block).
mkdir -p ~/.config/rae-local-agent
cat > ~/.config/rae-local-agent/env <<'EOF'
LOCAL_AGENT_OLLAMA_HOST=http://workhorse.lan:11434
LOCAL_AGENT_MODEL=qwen2.5-coder:32b
EOF

# Symlink the unit into the user systemd dir.
mkdir -p ~/.config/systemd/user
ln -s ~/projects/rae-local-agent/systemd/rae-local-agent-worker.service \
      ~/.config/systemd/user/rae-local-agent-worker.service

systemctl --user daemon-reload
systemctl --user enable --now rae-local-agent-worker
```

Verify it's healthy and tail logs:

```bash
systemctl --user status rae-local-agent-worker
journalctl --user -u rae-local-agent-worker -f
```

If you cloned somewhere other than `~/projects/rae-local-agent`,
override the paths with a drop-in rather than editing the unit:

```bash
systemctl --user edit rae-local-agent-worker
# add:
# [Service]
# WorkingDirectory=/your/path/rae-local-agent
# ExecStart=
# ExecStart=/your/path/to/uv run python worker.py
```

After editing the unit or dropping in overrides:

```bash
systemctl --user daemon-reload
systemctl --user restart rae-local-agent-worker
```

### Phase C issue poller

The repo ships `systemd/rae-local-agent-issue-poller.service`. As of
C4, every `poll_interval_seconds` the poller does two passes per repo:

**Adopt pass** — issues matching
`label:local-agent -label:agent-queued -label:agent-invalid`:

1. Parses the YAML frontmatter + markdown prose from the issue body
   (format in `PHASE_C_DESIGN.md` § "Issue → job translation").
2. On parse success: adds `agent-queued`, writes the job to
   `jobs/pending/`, posts a comment with the job id. The worker
   picks the job up and opens a draft PR.
3. On parse / enqueue failure: adds `agent-invalid`, posts a comment
   with the error. Remove `agent-invalid` and fix the body to retry.

**Completion pass** — issues matching
`label:agent-queued -label:agent-complete`:

1. Pulls the job id from the earlier `agent-queued` comment.
2. If the job is still pending/running, waits for a future tick.
3. If the job reached `done`/`failed`, posts a terminal comment
   (shape per status: `ok` with PR URL + diff preview, `tests_failed`
   with first 20 log lines, `model_declined` with a short note,
   `error:...` verbatim) and adds `agent-complete`.

#### Required labels

Each repo listed in `repos.yaml` must have these four labels
created on it (`gh label create` or GitHub UI):

- `local-agent` — the trigger; **the only one a human sets**.
- `agent-queued` — poller sets this after enqueueing.
- `agent-invalid` — poller sets this when parse or enqueue failed.
- `agent-complete` — poller sets this after posting the terminal
  comment with the result.

#### Config

`~/.config/rae-local-agent/repos.yaml` — format in
`PHASE_C_DESIGN.md` § "Configuration". Minimal example:

```yaml
poll_interval_seconds: 300  # optional, default 300
repos:
  rae004/rae-budget: /home/rae004/projects/rae-budget
```

#### Dry-run / enable

```bash
uv run python issue_poller.py                # interactive foreground
systemctl --user daemon-reload
systemctl --user enable --now rae-local-agent-issue-poller
journalctl --user -u rae-local-agent-issue-poller -f
```

A missing or invalid config exits non-zero with an error line naming
the offending field. A per-repo `gh` failure (auth, network) logs a
`skipping <repo> this tick` warning and the loop keeps running — one
bad repo doesn't take down the others.

## Why the rewrite protocol (not the diff protocol)

The first cut had the model emit a unified diff directly. It failed on
every real test:

- Hunk headers miscounted (`@@ -18,8 +18,8 @@` on a 9-line hunk)
- Phantom no-op hunks (`-foo` / `+foo` with identical content)
- Context lines showing post-rename state (bad fuzz)
- Blank context lines missing the required leading space

LLMs are bad at the bookkeeping unified diff demands. Pushing the diff
math to Python (`difflib.unified_diff`) makes the output
correct-by-construction. The model does what it's good at — rewriting
text — and deterministic code does the math.

See `server.py`'s module docstring for the long version.

## Limitations

- **One file at a time.** Multi-file scope works, but each file is a
  separate model call. For large scopes, consider splitting across
  conversations.
- **Byte cap: 100 KB per file.** Raise `MAX_FILE_BYTES` in `server.py`
  if you need bigger scopes; mind your model's context window.
- **No streaming.** The whole file is sent in one request and the whole
  rewrite comes back in one response. A 400-line file on a 32B model
  via Vulkan takes a few minutes.
- **Spec authoring matters.** The worker interprets specs literally.
  Spell out which cases to touch and which to leave alone.

## Development

Lint, format, and test — the same three checks CI runs on every PR:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

To auto-fix: `uv run ruff check --fix .` and `uv run ruff format .`.

Commits use [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, `chore:`, …) — release-please reads them to open release PRs
that bump the version in `pyproject.toml` and update `CHANGELOG.md`.

## License

MIT.
