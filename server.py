"""Local-agent MCP server.

Two tools:

- `apply_transformation(spec, scope, repo_root)` — synchronous rewrite
  protocol (Phase A). Calls the local Ollama coder model per file and
  returns the client-computed unified diff. The caller applies the diff.
- `enqueue_job(spec, scope, repo_root)` — fire-and-forget queue surface
  (Phase B). Writes a job record to `jobs/pending/` for the worker to
  pick up asynchronously.

The model-handoff logic itself lives in `transform.py` so the worker can
reuse it without importing the MCP server.

Design intent (Phase A, unchanged):
- No file writes, no git, no multi-step loops.
- One shot per file. If the model returns the file byte-identical to the
  input, that file contributes nothing to the final diff.
- Multi-file scope: one call per file, concatenated.

Shape of the escalation path:
- Phase A: `apply_transformation` (this module).
- Phase B: queue + worker + draft PRs (`worker.py`, MILESTONES.md).
- Phase C: standing task feed from GitHub issues.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

import job
from transform import run_transformation

mcp = MCPServer("local-agent")


@mcp.tool()
async def apply_transformation(
    spec: Annotated[
        str,
        Field(
            description=(
                "Tight description of the mechanical operation. Avoid design "
                "judgment — the local model is a worker, not a planner."
            )
        ),
    ],
    scope: Annotated[
        list[str],
        Field(description="Repo-relative file paths to transform."),
    ],
    repo_root: Annotated[
        str,
        Field(
            description=(
                "Absolute path to the repository root. `scope` paths are resolved against this."
            )
        ),
    ],
) -> str:
    """Hand a bounded mechanical code transformation to a local 32B coder.

    For each file in `scope`, asks the model to rewrite it per `spec`, then
    computes a unified diff client-side via `difflib`. The diff is
    correct-by-construction (right line counts, right context, right
    whitespace). Returns the concatenated diff across all files, or empty
    string if no file changed.

    Does NOT write files or make git changes — the caller applies the diff
    with its own tools if correct.

    Good fits: adding type hints, renaming across files, extending a test
    pattern, mechanical refactors with a clear before/after shape.

    Bad fits: anything requiring design judgment, multi-step reasoning, or
    exploration. The caller should keep those for itself.
    """
    return await run_transformation(spec, scope, repo_root)


# --- Phase B: enqueue_job --------------------------------------------------
# Fire-and-forget surface. The worker (worker.py, run as a systemd user
# service) picks the job up from jobs/pending/ asynchronously. See
# MILESTONES.md for the slice shape.


DEFAULT_TEST_CMD = ["uv", "run", "pytest", "-q"]


def _make_job_record(
    spec: str,
    scope: list[str],
    repo_root: str,
    base_branch: str = "main",
    test_cmd: list[str] | None = None,
    skip_tests: bool = False,
) -> dict:
    """Build the on-disk job record. Raises if repo_root is not a directory."""
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"repo_root is not a directory: {repo_root}")
    return {
        "id": job.new_id(),
        "created_at": datetime.now(UTC).isoformat(),
        "spec": spec,
        "scope": scope,
        "repo_root": str(root),
        "base_branch": base_branch,
        "test_cmd": test_cmd if test_cmd is not None else list(DEFAULT_TEST_CMD),
        "skip_tests": skip_tests,
    }


@mcp.tool()
async def enqueue_job(
    spec: Annotated[
        str,
        Field(
            description=(
                "Tight description of the mechanical operation, same shape "
                "as `apply_transformation`. Baked into the PR body when the "
                "worker completes."
            )
        ),
    ],
    scope: Annotated[
        list[str],
        Field(description="Repo-relative file paths to transform."),
    ],
    repo_root: Annotated[
        str,
        Field(
            description=(
                "Absolute path to the target repository root. `scope` paths "
                "resolve against this. The worker branches off `base_branch` "
                "of this repo and pushes a draft PR."
            )
        ),
    ],
    base_branch: Annotated[
        str,
        Field(
            description=(
                "Base branch on the target repo's `origin` to branch off. Defaults to `main`."
            )
        ),
    ] = "main",
    test_cmd: Annotated[
        list[str] | None,
        Field(
            description=(
                "Shell-safe argv the worker runs in the applied worktree after "
                "`git apply` and before `git commit`. Non-zero exit → job moves "
                "to failed/<id>/ with test output in log.txt, branch is NOT "
                "pushed. Defaults to `uv run pytest -q`."
            )
        ),
    ] = None,
    skip_tests: Annotated[
        bool,
        Field(
            description=(
                "Bypass the test gate entirely. Use for intentional-behavior-"
                "change jobs where the current test suite is expected to fail."
            )
        ),
    ] = False,
) -> str:
    """Enqueue a code-transformation job for the background worker.

    Fire-and-forget. Returns the job id immediately; the worker handles the
    rest. Use `get_job_status(id)` (B6) to check on it later, or watch
    GitHub for the draft PR (B5).

    Use this instead of `apply_transformation` when the result doesn't need
    to be reviewed inline — e.g. a type-hint sweep that can wait for a draft
    PR, or a multi-file rename to come back during the next review window.
    """
    record = _make_job_record(
        spec, scope, repo_root, base_branch, test_cmd=test_cmd, skip_tests=skip_tests
    )
    return job.write_pending(record)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
