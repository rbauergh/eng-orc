"""ADR-lite decision log.

Agents record every consequential choice (stack, structure, tradeoffs,
assumption promotions) with rationale and confidence. Downstream agents
receive relevant decisions in their briefs so nothing is re-litigated —
this is the direct fix for the v0 PM re-asking answered questions.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from .fsio import append_jsonl, iter_jsonl
from .util import iso_now, new_id


class Decision(BaseModel):
    id: str = Field(default_factory=lambda: new_id("dec"))
    ts: str = Field(default_factory=iso_now)
    title: str
    decision: str
    rationale: str = ""
    alternatives: list[str] = Field(default_factory=list)
    confidence: float = 0.8  # 0..1
    made_by: str = "system"
    item: str | None = None
    tags: list[str] = Field(default_factory=list)
    superseded_by: str | None = None


class DecisionLog:
    def __init__(self, path: Path):
        self.path = path

    def record(self, decision: Decision) -> Decision:
        append_jsonl(self.path, decision.model_dump(exclude_none=True))
        return decision

    def supersede(self, old_id: str, new_decision: Decision) -> Decision:
        self.record(new_decision)
        append_jsonl(
            self.path,
            {"record": "superseded", "id": old_id, "superseded_by": new_decision.id, "ts": iso_now()},
        )
        return new_decision

    def all(self, include_superseded: bool = False) -> list[Decision]:
        decisions: dict[str, Decision] = {}
        for raw in iter_jsonl(self.path):
            if raw.get("record") == "superseded":
                target = decisions.get(raw.get("id", ""))
                if target is not None:
                    target.superseded_by = raw.get("superseded_by")
                continue
            decision = Decision.model_validate(raw)
            decisions[decision.id] = decision
        result = sorted(decisions.values(), key=lambda d: d.ts)
        if include_superseded:
            return result
        return [d for d in result if d.superseded_by is None]

    def render_markdown(self, limit: int | None = None) -> str:
        decisions = self.all()
        if limit is not None:
            decisions = decisions[-limit:]
        if not decisions:
            return "(no decisions recorded)"
        lines = []
        for d in decisions:
            lines.append(f"- **{d.title}** — {d.decision}")
            if d.rationale:
                lines.append(f"  - why: {d.rationale}")
            if d.confidence < 0.7:
                lines.append(f"  - confidence: {d.confidence:.0%} (flag for review)")
        return "\n".join(lines)
