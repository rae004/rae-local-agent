# Phase C — Standing feed from GitHub issues

Design doc, not an implementation. Milestone shape lives in
`MILESTONES.md`; this doc exists so the shape of the loop is decided
before anyone writes a line of it.

## Context

Phase B's `enqueue_job` is a human-initiated surface — a Claude Code
session calls it. That gets us "do this thing while I'm doing another
thing", but it still needs a human in the loop to type.

Phase C's point is autonomy: the local agent should pick up work when
nobody is at the keyboard. The B worker already handles the pipeline
from `enqueue_job` onward (model → diff → apply → test → push →
draft PR). Phase C just changes *who* enqueues.

Chosen source: **GitHub issues**, labeled explicitly. Rationale: issues
are already how we track work; adding a label is cheap; the review
surface stays on GitHub where humans already look.

## Decided

| # | Question | Pick | Rejected alternative |
|---|---|---|---|
| 1 | Trigger | Polling | Webhooks (needs public endpoint; LAN-only project) |
| 2 | Source | Issues with label `local-agent` | Comment triggers like `/local-agent` on issues (adds state tracking) |
| 3 | Issue → job | YAML frontmatter + markdown prose in body | Labels + title conventions (too limited for `scope`) |
| 4 | Idempotency | GitHub labels as state (`local-agent` + `agent-queued`) | Local `issues_seen.json` (fragile across restarts) |
| 5 | Deployment | Second systemd user service, separate from worker | Fold polling into worker (couples cadences and failure modes) |
| 6 | Repo → local-path | `~/.config/rae-local-agent/repos.yaml` | Env vars or auto-discovery |
| 7 | Error path | Comment on issue + `agent-invalid` label | Silent skip |
| 8 | Comment-back on completion | Yes — poster gets a comment with the PR link | No comment-back; posters watch GitHub themselves |

## The loop

The poller is a long-running process doing one thing per tick:

```
for repo in config.repos:
    for issue in gh.list_issues(repo, label="local-agent", -label="agent-queued", -label="agent-invalid"):
        try:
            record = parse_issue_body(issue.body)
        except InvalidIssue as e:
            gh.comment(issue, f"Could not parse this issue: {e}. Fix and remove `agent-invalid`.")
            gh.add_label(issue, "agent-invalid")
            continue

        job_id = enqueue_job(**record, repo_root=config.repos[repo])
        gh.comment(issue, f"Enqueued as `{job_id}`. <!-- rae-local-agent:job_id={job_id} -->")
        gh.add_label(issue, "agent-queued")

for issue in gh.list_issues_with_label("agent-queued", -label="agent-complete"):
    job_id = extract_job_id_from_comments(issue)
    status = get_job_status(job_id)
    if status.state in ("done", "failed"):
        comment_completion(issue, status)
        gh.add_label(issue, "agent-complete")

sleep(POLL_INTERVAL)
```

Two passes per tick: new issues to enqueue, then existing enqueued
issues to close out with a result comment. Order matters — if a job
completes between the two passes, next tick picks it up.

## Issue → job translation

**Issue body format**:

```markdown
---
scope:
  - backend/app/routes/health.py
group_id: null
skip_tests: false
title: "chore(health): annotate route return"
---

Add a return type annotation `-> ResponseReturnValue` to every
function in this file that is decorated with `@health_bp.route(...)`.
Import `ResponseReturnValue` by adding the line `from flask.typing
import ResponseReturnValue` immediately after the existing `from
flask import ...` line. Change nothing else.
```

Mapping to `enqueue_job`:

- `scope`, `group_id`, `skip_tests`, `title`, `body` ← frontmatter
  fields (same names as the MCP tool params)
- `base_branch`, `test_cmd` ← frontmatter fields when present,
  defaults otherwise
- `spec` ← the markdown body after the frontmatter, dedented
- `repo_root` ← from `repos.yaml` (looked up by the GitHub repo)
- Issue title is used as the PR title fallback *only if* `title` is
  omitted; otherwise the frontmatter wins. The issue title stays on
  GitHub as the issue's name.

**Validation at parse time**:

- YAML frontmatter present, well-formed, no unknown top-level keys
- `scope` is a non-empty list of strings
- `group_id` matches the Phase B regex if present
- Spec body is non-empty
- `repo_root` (derived) exists on disk

A missing or malformed frontmatter → `InvalidIssue` → `agent-invalid`.

## State on GitHub

Three labels, applied by the poller, never required from the user:

| Label | Set by | Meaning |
|---|---|---|
| `local-agent` | **The human** | "Please process this." The only label a human sets. |
| `agent-queued` | Poller | "I enqueued this as job `<id>`." |
| `agent-complete` | Poller | "I posted the final result comment." |
| `agent-invalid` | Poller | "I could not parse this. Fix and remove this label." |

An issue's lifecycle:

1. Human opens issue with `local-agent` label.
2. Poller enqueues, comments with job id, adds `agent-queued`.
3. Worker (Phase B) processes the job.
4. Poller sees the job is done, comments the result (PR URL or error),
   adds `agent-complete`.

An issue with both `agent-queued` and `agent-complete` is done.
Removing all three agent-* labels + keeping `local-agent` would cause
re-enqueue (useful for retries after fixing an issue).

## The comment-back on completion

On completion, the poller posts a terminal comment on the issue:

- **Status `ok`**: PR URL + the first ~5 lines of the diff for a quick
  glance (collapsible details for the full diff).
- **Status `tests_failed`**: PR not opened; first ~20 lines of the
  test log in a code fence.
- **Status `model_declined`**: short note that the model returned no
  diff; suggests the spec may be too open-ended.
- **Status starting with `error:`**: the error string verbatim.

The comment carries an HTML comment tag so later ticks don't
double-post: `<!-- rae-local-agent:completion -->`.

## Deployment

A second systemd user service: `rae-local-agent-issue-poller.service`.

- `Type=simple`, `Restart=on-failure`, same unit style as the worker.
- `ExecStart=uv run python issue_poller.py` from the same working
  directory as the worker.
- Reads `~/.config/rae-local-agent/env` for the same env vars
  (Ollama host etc.) plus `LOCAL_AGENT_POLL_INTERVAL`.
- Independent of the worker: a poller crash doesn't take down the
  worker, and vice-versa.

Both services should be enabled together:

```bash
systemctl --user enable --now rae-local-agent-worker
systemctl --user enable --now rae-local-agent-issue-poller
```

## Configuration

`~/.config/rae-local-agent/repos.yaml`. Each repo value is either a
bare path string (back-compat) or a mapping with `path` + optional
`gate_cmds`:

```yaml
poll_interval_seconds: 300  # 5 min
repos:
  rae004/rae-budget:
    path: /home/rae004/projects/rae-budget
    gate_cmds:
      - [uv, run, ruff, format, "."]
      - [uv, run, ruff, check, "."]
      - [uv, run, pytest, "-q"]
  rae004/ai-security-digest:
    path: /home/rae004/projects/ai-security-digest
    gate_cmds:
      - [npm, run, lint:fix]
      - [npm, run, build]
      - [npm, test]
  rae004/legacy-repo: /home/rae004/projects/legacy-repo  # bare-string, default gate
```

- `poll_interval_seconds` — optional, default 300.
- `repos` — map from GitHub `owner/name` to either a bare path string
  (uses the default gate `[["uv", "run", "pytest", "-q"]]`) or a
  mapping carrying `path` and an optional per-repo `gate_cmds`.
- `gate_cmds` — list of commands run sequentially between `git apply`
  and `git commit` in the worker's worktree. First non-zero exit
  fails the gate and prevents push; per-command headers land in
  `log.txt`. Issue frontmatter's `gate_cmds` or `test_cmd` overrides
  the per-repo default when present. Setting both `gate_cmds` and
  `test_cmd` in one place is an error.

## Error handling

- **`gh` call fails** (network, auth): log and skip the tick. Next
  tick retries.
- **Malformed issue body**: `agent-invalid` label + comment with the
  parse error. Human-fixable; agent re-enters the loop when the label
  is removed.
- **Local path from `repos.yaml` doesn't exist**: `agent-invalid`
  label + comment naming the missing path. Agent config problem,
  surfaced to the issue.
- **`enqueue_job` raises**: `agent-invalid` label + comment with the
  error. Same shape as a malformed issue.
- **Worker never completes a job**: the completion comment never
  fires. The job stays in `agent-queued` indefinitely. The status
  tool shows the job is stuck. No automatic cleanup — a human can
  remove labels to retry.

## Open questions / out of scope for v1

- **Multiple issues at once**: the poller enqueues sequentially per
  tick; the worker still processes jobs sequentially. For high
  throughput we'd need worker parallelism, which is a Phase B+
  concern.
- **Auth model beyond `gh` CLI**: the poller inherits whatever user
  the service runs as, uses that user's `gh` auth. Fine for a
  single-maintainer setup.
- **Cross-repo groups**: `group_id` only makes sense within a single
  repo (branches are per-repo). If a poll feeds multiple repos, each
  repo gets independent groups.
- **Non-GitHub issue sources**: Jira, Linear, etc. — the loop shape
  is source-agnostic, but the only implemented adapter is GitHub.
  Deliberately out of scope.

## Milestones

See `MILESTONES.md` → Phase C. The slice shape mirrors Phase B's:
each milestone leaves the project in a working, demonstrable state.
