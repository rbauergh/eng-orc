"""File tools: windowed reads, syntax-gated writes, SEARCH/REPLACE edits.

Writes and edits are rejected before landing when they would break python
syntax (the single most effective guardrail from SWE-agent's ablations:
invalid edits compound). Code always arrives via the fenced payload, never
JSON-escaped.
"""

from __future__ import annotations

import ast

from .base import Tool, ToolContext, ToolResult

VIEW_WINDOW = 120
SEARCH_MARK = "<<<<<<< SEARCH"
DIVIDER_MARK = "======="
REPLACE_MARK = ">>>>>>> REPLACE"


def _numbered(text: str, start: int = 1) -> str:
    lines = text.splitlines()
    width = len(str(start + len(lines) - 1))
    return "\n".join(f"{i + start:>{width}}| {line}" for i, line in enumerate(lines))


def _syntax_gate(path_name: str, content: str) -> str | None:
    """Returns an error message when the content must not land."""
    if path_name.endswith((".py", ".pyi")):
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return f"rejected: python syntax error at line {exc.lineno}: {exc.msg}"
    return None


def read_file(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    path = ctx.jail(str(args.get("path", "")))
    if not path.is_file():
        return ToolResult(ok=False, output=f"no such file: {args.get('path')}")
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(args.get("start", 1)))
    end = int(args.get("end", start + VIEW_WINDOW - 1))
    end = min(end, len(lines))
    window = "\n".join(lines[start - 1 : end])
    header = f"{args.get('path')} lines {start}-{end} of {len(lines)}"
    hint = "" if end >= len(lines) else f'\n(more below: read_file {{"path": ..., "start": {end + 1}}})'
    return ToolResult(ok=True, output=f"{header}\n{_numbered(window, start)}{hint}")


def write_file(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    rel = str(args.get("path", ""))
    path = ctx.jail(rel)
    if not payload:
        return ToolResult(ok=False, output=(
            "write_file needs the file content in the fenced payload block, exactly like:\n"
            'ACTION: write_file {"path": "' + (rel or "path/to/file.py") + '"}\n'
            "```payload\n<the complete file content>\n```"
        ))
    error = _syntax_gate(rel, payload)
    if error:
        return ToolResult(ok=False, output=error)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = payload if payload.endswith("\n") else payload + "\n"
    path.write_text(content, encoding="utf-8")
    return ToolResult(
        ok=True,
        output=f"wrote {rel} ({len(content.splitlines())} lines)",
        data={"path": rel, "action": "write"},
    )


def _parse_search_replace(payload: str) -> list[tuple[str, str]] | str:
    blocks: list[tuple[str, str]] = []
    lines = payload.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() != SEARCH_MARK:
            i += 1
            continue
        try:
            divider = next(j for j in range(i + 1, len(lines)) if lines[j].strip() == DIVIDER_MARK)
            closer = next(j for j in range(divider + 1, len(lines)) if lines[j].strip() == REPLACE_MARK)
        except StopIteration:
            return "malformed SEARCH/REPLACE block: missing ======= or >>>>>>> REPLACE"
        blocks.append((
            "\n".join(lines[i + 1 : divider]),
            "\n".join(lines[divider + 1 : closer]),
        ))
        i = closer + 1
    if not blocks:
        return (
            "payload contained no SEARCH/REPLACE blocks; format:\n"
            f"{SEARCH_MARK}\n<exact existing lines>\n{DIVIDER_MARK}\n<replacement lines>\n{REPLACE_MARK}"
        )
    return blocks


def edit_file(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    rel = str(args.get("path", ""))
    path = ctx.jail(rel)
    if not path.is_file():
        return ToolResult(ok=False, output=f"no such file: {rel}")
    blocks = _parse_search_replace(payload)
    if isinstance(blocks, str):
        return ToolResult(ok=False, output=blocks)
    text = path.read_text(encoding="utf-8", errors="replace")
    applied = 0
    for search, replace in blocks:
        if not search.strip():
            return ToolResult(ok=False, output="empty SEARCH section; to create a file use write_file")
        count = text.count(search)
        if count == 0:
            preview = search.splitlines()[0][:80] if search.splitlines() else ""
            return ToolResult(
                ok=False,
                output=(
                    f"SEARCH text not found in {rel} (block {applied + 1}, starts with {preview!r}). "
                    "Read the file again and copy the exact lines, including indentation."
                ),
            )
        if count > 1:
            return ToolResult(
                ok=False,
                output=f"SEARCH text appears {count} times in {rel}; add surrounding lines to make it unique.",
            )
        text = text.replace(search, replace, 1)
        applied += 1
    error = _syntax_gate(rel, text)
    if error:
        return ToolResult(ok=False, output=f"{error} — the edit was NOT applied")
    path.write_text(text, encoding="utf-8")
    return ToolResult(
        ok=True,
        output=f"applied {applied} edit block(s) to {rel}",
        data={"path": rel, "action": "edit"},
    )


def list_dir(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    rel = str(args.get("path", ".") or ".")
    root = ctx.jail(rel)
    if not root.is_dir():
        return ToolResult(ok=False, output=f"no such directory: {rel}")
    ignore = set(ctx.config.index.ignore)
    entries: list[str] = []
    for path in sorted(root.rglob("*")):
        parts = set(path.relative_to(root).parts)
        if parts & ignore:
            continue
        depth = len(path.relative_to(root).parts) - 1
        if depth > 3:
            continue
        marker = "/" if path.is_dir() else ""
        entries.append("  " * depth + path.name + marker)
        if len(entries) >= 200:
            entries.append("… (listing truncated at 200 entries)")
            break
    return ToolResult(ok=True, output=f"{rel}:\n" + "\n".join(entries) if entries else f"{rel}: (empty)")


FS_TOOLS = [
    Tool(
        name="read_file",
        summary="Show a window of a file with line numbers (about 120 lines per call).",
        args_doc='{"path": "rel/path", "start": 1}',
        payload_doc="",
        handler=read_file,
    ),
    Tool(
        name="write_file",
        summary="Create or fully overwrite one file. Prefer edit_file for existing files.",
        args_doc='{"path": "rel/path"}',
        payload_doc="the complete file content, as plain text",
        handler=write_file,
        mutates=True,
    ),
    Tool(
        name="edit_file",
        summary="Apply exact SEARCH/REPLACE blocks to one file.",
        args_doc='{"path": "rel/path"}',
        payload_doc=f"one or more blocks: {SEARCH_MARK} / {DIVIDER_MARK} / {REPLACE_MARK}",
        handler=edit_file,
        mutates=True,
    ),
    Tool(
        name="list_dir",
        summary="Tree listing of a directory (3 levels deep, 200 entries max).",
        args_doc='{"path": "."}',
        payload_doc="",
        handler=list_dir,
    ),
]
