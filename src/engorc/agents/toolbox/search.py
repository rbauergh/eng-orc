"""Search tools: exact grep (ripgrep with a python fallback) and semantic
search over the project index. Hits are capped name-list style — search must
compress, never dump."""

from __future__ import annotations

import re
import shutil
import subprocess

from ...context.files import iter_source_files, read_capped
from .base import Tool, ToolContext, ToolResult

MAX_HITS = 50


def _rg(ctx: ToolContext, pattern: str, glob: str | None) -> list[str] | None:
    rg = shutil.which("rg")
    if rg is None:
        return None
    cmd = [rg, "--line-number", "--no-heading", "--max-count", "3", "--max-columns", "200",
           "--no-messages", "-e", pattern]
    if glob:
        cmd += ["--glob", glob]
    try:
        proc = subprocess.run(cmd, cwd=ctx.workroom, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):  # 1 = no matches
        return None
    return proc.stdout.splitlines()


def _python_grep(ctx: ToolContext, pattern: str) -> list[str]:
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    hits: list[str] = []
    for path in iter_source_files(ctx.workroom, ctx.config.index.ignore, only_indexable=False):
        rel = path.relative_to(ctx.workroom)
        per_file = 0
        for lineno, line in enumerate(read_capped(path).splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{lineno}:{line.strip()[:200]}")
                per_file += 1
                if per_file >= 3:
                    break
        if len(hits) >= MAX_HITS * 2:
            break
    return hits


def grep(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    pattern = str(args.get("pattern", "")) or payload.strip()
    if not pattern:
        return ToolResult(ok=False, output='grep needs {"pattern": "..."}')
    glob = args.get("glob")
    lines = _rg(ctx, pattern, glob)
    if lines is None:
        lines = _python_grep(ctx, pattern)
    if not lines:
        return ToolResult(ok=True, output=f"no matches for {pattern!r}")
    shown = lines[:MAX_HITS]
    suffix = f"\n… {len(lines) - MAX_HITS} more matches (narrow the pattern)" if len(lines) > MAX_HITS else ""
    return ToolResult(ok=True, output="\n".join(shown) + suffix)


def semantic_search(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    query = str(args.get("query", "")) or payload.strip()
    if not query:
        return ToolResult(ok=False, output='search needs {"query": "..."}')
    if ctx.index is None:
        return ToolResult(ok=True, output="semantic index not available here; use grep instead")
    try:
        snippets = ctx.index.search(query)  # type: ignore[attr-defined]
    except Exception as exc:
        return ToolResult(ok=True, output=f"semantic search unavailable ({exc}); use grep instead")
    if not snippets:
        return ToolResult(ok=True, output="no semantic matches; try grep with an identifier")
    parts = []
    for snip in snippets[:5]:
        body = snip.text.strip()
        if len(body) > 900:
            body = body[:900] + "\n…"
        parts.append(f"--- {snip.path} (score {snip.score:.2f}) ---\n{body}")
    return ToolResult(ok=True, output="\n".join(parts))


SEARCH_TOOLS = [
    Tool(
        name="grep",
        summary="Regex search across the project; returns path:line:text hits (max 50).",
        args_doc='{"pattern": "regex", "glob": "*.py"}',
        payload_doc="",
        handler=grep,
    ),
    Tool(
        name="search",
        summary="Semantic search over the indexed codebase for concept-level questions.",
        args_doc='{"query": "how is X configured"}',
        payload_doc="",
        handler=semantic_search,
    ),
]
