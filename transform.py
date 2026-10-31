"""Model-handoff logic shared by the MCP tool and the Phase B worker.

Both `server.apply_transformation` (synchronous MCP tool surface) and
`worker.process` (background queue) call `run_transformation` here. Keeping
it in its own module means the worker doesn't import `server.py` — which
would drag in the MCP machinery for no reason.

What's here: the Ollama call, the scoped file read, the diff computation,
and the small text-wrangling helpers for the model's output. See
`server.py` for the design rationale behind the rewrite protocol.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

import httpx

_OLLAMA_HOST = os.environ.get("LOCAL_AGENT_OLLAMA_HOST", "http://localhost:11434")
OLLAMA_URL = f"{_OLLAMA_HOST.rstrip('/')}/api/generate"
MODEL = os.environ.get("LOCAL_AGENT_MODEL", "qwen2.5-coder:32b")

# Context size sent per request. Ollama's default for this model is 32768,
# which allocates ~8 GiB of KV cache. Combined with the ~18.5 GiB of weights
# that overflows a 24 GiB GPU and forces partial CPU offload, dropping
# throughput to ~3 t/s. This workload sends one file + prompts per call
# (bounded at `MAX_FILE_BYTES = 100_000`, worst case ~25 K tokens), so 16 K
# gives ~2× headroom while shrinking the KV cache to ~4 GiB — model + cache
# fits in VRAM and generation stays on GPU.
OLLAMA_NUM_CTX = 16384

# Max output tokens per request. Catches runaway generation (observed on
# ai-security-digest#144: 60 K+ tokens emitted for one file before the
# 45 min wall-clock timeout fired). The largest legitimate rewrite in the
# current workload (`lib/stacks/orchestration-stack.ts`, ~3540 tokens)
# fits well under this with 2× headroom. 8192 output tokens at the
# observed ~26 t/s is ~5 min — a runaway now fails in minutes, not
# three-quarters of an hour.
OLLAMA_NUM_PREDICT = 8192

# Split timeouts: a short connect bound catches a dead/unreachable Ollama
# server fast, and a long read/write/pool budget lets a legitimately-slow
# generation finish. 45 min is a per-file budget, not a stuck-model
# detector — httpx doesn't offer a stream-level idle timeout, so a model
# producing tokens at any rate will keep the connection alive.
OLLAMA_TIMEOUT = httpx.Timeout(2700.0, connect=10.0)

MAX_FILE_BYTES = 100_000

# Extensions treated as source files during directory expansion. Direct file
# entries in `scope` bypass this filter — this only governs what gets picked
# up when a scope entry is a directory.
_SCOPE_FILE_EXTS = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})

# Directory names pruned during directory expansion.
_SCOPE_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        "cdk.out",
        ".yarn",
    }
)

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


def _expand_scope(root: Path, scope: list[str]) -> list[str]:
    """Expand directory entries in `scope` to the contained source files.

    File entries pass through unchanged. Directory entries are walked
    recursively; files with a suffix in `_SCOPE_FILE_EXTS` are kept, and
    directories in `_SCOPE_SKIP_DIRS` are pruned. Order is deterministic
    (sorted by relative path). Raises `ValueError` for path escape,
    nonexistent entry, or a directory that expands to zero source files.
    """
    expanded: list[str] = []
    for rel in scope:
        p = (root / rel).resolve()
        try:
            p.relative_to(root)
        except ValueError as e:
            raise ValueError(f"path escapes repo_root: {rel!r}") from e
        if p.is_file():
            expanded.append(rel)
            continue
        if not p.is_dir():
            raise ValueError(f"scope entry does not exist: {rel!r}")
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if d not in _SCOPE_SKIP_DIRS]
            dir_rel = Path(dirpath).relative_to(root)
            for fname in filenames:
                if Path(fname).suffix.lower() in _SCOPE_FILE_EXTS:
                    found.append(str(dir_rel / fname))
        if not found:
            raise ValueError(
                f"directory scope {rel!r} expanded to zero source files; "
                f"allowed extensions: {sorted(_SCOPE_FILE_EXTS)}"
            )
        expanded.extend(sorted(found))
    return expanded


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
    return "".join(difflib.unified_diff(orig, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))


async def _rewrite_one(client: httpx.AsyncClient, spec: str, rel: str, content: str) -> str:
    response = await client.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "system": SYSTEM_PROMPT,
            "prompt": _build_user_prompt(spec, rel, content),
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_ctx": OLLAMA_NUM_CTX,
                "num_predict": OLLAMA_NUM_PREDICT,
            },
        },
    )
    response.raise_for_status()
    return _strip_fences(response.json()["response"])


async def run_transformation(spec: str, scope: list[str], repo_root: str) -> str:
    """Apply `spec` to each file in `scope`; return the concatenated unified diff.

    Empty string means every file came back byte-identical (the "model
    declined to change anything" case). Any file-level error aborts the
    whole run — callers wrap this in their own error handling.
    """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"repo_root is not a directory: {repo_root}")

    expanded_scope = _expand_scope(root, scope)

    diffs: list[str] = []
    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
        for rel in expanded_scope:
            original = _read_scoped_file(root, rel)
            rewritten = await _rewrite_one(client, spec, rel, original)
            file_diff = _diff(rel, original, rewritten)
            if file_diff:
                diffs.append(file_diff)
    return "".join(diffs)
