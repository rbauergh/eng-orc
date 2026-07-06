"""Long-term memory records.

These outlive projects: lessons learned, conventions the user cares about,
postmortems, and one project card per finished mission. They are recalled
into future briefs so new work starts with the organization's accumulated
judgment instead of a blank slate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..util import iso_now, new_id

MemoryKind = Literal["lesson", "convention", "postmortem", "project_card", "decision", "note"]


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    ts: str = Field(default_factory=iso_now)
    kind: MemoryKind = "lesson"
    project: str = ""  # slug; empty = global
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    importance: int = 3  # 1 (critical) .. 5 (minor)

    def render_passage(self) -> str:
        """The canonical text stored in semantic memory backends."""
        scope = self.project or "global"
        return f"[{self.kind} | {scope}] {self.title}\n\n{self.body}"

    def passage_tags(self) -> list[str]:
        tags = [f"kind:{self.kind}"]
        if self.project:
            tags.append(f"project:{self.project}")
        tags.extend(self.tags)
        return tags


class MemoryHit(BaseModel):
    item: MemoryItem
    score: float = 0.0
    backend: str = "local"
