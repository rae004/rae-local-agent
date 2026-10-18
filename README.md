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
- **Phase B** (in progress): job queue, git branching, draft PR creation.
  Walking skeleton (`enqueue_job` + worker loop, no model/git/PR yet) is
  landed; see `MILESTONES.md` for the slice plan.
- **Phase C** (planned): standing task feed from GitHub issues.

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

### `enqueue_job(spec, scope, repo_root) -> job id` (Phase B, in progress)

Fire-and-forget variant. Enqueues a transformation for a background worker
to run; returns the job id immediately. Use this when the result doesn't
need to be reviewed inline — e.g. a type-hint sweep that can come back as a
draft PR during the next review window.

Current slice (B1) proves the queue mechanic: jobs flow `pending → running
→ done` on disk. Model handoff, git branching, and draft PR creation
arrive in B2–B5 (see `MILESTONES.md`).

Run the worker:

```bash
uv run python worker.py
```

The worker polls `jobs/pending/`, moves each job through the four
directories under `jobs/`, and logs via stdlib `logging`. Later milestones
package it as a systemd user service.

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
