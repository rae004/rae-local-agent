"""Local-agent MCP server (Phase A, rewrite protocol).

One tool: `apply_transformation(spec, scope, repo_root)`. For each file in
`scope`, hands the file + `spec` to a local Ollama coder model and asks for
the REWRITTEN file. The server computes a unified diff client-side via
`difflib`, so the diff is correct-by-construction: right line counts, right
context, right whitespace.

Why rewrite, not diff: a 32B worker is reliable at text transformation and
unreliable at the bookkeeping the unified-diff format demands (line counts,
context lines, blank-context whitespace, no-op hunks). Shifting the diff
math to Python eliminates all three failure modes seen in the first Phase A
tests. See the compacted-session notes for the specific pathologies.

Design intent (unchanged from the diff-protocol version):
- No file writes, no git, no multi-step loops. Phase A is advisory.
- Caller (Claude Code) reviews the returned diff and applies it with its
  own tools if correct.
- One shot per file. If the model returns the file byte-identical to the
  input (null case), that file contributes nothing to the final diff.
- Multi-file scope: one call per file, concatenated. A failure on one file
  does not poison the others.

Shape of the escalation path, deliberately not in this phase:
- Phase B: job queue, git branching, PR creation
- Phase C: standing task feed from GitHub issues
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Annotated

import httpx
from mcp.server.mcpserver import MCPServer
from pydantic import Field

# --- config ---------------------------------------------------------------
# Overridable via env vars so the server runs for anyone, not just me.
#   LOCAL_AGENT_OLLAMA_HOST  base URL of the Ollama server (no path)
#   LOCAL_AGENT_MODEL        Ollama model tag to call

_OLLAMA_HOST = os.environ.get("LOCAL_AGENT_OLLAMA_HOST", "http://localhost:11434")
OLLAMA_URL = f"{_OLLAMA_HOST.rstrip('/')}/api/generate"
MODEL = os.environ.get("LOCAL_AGENT_MODEL", "qwen2.5-coder:32b")
TIMEOUT_SECONDS = 600
MAX_FILE_BYTES = 100_000

SYSTEM_PROMPT = """You are a code transformation worker. Given a transformation \
spec and the current contents of a file, produce the REWRITTEN contents of that \
file after applying the transformation.

Rules:
- Return ONLY the full file contents, as if you were overwriting the file.
- No prose, no explanation, no markdown fences, no `FILE:` labels, no diff.
- Preserve every line that isn't part of the transformation exactly as given, \
including whitespace, blank lines, imports, docstrings, and comments.
- If the spec has nothing to do in this file, return the file UNCHANGED \
(byte-for-byte identical to the input).
"""

# --- MCP server + tool ----------------------------------------------------

mcp = MCPServer("local-agent")


def _read_scoped_file(root: Path, rel: str) -> str:
    """Read a file inside the repo root. Enforce size cap and path containment."""
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError as e:
        raise ValueError(f"path escapes repo_root: {rel!r}") from e
    content = p.read_text(encoding="utf-8")
    size = len(content.encode("utf-8"))
    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"{rel} is {size} bytes; MAX_FILE_BYTES={MAX_FILE_BYTES}. "
            "Split the scope or raise the cap."
        )
    return content


def _build_user_prompt(spec: str, rel: str, content: str) -> str:
    return (
        f"TRANSFORMATION SPEC:\n{spec}\n\n"
        f"FILE: {rel}\n```\n{content}\n```\n\n"
        "Return the full file contents, rewritten."
    )


def _strip_fences(raw: str) -> str:
    """Strip markdown fences and a leading `FILE:` label if the model
    emitted them despite being told not to."""
    s = raw.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines)
    first_nl = s.find("\n")
    if first_nl != -1 and s[:first_nl].strip().startswith("FILE:"):
        s = s[first_nl + 1 :]
    return s


def _diff(rel: str, original: str, rewritten: str) -> str:
    """Compute a unified diff; empty string if no changes."""
    orig = original.splitlines(keepends=True)
    new = rewritten.splitlines(keepends=True)
    # Normalise trailing newline so a model that drops it doesn't produce
    # a "\ No newline at end of file" false diff.
    if orig and not orig[-1].endswith("\n"):
        orig[-1] += "\n"
    if new and not new[-1].endswith("\n"):
        new[-1] += "\n"
    return "".join(
        difflib.unified_diff(orig, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3)
    )


async def _rewrite_one(
    client: httpx.AsyncClient, spec: str, rel: str, content: str
) -> str:
    response = await client.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "system": SYSTEM_PROMPT,
            "prompt": _build_user_prompt(spec, rel, content),
            "stream": False,
            "options": {"temperature": 0.1},
        },
    )
    response.raise_for_status()
    return _strip_fences(response.json()["response"])


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
                "Absolute path to the repository root. `scope` paths are "
                "resolved against this."
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
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"repo_root is not a directory: {repo_root}")

    diffs: list[str] = []
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for rel in scope:
            original = _read_scoped_file(root, rel)
            rewritten = await _rewrite_one(client, spec, rel, original)
            file_diff = _diff(rel, original, rewritten)
            if file_diff:
                diffs.append(file_diff)
    return "".join(diffs)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
