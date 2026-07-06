"""Compaction and digestion: deterministic renderers plus utility-model
summarization with mechanical fallbacks.

The renderers (journal → markdown, events → activity lines) are pure
functions so resume briefs and wrap-up digests exist even with no model
available; the LLM only ever *tightens* what the renderers produce.
"""

from __future__ import annotations

from ..config import RoleModel
from ..events import Event, Journal, Kind
from ..llm.client import LLMClient
from ..util import shorten, truncate_middle

_EVENT_RENDERERS = {
    Kind.PROJECT_CREATED: lambda e: f"project created: {e.payload.get('title', '')}",
    Kind.PHASE_ENTERED: lambda e: f"entered phase {e.payload.get('phase')}",
    Kind.PROJECT_STATE: lambda e: f"state → {e.payload.get('state')} {e.payload.get('reason', '')}".strip(),
    Kind.ATTEMPT_STARTED: lambda e: f"attempt started by {e.actor} on {e.item}",
    Kind.ATTEMPT_FINISHED: lambda e: (
        f"attempt on {e.item}: {e.payload.get('status')} — {shorten(e.payload.get('summary', ''), 140)}"
    ),
    Kind.ITEM_STATUS: lambda e: f"item {e.item} → {e.payload.get('status')}",
    Kind.VERIFY_RUN: lambda e: f"verify on {e.item}: {'PASS' if e.payload.get('passed') else 'FAIL'}",
    Kind.REVIEW: lambda e: (
        f"review on {e.item}: {e.payload.get('verdict')} ({e.payload.get('findings', 0)} findings)"
    ),
    Kind.GATE_OPENED: lambda e: f"asked user: {shorten(e.payload.get('question', ''), 140)}",
    Kind.GATE_ANSWERED: lambda e: f"user answered: {shorten(e.payload.get('answer', ''), 140)}",
    Kind.DECISION: lambda e: f"decision: {shorten(e.payload.get('title', ''), 120)}",
    Kind.COMMIT: lambda e: f"commit {e.payload.get('sha', '')}: {shorten(e.payload.get('message', ''), 100)}",
    Kind.RESUME: lambda e: "project resumed",
    Kind.USER_NOTE: lambda e: f"user note: {shorten(e.payload.get('note', ''), 140)}",
    Kind.ERROR: lambda e: (
        f"error[{e.actor}]: {shorten(e.payload.get('error', ''), 160)}"
        if e.actor != "system" else f"error: {shorten(e.payload.get('error', ''), 160)}"
    ),
}

_NOISE_KINDS = {Kind.AGENT_TURN, Kind.TOOL_CALL, Kind.STRUCTURED_CALL, Kind.STEP, Kind.MODEL_SWAP,
                Kind.INDEX_REFRESH, Kind.MEMORY_RECALLED, Kind.MEMORY_SAVED}


def render_events(events: list[Event]) -> str:
    lines = []
    for event in events:
        if event.kind in _NOISE_KINDS:
            continue
        renderer = _EVENT_RENDERERS.get(event.kind)
        if renderer is None:
            continue
        lines.append(f"- [{event.ts[5:16]}] {renderer(event)}")
    return "\n".join(lines)


def recent_activity(journal: Journal, n: int = 60) -> str:
    return render_events(journal.tail(n * 3)) or "(no notable activity yet)"


def summarize(
    client: LLMClient,
    utility: RoleModel,
    text: str,
    instruction: str,
    max_tokens: int = 500,
) -> str:
    """LLM tightening with a mechanical fallback — this must never raise."""
    if not text.strip():
        return ""
    try:
        result = client.chat(
            utility,
            [
                {"role": "system", "content": (
                    "You compress working notes for another engineer. Keep concrete facts: "
                    "file names, commands, errors, decisions. Drop pleasantries and repetition. "
                    "Output plain markdown bullets, nothing else."
                )},
                {"role": "user", "content": f"{instruction}\n\n{truncate_middle(text, 24000)}"},
            ],
            max_tokens=max_tokens,
            temperature=0.1,
        )
        summary = result.text.strip()
        if summary:
            return summary
    except Exception:
        pass
    return truncate_middle(text, max_tokens * 3)
