"""The project plan: a DAG of work items persisted as human-editable plan.yaml.

Work items are the unit of scheduling, attempts, verification, and review.
Each carries machine-checkable acceptance (verify_commands must exit 0) plus
human-readable acceptance criteria the reviewer holds the diff against.
The user may edit plan.yaml by hand between runs; `Plan.validate_graph`
guards the invariants (unique ids, existing deps, acyclic).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .fsio import atomic_write_yaml, read_yaml
from .util import iso_now, new_id

ItemStatus = Literal["todo", "in_progress", "blocked", "review", "done", "failed", "dropped"]
ItemKind = Literal["feature", "fix", "refactor", "test", "docs", "chore", "investigate", "integrate"]
ItemSize = Literal["S", "M", "L"]

TERMINAL_STATUSES: tuple[str, ...] = ("done", "dropped")
OPEN_STATUSES: tuple[str, ...] = ("todo", "in_progress", "blocked", "review", "failed")


class AttemptRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("att"))
    role: str = "implementer"
    model: str = ""
    started: str = Field(default_factory=iso_now)
    ended: str | None = None
    outcome: Literal["success", "fail", "stuck", "error"] | None = None
    summary: str = ""
    test_summary: str = ""
    transcript: str | None = None  # artifact-relative path
    base_sha: str = ""  # workroom HEAD when the attempt started (review diffs)
    tokens_in: int = 0
    tokens_out: int = 0


class WorkItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("wi"))
    title: str
    kind: ItemKind = "feature"
    description: str = ""
    acceptance: list[str] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    files_hint: list[str] = Field(default_factory=list)
    status: ItemStatus = "todo"
    priority: int = 3  # 1 (urgent) .. 5 (someday)
    size: ItemSize = "M"
    attempts: list[AttemptRecord] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    created: str = Field(default_factory=iso_now)
    updated: str = Field(default_factory=iso_now)

    def touch(self) -> None:
        self.updated = iso_now()

    def open_attempt_count(self) -> int:
        return sum(1 for a in self.attempts if a.outcome in ("fail", "stuck", "error"))

    def attempt_label(self, max_attempts: int) -> str:
        """Budget counter for a starting/in-flight attempt. Only failed
        attempts consume the per-item budget — successes and in-flight
        records don't (mirrors pick_item/exhausted_items) — so the label
        numbers this swing against the budget actually remaining."""
        swing = min(self.open_attempt_count() + 1, max_attempts)
        return f"attempt {swing}/{max_attempts}"

    def last_attempt(self) -> AttemptRecord | None:
        return self.attempts[-1] if self.attempts else None


class Plan(BaseModel):
    version: int = 1
    goal_recap: str = ""
    items: list[WorkItem] = Field(default_factory=list)

    # -- lookup ------------------------------------------------------------
    def by_id(self) -> dict[str, WorkItem]:
        return {item.id: item for item in self.items}

    def get(self, item_id: str) -> WorkItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(f"work item not found: {item_id}")

    # -- graph -------------------------------------------------------------
    def validate_graph(self) -> list[str]:
        """Returns a list of problems; empty means the plan is sound."""
        problems: list[str] = []
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            seen: set[str] = set()
            dupes = {i for i in ids if i in seen or seen.add(i)}  # type: ignore[func-returns-value]
            problems.append(f"duplicate item ids: {sorted(dupes)}")
        known = set(ids)
        for item in self.items:
            for dep in item.depends_on:
                if dep == item.id:
                    problems.append(f"{item.id} depends on itself")
                elif dep not in known:
                    problems.append(f"{item.id} depends on unknown item {dep}")
        if self._has_cycle():
            problems.append("dependency graph contains a cycle")
        return problems

    def _has_cycle(self) -> bool:
        indegree = {item.id: 0 for item in self.items}
        dependents: dict[str, list[str]] = {item.id: [] for item in self.items}
        for item in self.items:
            for dep in item.depends_on:
                if dep in indegree:
                    indegree[item.id] += 1
                    dependents[dep].append(item.id)
        queue = [iid for iid, deg in indegree.items() if deg == 0]
        visited = 0
        while queue:
            node = queue.pop()
            visited += 1
            for nxt in dependents[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        return visited != len(self.items)

    def deps_satisfied(self, item: WorkItem) -> bool:
        """Terminal dependencies satisfy: a DROPPED dep was judged unnecessary,
        which must not freeze its dependents forever."""
        index = self.by_id()
        return all(
            index[d].status in TERMINAL_STATUSES for d in item.depends_on if d in index
        )

    def ready_items(self) -> list[WorkItem]:
        """Items eligible to be worked right now, stable-sorted by priority then age."""
        ready = [
            item
            for item in self.items
            if item.status in ("todo", "failed") and self.deps_satisfied(item)
        ]
        return sorted(ready, key=lambda i: (i.priority, i.created, i.id))

    def items_in_status(self, *statuses: str) -> list[WorkItem]:
        return [item for item in self.items if item.status in statuses]

    def progress(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        counts["total"] = len(self.items)
        return counts

    def is_complete(self) -> bool:
        return bool(self.items) and all(item.status in TERMINAL_STATUSES for item in self.items)

    # -- mutation ----------------------------------------------------------
    def upsert(self, item: WorkItem) -> None:
        for idx, existing in enumerate(self.items):
            if existing.id == item.id:
                item.touch()
                self.items[idx] = item
                return
        self.items.append(item)

    def set_status(self, item_id: str, status: ItemStatus) -> WorkItem:
        item = self.get(item_id)
        item.status = status
        item.touch()
        return item

    def add_items(self, items: Iterable[WorkItem]) -> None:
        for item in items:
            self.upsert(item)


def load_plan(path: Path) -> Plan:
    data = read_yaml(path, default=None)
    if data is None:
        return Plan()
    return Plan.model_validate(data)


def save_plan(path: Path, plan: Plan) -> None:
    atomic_write_yaml(path, plan.model_dump(exclude_none=True))
