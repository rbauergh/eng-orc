"""File tools: windowed reads, syntax-gated writes, SEARCH/REPLACE edits.

Writes and edits are rejected before landing when they would break python
syntax (the single most effective guardrail from SWE-agent's ablations:
invalid edits compound). Code always arrives via the fenced payload, never
JSON-escaped.
"""

from __future__ import annotations

import ast
import difflib
import re

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
            "payload contained no SEARCH/REPLACE blocks. The ACTION line and the "
            "fenced payload must be in ONE reply — resend BOTH together, exactly like:\n"
            'ACTION: edit_file {"path": "<file>"}\n'
            "```payload\n"
            f"{SEARCH_MARK}\n<exact existing lines>\n{DIVIDER_MARK}\n<replacement lines>\n{REPLACE_MARK}\n"
            "```\n"
            "If you are ADDING new content (appending a class, a test, a section) "
            "rather than modifying existing lines, use write_file with the COMPLETE "
            "new file contents instead — no SEARCH/REPLACE needed."
        )
    return blocks


_LINE_NUMBER_RE = re.compile(r"^\s*\d+\| ?")


def _strip_line_numbers(block: str) -> str:
    """Models sometimes copy SEARCH lines verbatim from read_file's numbered
    output ('42| code') — strip the prefixes when every line carries one."""
    lines = block.splitlines()
    if lines and all(_LINE_NUMBER_RE.match(ln) for ln in lines if ln.strip()):
        return "\n".join(_LINE_NUMBER_RE.sub("", ln) for ln in lines)
    return block


def _fuzzy_locate(text: str, search: str) -> tuple[str | None, str]:
    """Trailing-whitespace-tolerant location of `search`: returns the EXACT
    slice of the file to replace plus a note. Only an unambiguous single
    match counts — anything looser is the model's error to correct, and the
    not-found message shows it the real lines to copy."""
    file_lines = text.splitlines()
    search_lines = search.splitlines()
    n = len(search_lines)
    if not n or n > len(file_lines):
        return None, ""
    rstrip_target = [ln.rstrip() for ln in search_lines]
    hits: list[int] = []
    for i in range(len(file_lines) - n + 1):
        if [ln.rstrip() for ln in file_lines[i:i + n]] == rstrip_target:
            hits.append(i)
    if len(hits) == 1:
        i = hits[0]
        return "\n".join(file_lines[i:i + n]), "matched ignoring trailing whitespace"
    return None, ""


def _closest_region(text: str, search: str) -> str:
    """The best-matching real region of the file, numbered — so the model's
    next SEARCH can copy actual lines instead of guessing again."""
    file_lines = text.splitlines()[:4000]
    search_lines = [ln.strip() for ln in search.splitlines()]
    n = max(1, len(search_lines))
    joined_search = "\n".join(search_lines)
    best_i, best_ratio = -1, 0.0
    for i in range(max(1, len(file_lines) - n + 1)):
        window = "\n".join(ln.strip() for ln in file_lines[i:i + n])
        ratio = difflib.SequenceMatcher(None, joined_search, window).ratio()
        if ratio > best_ratio:
            best_i, best_ratio = i, ratio
    if best_i < 0 or best_ratio < 0.3:
        return ""
    start = max(0, best_i - 1)
    end = min(len(file_lines), best_i + n + 1)
    shown = "\n".join(f"{j + 1}| {file_lines[j]}" for j in range(start, end))
    return (f"\nClosest existing text (lines {start + 1}-{end}, {best_ratio:.0%} similar) — "
            f"copy THESE lines exactly into your SEARCH block:\n{shown}")


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
    notes: list[str] = []
    for search, replace in blocks:
        if not search.strip():
            return ToolResult(ok=False, output="empty SEARCH section; to create a file use write_file")
        # models drift from the real file (paraphrased comments, whitespace,
        # copied line-number prefixes) — degrade gracefully through exact →
        # de-numbered → whitespace-tolerant, applying only unambiguous matches
        search = _strip_line_numbers(search)
        count = text.count(search)
        if count > 1:
            return ToolResult(
                ok=False,
                output=f"SEARCH text appears {count} times in {rel}; add surrounding lines to make it unique.",
            )
        located, note = (search, "") if count == 1 else _fuzzy_locate(text, search)
        if located is None:
            preview = search.splitlines()[0][:80] if search.splitlines() else ""
            return ToolResult(
                ok=False,
                output=(
                    f"SEARCH text not found in {rel} (block {applied + 1}, starts with {preview!r})."
                    + _closest_region(text, search)
                ),
            )
        if note:
            notes.append(note)
        text = text.replace(located, replace, 1)
        applied += 1
    error = _syntax_gate(rel, text)
    if error:
        return ToolResult(ok=False, output=f"{error} — the edit was NOT applied")
    path.write_text(text, encoding="utf-8")
    suffix = f" ({'; '.join(notes)})" if notes else ""
    return ToolResult(
        ok=True,
        output=f"applied {applied} edit block(s) to {rel}{suffix}",
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
