"""Brief assembly: every agent's entire world, packed by priority.

Agents never see chat history — they see a brief built fresh from disk:
the task, the governing documents, retrieved code context, standing memory,
prior-attempt evidence, and user answers. This is what makes any project
resumable at any time with a fresh effort: the brief IS the rehydration.

Section priorities (1 = sacrificed last):
1 task/mission + acceptance     2 gate answers + review findings
3 governing docs (charter/design excerpts)   4 code context
5 memory recall                 6 activity/attempt history
"""

from __future__ import annotations

import yaml

from ..config import Config
from ..context.summarizer import recent_activity
from ..llm.budget import Section
from ..memory.recall import build_recall_section, standing_context
from ..memory.store import CompositeMemory, NullMemory
from ..plan import Plan, WorkItem
from ..project import Project
from ..util import shorten
from .services import Services


def _mission_section(project: Project) -> Section:
    return Section(name="Mission", text=project.mission(), priority=1, truncate="middle")


def _charter_section(project: Project) -> Section:
    charter = project.charter()
    text = yaml.safe_dump(charter, sort_keys=False, allow_unicode=True) if charter else ""
    return Section(name="Charter", text=text, priority=3)


def _design_section(project: Project, max_priority: int = 3) -> Section:
    text = project.design_path.read_text(encoding="utf-8") if project.design_path.exists() else ""
    return Section(name="Design", text=text, priority=max_priority)


def _decisions_section(project: Project) -> Section:
    return Section(name="Decisions so far", text=project.decisions.render_markdown(limit=30), priority=3)


def _answers_section(project: Project) -> tuple[Section, list[str]]:
    answers = project.unconsumed_answers()
    if not answers:
        return Section(name="User answers", text="", priority=2), []
    lines = []
    for gate in answers:
        lines.append(f"Q ({gate.from_role}): {gate.question}\nA (user): {gate.answer}")
    return Section(name="User answers to earlier questions", text="\n\n".join(lines), priority=2), [
        g.id for g in answers
    ]


def _memory_sections(memory: CompositeMemory | NullMemory, query: str, project_slug: str) -> list[Section]:
    sections = []
    standing = standing_context(memory)
    if standing:
        sections.append(Section(name="Standing context", text=standing, priority=5))
    recall = build_recall_section(memory, query, k=4, project=project_slug)
    if recall:
        sections.append(Section(name="Lessons from past work", text=recall, priority=5))
    return sections


def _codebase_report_section(project: Project) -> Section:
    report = project.artifacts.read("codebase-report.md") or ""
    return Section(name="Codebase report", text=report, priority=4)


def charter_brief(services: Services, project: Project) -> tuple[list[Section], list[str]]:
    answers, consumed = _answers_section(project)
    sections = [
        _mission_section(project),
        answers,
        _codebase_report_section(project),
        _decisions_section(project),
        *_memory_sections(services.memory, project.mission()[:500], project.root.name),
        Section(name="Existing charter (you are revising it)",
                text=yaml.safe_dump(project.charter(), sort_keys=False) if project.charter() else "",
                priority=4),
    ]
    return sections, consumed


def design_brief(services: Services, project: Project) -> list[Section]:
    return [
        _mission_section(project),
        _charter_section(project),
        _codebase_report_section(project),
        Section(name="Repository map", text=_repomap_text(services, project), priority=4),
        _decisions_section(project),
        *_memory_sections(services.memory, project.mission()[:500], project.root.name),
    ]


def plan_brief(services: Services, project: Project, validation_errors: list[str] | None = None) -> list[Section]:
    sections = [
        _mission_section(project),
        _charter_section(project),
        _design_section(project, max_priority=2),
        _codebase_report_section(project),
    ]
    if validation_errors:
        sections.insert(
            0,
            Section(
                name="Your previous plan failed validation — fix these problems",
                text="\n".join(f"- {e}" for e in validation_errors),
                priority=1,
            ),
        )
    return sections


def item_task_text(item: WorkItem) -> str:
    lines = [f"**{item.title}** ({item.kind}, size {item.size})", "", item.description, "", "Acceptance criteria:"]
    lines += [f"- {a}" for a in item.acceptance] or ["- (none recorded — use your judgment)"]
    if item.verify_commands:
        lines.append("\nVerification commands (must exit 0):")
        lines += [f"- `{c}`" for c in item.verify_commands]
    if item.files_hint:
        lines.append("\nLikely files: " + ", ".join(item.files_hint))
    return "\n".join(lines)


def item_recitation(item: WorkItem) -> str:
    lines = [item.title, "Acceptance:"]
    lines += [f"- {a}" for a in item.acceptance[:6]]
    return "\n".join(lines)


def plan_overview_text(plan: Plan, current: WorkItem) -> str:
    """The whole plan with the current item marked — how a worker knows what
    is NOT its job."""
    lines = []
    for item in plan.items:
        marker = "  ← YOUR ITEM" if item.id == current.id else ""
        lines.append(f"- [{item.status}] {shorten(item.title, 70)}{marker}")
    lines.append("")
    lines.append(
        "Scope rule: work ONLY your item. Anything another item names is OUT OF "
        "SCOPE here — do not build it early, and do not assume it exists unless "
        "its item is done. Unsure where a piece belongs? ask_architect."
    )
    return "\n".join(lines)


def _attempt_history_section(item: WorkItem) -> Section:
    """Attempt history AND item notes: notes carry request context, plan-review
    flags, and investigation findings — they must reach the FIRST attempt too,
    not only retries."""
    if not item.attempts and not item.notes:
        return Section(name="Prior attempts", text="", priority=2)
    lines = []
    for attempt in item.attempts[-3:]:
        outcome = attempt.outcome or "interrupted"
        lines.append(f"- {attempt.role} → {outcome}: {shorten(attempt.summary, 300)}")
        if attempt.test_summary:
            lines.append(f"  tests: {shorten(attempt.test_summary, 400)}")
    for note in item.notes[-4:]:
        lines.append(f"- note: {shorten(note, 300)}")
    name = ("Prior attempts on this task (do not repeat these failures)"
            if item.attempts else "Notes on this task (context you should use)")
    return Section(name=name, text="\n".join(lines), priority=2, truncate="tail")


def item_brief(
    services: Services,
    project: Project,
    plan: Plan,
    item: WorkItem,
    config: Config,
) -> tuple[list[Section], list[str]]:
    ctx = services.context_for(project)
    query = f"{item.title}\n{item.description}\n{' '.join(item.files_hint)}"
    answers, consumed = _answers_section(project)
    progress = plan.progress()
    plan_line = ", ".join(f"{k}:{v}" for k, v in sorted(progress.items()) if k != "total")
    sections = [
        Section(name="Your task", text=item_task_text(item), priority=1),
        answers,
        _attempt_history_section(item),
        _design_section(project, max_priority=3),
        Section(name="The full plan (your scope boundaries)",
                text=plan_overview_text(plan, item), priority=3, truncate="tail"),
        Section(
            name="Repository map",
            text=_repomap_text(services, project, focus=item.files_hint),
            priority=4,
        ),
        Section(
            name="Relevant code",
            text=ctx.retriever.gather(query, focus_files=item.files_hint, budget_tokens=3000),
            priority=4,
        ),
        *_memory_sections(services.memory, query, project.root.name),
        Section(
            name="Project state",
            text=f"Plan progress: {plan_line}. Recent activity:\n{recent_activity(project.journal, 12)}",
            priority=6,
            truncate="tail",
        ),
    ]
    return sections, consumed


def scout_brief(project: Project) -> str:
    return (
        "Explore this codebase and produce the report described in your role. "
        "It will orient every later agent, so accuracy beats completeness.\n\n"
        f"## Mission (why we are here)\n{project.mission()}"
    )


def _repomap_text(services: Services, project: Project, focus: list[str] | None = None) -> str:
    try:
        return services.context_for(project).repomap.render(
            focus_files=focus,
            budget_tokens=services.config.index.repomap_tokens,
        )
    except Exception:
        return ""


def sections_to_text(sections: list[Section]) -> str:
    return "\n\n".join(s.header() + s.text.rstrip() for s in sections if s.text.strip())
