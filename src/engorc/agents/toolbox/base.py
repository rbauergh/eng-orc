"""Tool plumbing for the agent loop.

Design constraints (evidence-based, for small local models):
- few tools, each with one obvious purpose and a short doc line;
- args are a small flat JSON object of scalars ONLY — file contents, patches,
  commands and other free text always travel in the fenced payload, never
  inside JSON strings;
- observations are capped and shaped before they reach the context window.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ...config import Config
from ...events import Journal
from ...util import strip_ansi, truncate_middle

if TYPE_CHECKING:  # pragma: no cover
    from ...project import Project


@dataclass
class ToolContext:
    project: Project
    config: Config
    journal: Journal
    item_id: str | None = None
    role: str = "agent"
    index: object | None = None  # CodebaseIndex when available; duck-typed to avoid heavy imports
    extras: dict = field(default_factory=dict)

    @property
    def workroom(self) -> Path:
        return self.project.workroom

    def jail(self, raw_path: str) -> Path:
        """Resolve a path inside the workroom; reject escapes."""
        candidate = (self.workroom / raw_path).resolve()
        root = self.workroom.resolve()
        if candidate != root and root not in candidate.parents:
            raise PermissionError(f"path escapes the workroom: {raw_path}")
        return candidate


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: dict = field(default_factory=dict)

    def shaped(self, max_chars: int) -> str:
        text = strip_ansi(self.output).strip() or "(no output)"
        return truncate_middle(text, max_chars)


@dataclass
class Tool:
    name: str
    summary: str  # one line shown in the system prompt
    args_doc: str  # e.g. '{"path": "relative/file.py"}' — scalars only
    payload_doc: str  # what belongs in the fenced payload; "" = payload unused
    handler: Callable[[ToolContext, dict, str], ToolResult]
    mutates: bool = False  # used to gate read-only roles

    def run(self, ctx: ToolContext, args: dict, payload: str) -> ToolResult:
        try:
            return self.handler(ctx, args, payload)
        except PermissionError as exc:
            return ToolResult(ok=False, output=f"denied: {exc}")
        except FileNotFoundError as exc:
            return ToolResult(ok=False, output=f"not found: {exc}")
        except Exception as exc:  # a tool bug must never kill the attempt
            return ToolResult(ok=False, output=f"tool error: {type(exc).__name__}: {exc}")


def render_tool_docs(tools: list[Tool]) -> str:
    lines = []
    for tool in tools:
        lines.append(f"- {tool.name} {tool.args_doc}")
        lines.append(f"    {tool.summary}")
        if tool.payload_doc:
            lines.append(f"    payload: {tool.payload_doc}")
    return "\n".join(lines)
