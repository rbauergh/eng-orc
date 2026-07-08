"""Deterministic supervision policy.

Routing between phases and picking the next work item are pure functions of
on-disk state — never an LLM call. v0 burned tokens asking an 8B model
"should we retry?"; here judgment-free decisions are code, and models are
spent only where judgment lives (charters, designs, code, reviews).
"""

from __future__ import annotations

from ..plan import Plan, WorkItem
from ..project import Project


def next_phase(project: Project) -> str:
    """Reconstruct the correct phase from disk facts; the stored phase is a
    hint, not the truth (files may have been edited or half-written)."""
    meta = project.meta
    # a queued change request outranks "done": a living codebase is never
    # finished, only quiet — but the definition phases must exist first
    if project.design_path.exists() and project.pending_requests():
        return "request"
    if meta.phase == "done":
        return "done"
    if meta.external_workroom and not project.artifacts.exists("codebase-report.md"):
        return "scout"
    charter = project.charter()
    if charter is None:
        return "charter"
    # Only the charterer's own questions route back through a charter revision;
    # answers to supervisor/implementer questions are consumed where they were
    # asked (stuck-item guidance in build, item briefs during attempts).
    if any(g.from_role == "charterer" for g in project.unconsumed_answers()):
        return "charter"
    if not charter.get("ready_to_build", False):
        return "charter"
    if not project.design_path.exists():
        return "design"
    plan = project.load_plan()
    if not plan.items:
        return "plan"
    if plan.is_complete():
        return "wrap"
    return "build"


INTERRUPTED_NOTE = ("an attempt was interrupted mid-run (orc stopped); partial "
                    "changes may remain in the workroom — check before redoing work")


def cleanup_dangling_attempts(plan: Plan) -> bool:
    """A stop mid-attempt (Ctrl-C, crash) leaves an attempt with no outcome.
    That is not a model failure: the record is removed so it never consumes
    attempt budget, and a note warns the next attempt that partial changes
    may already sit in the workroom."""
    changed = False
    for item in plan.items:
        kept = [a for a in item.attempts if a.outcome is not None or a.ended is not None]
        if len(kept) != len(item.attempts):
            item.attempts = kept
            if not item.notes or item.notes[-1] != INTERRUPTED_NOTE:
                item.notes.append(INTERRUPTED_NOTE)
            item.touch()
            changed = True
        if item.status == "in_progress":
            item.status = "todo"
            item.touch()
            changed = True
    return changed


def pick_item(plan: Plan, max_attempts: int) -> WorkItem | None:
    """Highest-priority ready item that still has attempt budget."""
    for item in plan.ready_items():
        if item.open_attempt_count() < max_attempts:
            return item
    return None


def exhausted_items(plan: Plan, max_attempts: int) -> list[WorkItem]:
    return [
        item
        for item in plan.items
        if item.status not in ("done", "dropped") and item.open_attempt_count() >= max_attempts
    ]


def needs_tester_first(item: WorkItem) -> bool:
    if not getattr(item, "test_first", False):
        return False
    return not any(a.role == "tester" and a.outcome == "success" for a in item.attempts)


def charterer_questions_asked(project: Project) -> int:
    return sum(1 for gate in project.gates.all() if gate.from_role == "charterer")
