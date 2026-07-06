"""Recall injection: turn stored memory into brief sections agents receive."""

from __future__ import annotations

from ..util import shorten
from .schema import MemoryHit
from .store import CompositeMemory, NullMemory


def recall_markdown(hits: list[MemoryHit], max_body_chars: int = 400) -> str:
    if not hits:
        return ""
    lines = []
    for hit in hits:
        item = hit.item
        scope = item.project or "global"
        lines.append(f"- **{item.title}** ({item.kind}, {scope})")
        if item.body:
            lines.append(f"  {shorten(item.body, max_body_chars)}")
    return "\n".join(lines)


def build_recall_section(
    memory: CompositeMemory | NullMemory,
    query: str,
    k: int = 5,
    project: str | None = None,
    kinds: list[str] | None = None,
) -> str:
    """Best-effort: recall must never block or break the work it serves."""
    try:
        hits = memory.search(query, k=k, kinds=kinds, project=project)
    except Exception:
        return ""
    return recall_markdown(hits)


def standing_context(memory: CompositeMemory | NullMemory) -> str:
    """The always-loaded blocks: who the user is, what conventions they hold."""
    parts = []
    try:
        profile = memory.get_block("user_profile")
        conventions = memory.get_block("engineering_conventions")
    except Exception:
        return ""
    if profile:
        parts.append(f"### User profile\n{profile}")
    if conventions:
        parts.append(f"### Engineering conventions\n{conventions}")
    return "\n\n".join(parts)
