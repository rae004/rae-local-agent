# Phase E — Transformation resilience

Design doc, not an implementation. Milestone shape lives in
`MILESTONES.md`; this doc exists so the shape of the resilience
layer is decided before anyone writes a line of it.

## Context

Phases B through D got the pipeline running end-to-end: a GitHub issue
with YAML frontmatter and a label becomes a draft PR after the model
rewrites the scoped files. What's missing is **what happens when a
file-level step misbehaves**.

Today, `transform.py`'s `run_transformation` runs one Ollama call per
file in a serial for-loop, and *any file-level error aborts the whole
run* (`transform.py:115` in the current main). Observed consequences
on `rae004/ai-security-digest#144`:

- File 1 completed successfully in 22 s.
- File 2 hit a `ReadTimeout` after 10 min on the pre-fix timeout cap.
- Files 3–7 never ran. File 1's work was discarded.
- Debugging + re-running the batch cost ~2 hours of wall-clock and 7
  comment cycles on one issue before the first good PR could even be
  attempted.

The failures weren't exotic. For a 7-file batch where each file has
an independent failure probability `P`, the whole-batch success rate
is `(1 - P)^7`:

| P (per-file failure) | Whole-batch success |
|---|---|
| 0.1 | 48% |
| 0.2 | 21% |
| 0.3 | 8% |

Running an off-the-shelf 32 B model served through Vulkan on consumer
hardware, `P` is realistically 0.1–0.3 for a nontrivial task (observed
failure modes so far: fabrication #137/#138, timeout #144 pre-fix,
future: parse errors from fenced output, model refusals, oversized
inputs). The all-or-nothing shape of today's run means **the batch
tool fails most of the time it's used on a real multi-file workload**.

Phase 2 of the sibling model-training-lab project will reduce `P` by
fine-tuning the model, but that doesn't help the architecture: even a
near-perfect model has some non-zero `P`, and the current pipeline
compounds it across every file in scope.

Phase E's point: **the batch tool should deliver partial value when
individual files fail, and should fail fast on inputs it cannot
possibly process.** Not faster inference. Not a smarter prompt. Not a
preview gate. Just: when one file breaks, don't lose the other six.

## Decided

| # | Question | Pick | Rejected alternative |
|---|---|---|---|
| 1 | Granularity of recovery | **Per-file** | Per-run (today's shape; the one being replaced) |
| 2 | Failure taxonomy | **Hard vs. soft errors** — hard aborts the run, soft is logged and skipped | One class (today — everything hard) |
| 3 | Where partial progress lives | **On disk under `jobs/<id>/files/<rel>/`** — one subdir per file with input/output/status | In memory until the run finishes (lost on crash) |
| 4 | What ends up in the PR | **One commit per successful file + a "could not transform" section in the PR body** listing skipped files + reason | Nothing commits unless every file succeeded |
| 5 | Pre-flight sizing | **Token-budget estimate per file; reject before any Ollama call if estimate exceeds `num_ctx` headroom** | Trust `num_ctx` to error at runtime (wastes a full timeout) |
| 6 | Oversized-file chunking | **Deferred until a real file fails pre-flight.** When built: split at top-level declaration (import block / class / exported function). | Build it speculatively now |
| 7 | Retry trigger | **Human removes `agent-complete` to re-run** — the next cycle picks up where it left off because successful files are already on disk | Automatic retry (hides failure cause) |
| 8 | Progress visibility | **Worker log + the "could not transform" PR section only.** No mid-run comments on the issue. | Comment every N files (noisy, zero robustness value) |
| 9 | Parallelism | **Keep serial.** The workhorse Ollama is single-slot (`-np 1`) and VRAM-bound. | Multi-file concurrency (no VRAM budget) |

## Failure taxonomy

Every file-level step emits one of three outcomes:

- **`ok`** — model returned a plausible rewrite; diff was non-empty or
  explicitly empty; committed.
- **`skipped:<reason>`** — the file couldn't be transformed, but the
  batch should continue. Soft error. Logged, listed in the PR body,
  does not block the commit of other files.
- **`error:<msg>`** — the run itself is broken; no further files are
  attempted. Hard error. Behaves like today's abort.

Classification table:

| Condition | Class | Why |
|---|---|---|
| Pre-flight: token estimate exceeds budget | `skipped:too_large` | A `num_ctx`-sized Ollama call cannot succeed; wasting the inference is worse than skipping. Fix is caller-side (split scope). |
| Pre-flight: path escapes `repo_root` | `error:path_escape` | Security guardrail. Never continue. |
| Pre-flight: `MAX_FILE_BYTES` exceeded | `skipped:file_too_large` | Same reasoning as `too_large`. |
| Ollama HTTP 4xx (bad request) | `error:bad_request` | The protocol broke; retrying wastes time. |
| Ollama HTTP 5xx | `skipped:server_error` | Server-side blip; other files may still work on the same server. |
| `httpx.ReadTimeout` | `skipped:timeout` | The 45 min budget was exceeded. One file over budget doesn't poison the batch. |
| Model returned non-UTF-8 or empty | `skipped:bad_response` | Transient model confusion. Log + continue. |
| Diff computed but `git apply --check` fails | `skipped:unapplicable_diff` | Will come up more as scopes grow. Log + continue. |
| Local git state unclean (pre-run guard) | `error:dirty_tree` | Existing behavior in `worker.py:133`. Already an error. Keep. |

The taxonomy is explicit so the worker doesn't have to make judgment
calls at the error site — classification lives in one table in
`transform.py` and the call sites just raise typed exceptions.

## On-disk layout

Each job gets a `files/` subdirectory next to `status.txt`:

```
jobs/running/<job-id>/
├── job.json              # unchanged — the enqueue record
├── log.txt               # unchanged — worker stdout
├── diff.patch            # whole-run concatenated diff (final step)
├── status.txt            # overall: ok | partial | error:<msg>
└── files/
    ├── <rel-path-1>/
    │   ├── original.txt  # input file bytes
    │   ├── rewritten.txt # model output (empty on skip/error)
    │   ├── diff.patch    # per-file unified diff (empty on skip)
    │   └── status.txt    # ok | skipped:<reason> | error:<msg>
    ├── <rel-path-2>/
    │   └── ...
    └── manifest.json     # ordered list of files + their status
```

Guarantees:

- `files/<rel>/original.txt` is written **before** the Ollama call for
  that file, so a crashed/killed worker can be inspected.
- `files/<rel>/status.txt` is the atomic signal — the file's directory
  is complete when `status.txt` exists.
- A re-run that starts with partially-populated `files/` **skips any
  file whose `status.txt` already says `ok`** and re-attempts the rest.

The whole-run `diff.patch` is a concatenation of the per-file diffs
whose status is `ok`. Non-ok files contribute nothing to the diff but
appear in the PR body.

Overall `status.txt` becomes:
- `ok` — every file in scope has `files/<rel>/status.txt == ok`.
- `partial` — at least one file is `ok` and at least one is
  `skipped:*`. The PR still gets created; its body lists the skips.
- `error:<msg>` — a hard error aborted the run before completion.

## Pre-flight sizing

Before the first Ollama call, every file in the expanded scope gets a
token-budget estimate:

```
estimated_tokens = (
    len(system_prompt_bytes) // 2
    + len(user_prompt_boilerplate_bytes) // 2
    + len(spec_bytes) // 2
    + 2 * (len(file_bytes) // 2)   # input + ~equal output
)
```

The `// 2` is a conservative chars-to-tokens ratio for code (dense
with symbols; closer to 2 chars/token than the ~4 of natural
language). The `2 *` doubles the file size to account for the model
rewriting back ~the same amount.

If `estimated_tokens > OLLAMA_NUM_CTX - headroom` (initial headroom:
512), the file is classified `skipped:too_large` **without any Ollama
call**. The PR body includes the estimate and the budget so the
author can see exactly how far over the file was.

The budget is checked against `OLLAMA_NUM_CTX`, not the model's
maximum context, so lowering `num_ctx` to fit VRAM (per the Phase E
precursor PR #61) automatically tightens the pre-flight check too.

## Oversized-file chunking (deferred)

Not built until a real file fails pre-flight. When built, the shape:

- Parse the file (language-aware: `.ts`/`.tsx` via `tsc` or
  `typescript-estree`; `.py` via `ast`) into top-level declarations.
- Group adjacent declarations until each group fits in the token
  budget.
- Rewrite each group independently, with its own Ollama call and its
  own entry in `files/<rel>/chunks/`.
- Reassemble: concatenate rewritten groups in original order.

Risks (why it's deferred):
- Imports and type definitions are referenced across groups.
- The model's "preserve existing formatting outside the annotations
  themselves" rule is harder to enforce when each chunk is a separate
  call.
- `tsc`/`ast` parsing can fail on files with syntax errors.

Build it when a real workload needs it, not before. The pre-flight
`skipped:too_large` signal tells us when that moment arrives.

## Out of scope for v1

- **Preview / dry-run gate.** The PR is already the human-review
  surface. Adding another gate costs human attention and provides no
  robustness win.
- **Mid-run progress comments on the issue.** Noisy. Zero robustness
  value. The log file is enough.
- **Multi-file concurrency.** Workhorse Ollama is `-np 1`,
  VRAM-constrained. Parallelism is a Phase 2 (model-training-lab) or
  future-infra conversation.
- **Automatic retry on soft errors.** The human removes
  `agent-complete` to retry. Keeps failure causes visible.
- **Fancy error classification per model-vendor.** The taxonomy above
  is tied to this worker's known failure modes, not an abstract class
  hierarchy.
- **Caching of successful file rewrites across *different* issues.**
  Each issue is a fresh batch; no cross-issue memoization.

## Milestones

To be filled in once this doc is reviewed. Rough sketch:

- **E-0:** Pre-flight token-budget check — the smallest useful increment
  and the one with the highest value-per-line.
- **E-1:** Per-file on-disk checkpointing + the `files/` layout, no
  behavior change on the happy path (every file still succeeds; the
  files/ dir just gets populated as it goes).
- **E-2:** Soft/hard error taxonomy and the `skipped:*` statuses.
  Worker continues on soft, aborts on hard.
- **E-3:** PR body includes the "could not transform" section when
  the run is `partial`.
- **E-4:** Re-run picks up `ok` files from a prior cycle and only
  re-attempts non-ok ones.
- **E-5:** (deferred — only if an E-0 `skipped:too_large` is hit in
  practice) oversized-file chunking.

## Pointer to the precursor fixes

Three PRs on main precede this phase and are prerequisites that
should land before any E milestone:

- `#58` — directory scope expansion (otherwise E-0's pre-flight has
  nothing to iterate over for directory scopes).
- `#60` — split connect/read timeouts; 45 min per-file budget.
- `#61` — pin `num_ctx: 16384` so inference stays on GPU; the
  pre-flight budget check in E-0 is checked against this constant.
