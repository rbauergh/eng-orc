"""Phase implementations: each function performs ONE unit of work for a
project and records everything it did on disk before returning.

A phase function returns a short human-readable note (surfaced in status and
the scheduler log). Phases never block on the user: questions become gates
and the project parks itself.
"""

from __future__ import annotations

import re

from ..agents import load_prompt, role
from ..agents.runtime import AttemptResult, ToolLoop, one_shot_prose, one_shot_structured
from ..agents.schemas import (
    Charter,
    DesignExtract,
    DigestExtract,
    Finding,
    PlanDraft,
    ReviewVerdict,
    TriageReport,
)
from ..agents.toolbox import (
    ToolContext,
    ensure_project_venv,
    ensure_repo,
    run_verification,
    tools_named,
)
from ..artifacts import Handoff
from ..config import PanelReviewer, RoleModel
from ..context.summarizer import recent_activity, summarize
from ..decisions import Decision
from ..events import Kind
from ..fsio import atomic_write_text, atomic_write_yaml
from ..llm.budget import ContextPacker, Section
from ..llm.catalog import model_for_agent
from ..llm.structured import StructuredError
from ..memory.schema import MemoryItem
from ..obs.console import log
from ..plan import AttemptRecord, Plan, WorkItem
from ..project import Project
from ..util import iso_now, shorten, truncate_middle
from . import briefs, supervisor
from .services import Services

# --------------------------------------------------------------------------- scout


def phase_scout(services: Services, project: Project) -> str:
    services.refresh_index(project)
    spec = role("scout")
    ctx = ToolContext(
        project=project,
        config=services.config,
        journal=project.journal,
        role="scout",
        index=services.context_for(project).index,
        extras={"phase": "scout"},
    )
    loop = ToolLoop(
        client=services.client,
        config=services.config,
        role_name="scout",
        role_model=model_for_agent(services.config, "scout"),
        tools=tools_named(*spec.tools),
        ctx=ctx,
        journal=project.journal,
        system_prompt=load_prompt(spec.prompt_file),
    )
    result = loop.run(
        brief=briefs.scout_brief(project),
        task_recitation="Produce the codebase report and finish.",
        max_turns=spec.max_turns,
    )
    report = result.handoff_md or f"(scout ended with status {result.status}: {result.summary})"
    project.artifacts.write("codebase-report.md", report)
    return f"scouted the codebase ({result.turns} turns)"


# --------------------------------------------------------------------------- charter


def phase_charter(services: Services, project: Project) -> str:
    config = services.config
    log.agent("charterer", "drafting the charter …")
    sections, consumed_gate_ids = briefs.charter_brief(services, project)
    caller = services.caller(project.journal, "charterer")
    try:
        charter = one_shot_structured(
            caller,
            model_for_agent(config, "charterer"),
            Charter,
            load_prompt("charterer.md"),
            sections,
        )
    except StructuredError as exc:
        project.journal.append(Kind.ERROR, actor="charterer", error=str(exc)[:500])
        return "charter attempt failed validation; will retry next visit"

    payload = charter.model_dump(exclude={"reasoning"})
    asked_before = supervisor.charterer_questions_asked(project)
    budget_left = max(0, config.run.clarification_budget - asked_before)
    questions = charter.blocking_questions[:budget_left]
    forced = bool(charter.blocking_questions) and not questions

    payload["ready_to_build"] = charter.ready_to_build or forced or not charter.blocking_questions
    if questions:
        payload["ready_to_build"] = False
    atomic_write_yaml(project.charter_path, payload)

    if consumed_gate_ids:
        project.mark_gates_consumed(consumed_gate_ids)

    for assumption in charter.assumptions:
        project.decisions.record(
            Decision(
                title=f"Assumption: {shorten(assumption.text, 80)}",
                decision=assumption.text,
                rationale=assumption.basis,
                confidence=assumption.confidence,
                made_by="charterer",
            )
        )
    if forced:
        project.decisions.record(
            Decision(
                title="Proceeding despite open questions (clarification budget spent)",
                decision="; ".join(q.question for q in charter.blocking_questions),
                rationale="charterer judgment: assume defaults and keep moving",
                confidence=0.5,
                made_by="charterer",
            )
        )

    if questions:
        for question in questions:
            options = question.options or []
            project.gates.open(
                question=question.question,
                from_role="charterer",
                phase="charter",
                context=question.why_blocking,
                options=options,
            )
            project.journal.append(
                Kind.GATE_OPENED, actor="charterer", question=question.question
            )
        project.set_state("blocked_on_user", reason="charter has blocking questions")
        return f"charter drafted; parked on {len(questions)} question(s) for you (see `orc inbox`)"

    project.set_state("active")
    project.set_phase("design")
    return "charter complete; ready to design"


# --------------------------------------------------------------------------- design


def phase_design(services: Services, project: Project) -> str:
    config = services.config
    log.agent("architect", "writing the design document …")
    design_md, usage = one_shot_prose(
        services.client,
        model_for_agent(config, "architect"),
        load_prompt("architect.md"),
        briefs.design_brief(services, project),
        max_tokens=4096,
    )
    if not design_md.strip():
        project.journal.append(Kind.ERROR, actor="architect", error="empty design output")
        return "architect produced nothing; will retry next visit"
    atomic_write_text(project.design_path, design_md + "\n")
    project.artifacts.write("design.md", design_md + "\n")
    project.journal.append(
        Kind.AGENT_TURN, actor="architect", tool="(design)",
        prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
    )

    # Structured extraction so downstream briefs carry decisions, not prose.
    try:
        extract = one_shot_structured(
            services.caller(project.journal, "architect"),
            config.models.utility,
            DesignExtract,
            "Extract the consequential decisions from this design document. Be faithful; do not invent.",
            [Section(name="Design document", text=design_md, priority=1)],
        )
        for dec in extract.decisions:
            project.decisions.record(
                Decision(
                    title=dec.title,
                    decision=dec.decision,
                    rationale=dec.rationale,
                    confidence=dec.confidence,
                    made_by="architect",
                )
            )
    except StructuredError:
        log.debug("design decision extraction failed; design.md still stands")

    project.set_phase("plan")
    return "design written"


# --------------------------------------------------------------------------- plan


_TESTABLE_KINDS = ("feature", "fix", "refactor")


def _draft_to_plan(draft: PlanDraft, test_policy: str = "auto") -> Plan:
    items: list[WorkItem] = []
    for entry in draft.items:
        items.append(
            WorkItem(
                title=entry.title,
                kind=entry.kind,
                description=entry.description,
                acceptance=entry.acceptance,
                verify_commands=entry.verify_commands,
                files_hint=entry.files_hint,
                size=entry.size,
            )
        )
    for index, entry in enumerate(draft.items):
        deps = []
        for dep_index in entry.depends_on:
            if 0 <= dep_index < len(items) and dep_index != index:
                deps.append(items[dep_index].id)
        items[index].depends_on = deps
        test_first = entry.test_first
        if test_policy == "always" and entry.kind in _TESTABLE_KINDS:
            test_first = True
        elif test_policy == "never":
            test_first = False
        if test_first:
            items[index].notes.append("test_first")
    plan = Plan(goal_recap=draft.goal_recap, items=items)
    return plan


def phase_plan(services: Services, project: Project) -> str:
    config = services.config
    log.agent("planner", "breaking the design into work items …")
    caller = services.caller(project.journal, "planner")
    errors: list[str] | None = None
    for _round in range(2):
        try:
            draft = one_shot_structured(
                caller,
                model_for_agent(config, "planner"),
                PlanDraft,
                load_prompt("planner.md"),
                briefs.plan_brief(services, project, validation_errors=errors),
            )
        except StructuredError as exc:
            project.journal.append(Kind.ERROR, actor="planner", error=str(exc)[:500])
            return "plan attempt failed validation; will retry next visit"
        plan = _draft_to_plan(draft, config.run.test_first)
        problems = plan.validate_graph()
        if not plan.items:
            problems.append("the plan has zero items")
        if not problems:
            project.save_plan(plan)
            project.journal.append(Kind.DECISION, actor="planner", title="plan created",
                                   items=len(plan.items))
            project.set_phase("build")
            titles = ", ".join(shorten(i.title, 40) for i in plan.items[:5])
            return f"planned {len(plan.items)} work item(s): {titles}"
        errors = problems
    project.journal.append(Kind.ERROR, actor="planner", error=f"plan invalid twice: {errors}")
    return "planner produced an invalid plan twice; will retry next visit"


def plan_request(services: Services, project: Project, request_text: str) -> str:
    """Extend an existing project's plan with items for a new bug fix or
    feature request (the natural continuation path). Reactivates wrapped
    projects: a living codebase is never 'done', only quiet."""
    import yaml as _yaml

    config = services.config
    plan = project.load_plan()
    existing = "\n".join(f"- [{i.status}] {i.title}" for i in plan.items) or "(no items yet)"
    system = load_prompt("planner.md") + (
        "\n## Mode\nYou are EXTENDING an existing project's plan with new items for the "
        "user's request. Do not restate or duplicate existing items; depends_on indices "
        "refer only to YOUR new items; reuse the established stack and conventions."
    )
    sections = [
        Section(name="New request from the user", text=request_text, priority=1),
        Section(name="Existing plan (context only — add new items, never duplicates)",
                text=existing, priority=2),
        Section(name="Charter",
                text=_yaml.safe_dump(project.charter(), sort_keys=False) if project.charter() else "",
                priority=3),
        Section(name="Design",
                text=project.design_path.read_text(encoding="utf-8")
                if project.design_path.exists() else "", priority=3, truncate="middle"),
        Section(name="Repository map",
                text=briefs._repomap_text(services, project), priority=4),
    ]
    try:
        draft = one_shot_structured(
            services.caller(project.journal, "planner"),
            model_for_agent(config, "planner"),
            PlanDraft,
            system,
            sections,
        )
    except StructuredError as exc:
        project.journal.append(Kind.ERROR, actor="planner", error=str(exc)[:400])
        return "the planner failed to turn the request into work items — try rephrasing"
    new_items = _draft_to_plan(draft, config.run.test_first).items
    if not new_items:
        return "the planner produced no items for this request — try being more specific"
    for item in new_items:
        item.notes.append(f"request: {shorten(request_text.splitlines()[0], 120)}")
    plan.add_items(new_items)
    problems = plan.validate_graph()
    if problems:
        project.journal.append(Kind.ERROR, actor="planner", error=f"request plan invalid: {problems}")
        return f"the planner produced an invalid extension ({problems[0]}) — try again"
    project.save_plan(plan)

    meta = project.meta
    if meta.phase in ("wrap", "done") or meta.state == "done":
        project.set_phase("build")
    project.set_state("active", reason="new request queued")
    project.journal.append(Kind.USER_NOTE, actor="user",
                           note=f"request: {shorten(request_text, 300)}")
    titles = ", ".join(shorten(i.title, 40) for i in new_items[:4])
    return f"queued {len(new_items)} new item(s): {titles}"


# --------------------------------------------------------------------------- build


def _needs_tester(item: WorkItem) -> bool:
    if "test_first" not in item.notes:
        return False
    return not any(a.role == "tester" and a.outcome == "success" for a in item.attempts)


def _absorb_supervisor_guidance(project: Project, plan: Plan) -> bool:
    """User answers to the supervisor's stuck-item gate become actionable:
    failed items get the guidance as notes (with the failed attempts' summaries
    preserved) and a fresh attempt budget. This is what makes answering the
    gate meaningfully different from re-running into the same wall."""
    answers = [g for g in project.unconsumed_answers() if g.from_role == "supervisor"]
    if not answers:
        return False
    changed = False
    for gate in answers:
        for item in plan.items:
            if item.status != "failed":
                continue
            for attempt in item.attempts[-2:]:
                if attempt.summary:
                    item.notes.append(f"earlier attempt ({attempt.role}): {shorten(attempt.summary, 160)}")
            item.notes.append(f"user guidance: {shorten(gate.answer, 300)}")
            item.attempts = []
            item.status = "todo"
            item.touch()
            changed = True
        project.journal.append(Kind.USER_NOTE, actor="user",
                               note=f"guidance on stuck items: {gate.answer}")
        log.info(f"applying your guidance to stuck items: {shorten(gate.answer, 100)}")
    project.mark_gates_consumed([g.id for g in answers])
    if changed:
        project.save_plan(plan)
    return changed


def _triage_round(item: WorkItem) -> int:
    return sum(1 for note in item.notes if note.startswith("triage#"))


def _triage_evidence(project: Project, items: list[WorkItem]) -> str:
    parts = []
    charter = project.charter() or {}
    criteria = charter.get("success_criteria") or []
    if criteria:
        parts.append("## Charter success criteria (work these need must NOT be dropped)\n"
                     + "\n".join(f"- {c}" for c in criteria))
    for item in items:
        lines = [f"## Item {item.id}: {item.title}", item.description or "(no description)"]
        if item.acceptance:
            lines.append("Acceptance: " + "; ".join(item.acceptance))
        if item.verify_commands:
            lines.append("Verify commands: " + "; ".join(item.verify_commands))
        for attempt in item.attempts[-3:]:
            lines.append(
                f"- attempt [{attempt.role}/{attempt.model}] {attempt.outcome}: "
                f"{shorten(attempt.summary, 220)}"
            )
            if attempt.test_summary:
                lines.append(f"  verification: {shorten(attempt.test_summary, 300)}")
        for note in item.notes[-8:]:
            lines.append(f"- note: {shorten(note, 220)}")
        parts.append("\n".join(lines))
    errors = project.journal.tail(10, kinds=[Kind.ERROR])
    if errors:
        parts.append("## Recent errors (some failures may be infrastructure, not the work)\n"
                     + "\n".join(f"- [{e.actor}] {shorten(str(e.payload.get('error', '')), 220)}"
                                 for e in errors))
    return "\n\n".join(parts)


def _run_triage(services: Services, project: Project, plan: Plan, items: list[WorkItem]) -> str:
    """The planner inspects the failure evidence and revises the plan so the
    build/test/review agents get NEW information — escalating to the human
    only for decisions a model cannot make. Returns "" when triage failed
    (caller falls back to the human gate)."""
    config = services.config
    log.agent("triage", f"diagnosing {len(items)} stuck item(s) from the evidence …")
    try:
        report = one_shot_structured(
            services.caller(project.journal, "triage"),
            model_for_agent(config, "planner"),
            TriageReport,
            load_prompt("triage.md"),
            [Section(name="Evidence", text=_triage_evidence(project, items),
                     priority=1, truncate="middle")],
        )
    except Exception as exc:
        project.journal.append(Kind.ERROR, actor="triage", error=str(exc)[:400])
        return ""

    by_id = {item.id: item for item in items}
    questions: list[tuple[WorkItem, object]] = []
    acted = 0
    for entry in report.items:
        item = by_id.get(entry.item_id)
        if item is None:
            continue
        marker = f"triage#{_triage_round(item) + 1}: {shorten(entry.diagnosis, 400)}"
        log.agent("triage", f"{entry.action} '{shorten(item.title, 50)}' — {shorten(entry.diagnosis, 90)}")
        project.journal.append(Kind.DECISION, actor="triage", item=item.id,
                               title=f"triage: {entry.action}",
                               diagnosis=shorten(entry.diagnosis, 240))
        if entry.action in ("revise", "retry"):
            if entry.action == "revise":
                if entry.new_description:
                    item.description = entry.new_description
                if entry.new_acceptance:
                    item.acceptance = entry.new_acceptance
                if entry.new_verify_commands:
                    item.verify_commands = entry.new_verify_commands
            item.notes.append(marker)
            if entry.guidance:
                item.notes.append(f"triage guidance: {shorten(entry.guidance, 300)}")
            item.attempts = []
            item.status = "todo"
            item.touch()
            acted += 1
        elif entry.action == "drop":
            item.notes.append(marker)
            plan.set_status(item.id, "dropped")
            project.decisions.record(Decision(
                title=f"Dropped: {shorten(item.title, 60)}",
                decision=entry.diagnosis,
                rationale=entry.guidance or "triage judgment",
                confidence=0.6,
                made_by="triage",
                item=item.id,
            ))
            acted += 1
        elif entry.action == "split" and entry.split_items:
            replacements = _draft_to_plan(
                PlanDraft(reasoning="", goal_recap="", items=entry.split_items),
                config.run.test_first,
            ).items
            previous = None
            for replacement in replacements:
                replacement.depends_on = list(set(replacement.depends_on) | set(item.depends_on))
                if previous is not None and previous.id not in replacement.depends_on:
                    replacement.depends_on.append(previous.id)
                replacement.notes.append(f"split from {item.id}: {shorten(entry.diagnosis, 160)}")
                previous = replacement
            plan.add_items(replacements)
            item.notes.append(marker)
            plan.set_status(item.id, "dropped")
            acted += 1
        elif entry.action == "ask_user":
            questions.append((item, entry))
    for note in report.systemic_notes:
        log.warn(f"triage flags a systemic problem: {note}")
        project.journal.append(Kind.ERROR, actor="triage", error=f"systemic: {shorten(note, 300)}")
    project.save_plan(plan)

    if questions:
        for item, entry in questions:
            project.gates.open(
                question=f"{entry.question}\n\nTriage diagnosis: {shorten(entry.diagnosis, 240)}",
                from_role="supervisor",
                phase="build",
                item=item.id,
                context=f"'{item.title}' failed repeatedly; triage could not resolve it autonomously",
            )
            project.journal.append(Kind.GATE_OPENED, actor="triage", item=item.id,
                                   question=entry.question)
        project.set_state("blocked_on_user", reason="triage needs a human decision")
        return (f"triage: {acted} item(s) adjusted autonomously, "
                f"{len(questions)} question(s) need you (orc inbox)")
    if acted:
        return f"triage adjusted {acted} stuck item(s) with fresh direction; retrying"
    return ""


def _handoff_excerpt(handoff_md: str, max_lines: int = 6) -> list[str]:
    """The meat of a handoff note: content lines, headers and blanks dropped."""
    lines = []
    for raw in handoff_md.splitlines():
        stripped = raw.strip().lstrip("#").strip()
        if not stripped:
            continue
        lines.append(shorten(stripped, 140))
        if len(lines) >= max_lines:
            break
    return lines


def implementer_model_role(config, prior_failures: int) -> str:
    """Which model slot writes this attempt. The primary coder gets the early
    attempts; once those fail, later attempts rotate through coder_fallbacks —
    a different model family retries with the failure evidence in its brief."""
    fallbacks = list(config.run.coder_fallbacks)
    if not fallbacks:
        return "coder"
    primary_attempts = max(1, config.run.max_attempts_per_item - len(fallbacks))
    if prior_failures < primary_attempts:
        return "coder"
    return fallbacks[min(prior_failures - primary_attempts, len(fallbacks) - 1)]


def _run_item_loop(
    services: Services,
    project: Project,
    plan: Plan,
    item: WorkItem,
    role_name: str,
) -> tuple[AttemptRecord, AttemptResult]:
    config = services.config
    spec = role(role_name)
    if role_name == "implementer":
        model_role = implementer_model_role(config, item.open_attempt_count())
        try:
            role_model = config.models.for_role(model_role)
        except KeyError:
            project.journal.append(
                Kind.ERROR, actor=role_name, item=item.id,
                error=f"coder fallback role {model_role!r} not configured; using primary coder",
            )
            role_model = config.models.coder
    else:
        role_model = model_for_agent(config, role_name)
    attempt = AttemptRecord(role=role_name, model=role_model.model)
    item.attempts.append(attempt)
    item.status = "in_progress"
    item.touch()
    project.save_plan(plan)
    project.journal.append(Kind.ATTEMPT_STARTED, actor=role_name, item=item.id, attempt=attempt.id)
    log.agent(
        role_name,
        f"starting '{shorten(item.title, 60)}' "
        f"(attempt {len(item.attempts)}/{config.run.max_attempts_per_item}, model {role_model.model})",
    )

    services.observe_gpu()
    ctx = ToolContext(
        project=project,
        config=config,
        journal=project.journal,
        item_id=item.id,
        role=role_name,
        index=services.context_for(project).index,
        extras={"phase": "build", "verify_commands": item.verify_commands},
    )
    ensure_project_venv(ctx)  # the dependency sandbox: pip installs stay project-local
    sections, consumed = briefs.item_brief(services, project, plan, item, config)
    brief_text = _pack_brief(services, role_model, sections)
    loop = ToolLoop(
        client=services.client,
        config=config,
        role_name=role_name,
        role_model=role_model,
        tools=tools_named(*spec.tools),
        ctx=ctx,
        journal=project.journal,
        system_prompt=load_prompt(spec.prompt_file),
    )
    max_turns = spec.max_turns or config.run.max_turns_coder
    result = loop.run(
        brief=brief_text,
        task_recitation=briefs.item_recitation(item),
        max_turns=max_turns,
    )
    if consumed:
        project.mark_gates_consumed(consumed)

    attempt.ended = iso_now()
    attempt.summary = result.summary
    attempt.transcript = result.transcript
    attempt.tokens_in = result.tokens_in
    attempt.tokens_out = result.tokens_out
    attempt.outcome = {
        "done": "success",
        "failed": "fail",
        "stuck": "stuck",
        "error": "error",
        "blocked_on_user": "fail",
    }[result.status]
    handoff_lines = _handoff_excerpt(result.handoff_md) if result.handoff_md else []
    project.journal.append(
        Kind.ATTEMPT_FINISHED,
        actor=role_name,
        item=item.id,
        status=result.status,
        summary=shorten(result.summary, 300),
        handoff=handoff_lines,
    )
    # The tester's "behaviors I encoded" and the implementer's "what I built"
    # live in their handoff notes — narrate them instead of burying them.
    if result.status == "done" and handoff_lines:
        for line in handoff_lines:
            log.info(f"    ↳ {line}")
    if result.handoff_md:
        handoff = Handoff(
            from_role=role_name,
            item=item.id,
            summary=result.summary,
            state_of_work=result.handoff_md,
            touched_files=result.touched_files,
        )
        project.artifacts.write(
            f"handoff-{role_name}.md", handoff.to_markdown(), subdir=f"attempts/{item.id}"
        )
    return attempt, result


def _pack_brief(services: Services, role_model: RoleModel, sections: list[Section]) -> str:
    packer = ContextPacker(
        context_window=role_model.context_window,
        reserve_output=role_model.max_output_tokens,
    )
    packed = packer.pack(sections, fixed_tokens=1200)  # system prompt + tool docs live outside
    return packed.text


def _review_sections(project: Project, item: WorkItem, diff: str,
                     verify_summary: str = "") -> list[Section]:
    from ..agents.toolbox.git import tracked_files

    files = tracked_files(project.workroom)
    sections = [
        Section(name="Work item", text=briefs.item_task_text(item), priority=1),
        Section(name="Diff under review", text=diff or "(no diff captured)", priority=1, truncate="middle"),
        Section(
            name="Files present in the workroom (verify 'missing file' claims against this)",
            text="\n".join(files) if files else "(none)",
            priority=3,
        ),
        Section(
            name="Design (excerpt)",
            text=project.design_path.read_text(encoding="utf-8") if project.design_path.exists() else "",
            priority=4,
        ),
    ]
    if verify_summary:
        sections.insert(2, Section(
            name=("Verification results — the harness ALREADY RAN these commands; "
                  "runtime acceptance criteria are settled by them"),
            text=verify_summary,
            priority=2,
        ))
    return sections


def _review_one_seat(
    services: Services,
    project: Project,
    item: WorkItem,
    diff: str,
    seat: PanelReviewer,
    artifact_name: str,
    stage: str = "code",
    verify_summary: str = "",
) -> tuple[str, ReviewVerdict] | None:
    """One reviewer, one verdict: journaled, narrated, archived. Returns
    (model, verdict) or None when the seat is unavailable — a dead reviewer
    must never wedge the pipeline."""
    from ..agents.roles import REVIEW_LENSES

    config = services.config
    actor = f"reviewer:{seat.lens}@{seat.model_role}"
    try:
        role_model = config.models.for_role(seat.model_role)
    except KeyError as exc:
        project.journal.append(Kind.ERROR, actor=actor, item=item.id,
                               error=f"panel seat skipped: {exc}")
        return None
    lens_text = REVIEW_LENSES.get(seat.lens, REVIEW_LENSES["correctness"])
    system = load_prompt("reviewer.md") + "\n## Your lens for this review\n" + lens_text
    if stage == "tests":
        system += (
            "\n## Stage note\nYou are reviewing ONLY the tests, before any implementation "
            "exists. Judge whether they encode the acceptance criteria faithfully: failing "
            "now is expected and correct; weak assertions, permutation spam, or tests that "
            "mirror a guessed implementation instead of the required behavior are blockers."
        )
    log.agent(actor, f"reviewing {'tests for ' if stage == 'tests' else ''}"
                     f"'{shorten(item.title, 50)}' on {role_model.model} …")
    try:
        verdict = one_shot_structured(
            services.caller(project.journal, actor),
            role_model,
            ReviewVerdict,
            system,
            _review_sections(project, item, diff, verify_summary),
        )
    except Exception as exc:
        project.journal.append(Kind.ERROR, actor=actor, item=item.id,
                               error=f"panel seat unavailable: {str(exc)[:300]}")
        return None
    blocker_lines = [
        f"{f.category}/{f.severity}: {shorten(f.description, 120)} → {shorten(f.recommendation, 80)}"
        for f in verdict.blockers()
    ]
    project.journal.append(
        Kind.REVIEW, item=item.id, verdict=verdict.verdict,
        findings=len(verdict.findings), lens=seat.lens, model=role_model.model,
        blockers=blocker_lines, stage=stage,
    )
    log.agent(actor, f"{verdict.verdict} — {len(verdict.findings)} finding(s): "
                     f"{shorten(verdict.summary, 90)}")
    for line in blocker_lines:
        log.info(f"    ↳ {line}")
    services.observe_gpu()  # panel seats are where swaps happen mid-step
    review_md = [f"# Review ({stage}) — {verdict.verdict}",
                 f"_lens: {seat.lens} · model: {role_model.model}_", "", verdict.summary, ""]
    for finding in verdict.findings:
        review_md.append(
            f"- **{finding.category}/{finding.severity}** {finding.description}"
            f" → {finding.recommendation}"
        )
    project.artifacts.write(artifact_name, "\n".join(review_md), subdir=f"attempts/{item.id}")
    return role_model.model, verdict


def run_review_panel(
    services: Services, project: Project, item: WorkItem, diff: str,
    verify_summary: str = "",
) -> list[tuple[PanelReviewer, str, ReviewVerdict]]:
    """Each panelist reviews independently: distinct weights and lenses catch
    failure modes a single (self-preferring) model misses. A panelist whose
    model is unreachable or whose verdict fails validation is skipped with a
    journal record — the surviving panel decides."""
    config = services.config
    panel = config.review.panel or [PanelReviewer()]
    results: list[tuple[PanelReviewer, str, ReviewVerdict]] = []
    for seat_no, seat in enumerate(panel, 1):
        outcome = _review_one_seat(
            services, project, item, diff, seat,
            artifact_name=f"review-{seat_no}-{seat.lens}.md",
            verify_summary=verify_summary,
        )
        if outcome is not None:
            results.append((seat, outcome[0], outcome[1]))
    return results


def _tests_seat(config) -> PanelReviewer:
    for seat in config.review.panel:
        if seat.lens == "tests":
            return seat
    return PanelReviewer(model_role="coder", lens="tests")


def review_tests(
    services: Services, project: Project, item: WorkItem, attempt: AttemptRecord, sha_before: str
) -> str | None:
    """The TDD gate: the tester's behavioral tests get their own review before
    any implementation is built to them. Returns a step note when the tests
    were rejected (or absent); None means proceed to implementation."""
    from ..agents.toolbox.git import diff_since

    diff = truncate_middle(diff_since(project.workroom, sha_before), 20000)
    if not diff.strip():
        attempt.outcome = "fail"
        attempt.summary = "tester finished without writing any tests"
        item.notes.append("tester claimed done but produced no test changes")
        return f"tester produced no test changes on '{shorten(item.title, 50)}'; retrying"
    seat = _tests_seat(services.config)
    outcome = _review_one_seat(
        services, project, item, diff, seat,
        artifact_name=f"review-tests-{seat.lens}.md", stage="tests",
    )
    if outcome is None:
        return None  # reviewer unavailable — never wedge the pipeline on the gate
    _, verdict = outcome
    if verdict.verdict == "request_changes" and verdict.blockers():
        for finding in verdict.blockers():
            item.notes.append(
                f"test-review[{seat.lens}] {finding.category}/{finding.severity}: "
                f"{finding.description} → {finding.recommendation}"
            )
        attempt.outcome = "fail"
        attempt.summary = f"test review requested changes ({len(verdict.blockers())} blocking)"
        return f"test review requested changes on '{shorten(item.title, 50)}'"
    return None


def panel_outcome(
    results: list[tuple[PanelReviewer, str, ReviewVerdict]],
) -> tuple[bool, list[tuple[str, Finding]]]:
    """Sign-off requires every panelist to approve. Blocking findings (from
    panelists requesting changes) are unioned and deduplicated; each keeps its
    lens label so the implementer knows who is asking for what."""
    blockers: list[tuple[str, Finding]] = []
    seen: set[tuple[str, str, str]] = set()
    approved = True
    for seat, _model, verdict in results:
        seat_blockers = verdict.blockers()
        if verdict.verdict == "request_changes" and seat_blockers:
            approved = False
            for finding in seat_blockers:
                key = (
                    finding.category,
                    finding.file,
                    re.sub(r"[^a-z0-9]+", "", finding.description.lower())[:80],
                )
                if key in seen:
                    continue
                seen.add(key)
                blockers.append((seat.lens, finding))
    return approved, blockers


def _integrate_item(services: Services, project: Project, plan: Plan, item: WorkItem) -> str:
    from ..agents.toolbox.git import commit_all

    prefix = {"feature": "feat", "fix": "fix", "refactor": "refactor", "test": "test",
              "docs": "docs", "chore": "chore", "investigate": "chore", "integrate": "chore"}[item.kind]
    ok, sha_or_err = commit_all(project.workroom, f"{prefix}: {item.title}")
    if ok:
        project.journal.append(Kind.COMMIT, item=item.id, sha=sha_or_err, message=item.title)
    plan.set_status(item.id, "done")
    project.save_plan(plan)
    project.journal.append(Kind.ITEM_STATUS, item=item.id, status="done")
    services.refresh_index(project)
    return sha_or_err if ok else ""


def phase_build(services: Services, project: Project) -> str:
    config = services.config
    plan = project.load_plan()
    if supervisor.cleanup_dangling_attempts(plan):
        project.save_plan(plan)
    _absorb_supervisor_guidance(project, plan)
    if plan.is_complete():
        project.set_phase("wrap")
        return "all work items complete; wrapping up"

    item = supervisor.pick_item(plan, config.run.max_attempts_per_item)
    if item is None:
        exhausted = supervisor.exhausted_items(plan, config.run.max_attempts_per_item)
        open_exhausted = [i for i in exhausted if i.status != "failed"]
        for stuck_item in open_exhausted:
            plan.set_status(stuck_item.id, "failed")
        if open_exhausted:
            project.save_plan(plan)

        # Before asking a human: let the planner debug the failures and feed
        # NEW information to the next attempts (revise/split/drop/retry).
        candidates = [i for i in exhausted
                      if i.status == "failed" and _triage_round(i) < config.run.triage_rounds]
        if candidates:
            note = _run_triage(services, project, plan, candidates)
            if note:
                return note

        if exhausted and not project.gates.open_gates():
            # this question is a human-facing artifact: the diagnosis travels
            # WHOLE — a truncated diagnosis is homework, not information
            lines = []
            for stuck in exhausted[:3]:
                last = stuck.last_attempt()
                diagnosis = next((n for n in reversed(stuck.notes) if n.startswith("triage#")), "")
                entry = f"'{stuck.title}': {shorten((last.summary if last else ''), 200)}"
                if diagnosis:
                    entry += f"\n  {shorten(diagnosis, 600)}"
                lines.append(entry)
            project.gates.open(
                question=(
                    "Triage could not unstick these items and I need direction "
                    "(simplify the goal, drop them, or point at the fix):\n- "
                    + "\n- ".join(lines)
                ),
                from_role="supervisor",
                phase="build",
            )
            project.set_state("blocked_on_user", reason="work items exhausted their attempts")
            return "stuck on failed items; asked for your guidance (see `orc inbox`)"
        if project.gates.open_gates():
            project.set_state("blocked_on_user", reason="waiting on open questions")
            return "waiting on your answers (see `orc inbox`)"
        project.set_phase("wrap")
        return "no runnable items remain; wrapping up with what succeeded"

    ensure_repo(project.workroom)
    from ..agents.toolbox.git import diff_since, head_sha

    sha_before = head_sha(project.workroom)
    role_name = "tester" if _needs_tester(item) else "implementer"
    attempt, result = _run_item_loop(services, project, plan, item, role_name)

    if result.status == "blocked_on_user":
        plan.set_status(item.id, "blocked")
        project.save_plan(plan)
        project.set_state("blocked_on_user", reason=f"{role_name} asked a question on {item.title}")
        return f"{role_name} parked a question for you (see `orc inbox`)"

    if role_name == "tester":
        gate_note = None
        if attempt.outcome == "success" and config.run.review_required and config.run.review_tests:
            gate_note = review_tests(services, project, item, attempt, sha_before)
        plan.set_status(item.id, "todo")
        project.save_plan(plan)
        if gate_note:
            return gate_note
        if attempt.outcome == "success":
            return f"tests written and reviewed for '{shorten(item.title, 50)}'; implementation is next"
        return f"tester {attempt.outcome} on '{shorten(item.title, 50)}'"

    if result.status != "done":
        plan.set_status(item.id, "todo")
        project.save_plan(plan)
        return f"implementer {attempt.outcome} on '{shorten(item.title, 50)}': {shorten(result.summary, 80)}"

    # Deterministic verification — the implementer saying "done" is a claim, not a fact.
    ctx = ToolContext(project=project, config=config, journal=project.journal,
                      item_id=item.id, role="verifier")
    report = run_verification(ctx, item.verify_commands)
    project.journal.append(Kind.VERIFY_RUN, item=item.id, passed=report.passed,
                           summary=report.summary())
    if not report.passed:
        log.warn(
            f"verify failed on '{shorten(item.title, 40)}': "
            f"{shorten(report.failure_detail(600), 160)}"
        )
        attempt.outcome = "fail"
        attempt.test_summary = truncate_middle(report.summary() + "\n" + report.failure_detail(), 2000)
        item.notes.append(f"verification failed after 'done': {shorten(report.failure_detail(800), 300)}")
        plan.set_status(item.id, "todo")
        project.save_plan(plan)
        return f"verification failed on '{shorten(item.title, 50)}'; feedback recorded for the next attempt"
    attempt.test_summary = report.summary()

    diff = diff_since(project.workroom, sha_before)
    if not diff.strip():
        # "done" with zero file changes is not done — do not waste the panel on it
        attempt.outcome = "fail"
        attempt.summary = "finished without changing any files"
        item.notes.append("previous attempt claimed done but produced no file changes")
        plan.set_status(item.id, "todo")
        project.save_plan(plan)
        return f"implementer produced no changes on '{shorten(item.title, 50)}'; retrying"

    if config.run.review_required:
        results = run_review_panel(services, project, item, truncate_middle(diff, 24000),
                                   verify_summary=report.summary())
        if not results:
            project.journal.append(
                Kind.ERROR, actor="reviewer", item=item.id,
                error="no review panelist was available; integrating unreviewed",
            )
        else:
            approved, blockers = panel_outcome(results)
            summary_md = [f"# Review panel — {'approved' if approved else 'changes requested'}", ""]
            for seat, model, verdict in results:
                summary_md.append(
                    f"- {seat.lens} ({model}): **{verdict.verdict}**, "
                    f"{len(verdict.findings)} finding(s) — {shorten(verdict.summary, 140)}"
                )
            project.artifacts.write("review.md", "\n".join(summary_md), subdir=f"attempts/{item.id}")
            if not approved:
                for lens, finding in blockers:
                    item.notes.append(
                        f"review[{lens}] {finding.category}/{finding.severity}: "
                        f"{finding.description} → {finding.recommendation}"
                    )
                attempt.outcome = "fail"
                attempt.summary = (
                    f"review panel requested changes ({len(blockers)} blocking finding(s) "
                    f"across {len(results)} reviewer(s))"
                )
                plan.set_status(item.id, "todo")
                project.save_plan(plan)
                return f"review panel requested changes on '{shorten(item.title, 50)}'"

    sha = _integrate_item(services, project, plan, item)
    suffix = f" (commit {sha})" if sha else ""
    return f"completed '{shorten(item.title, 50)}'{suffix}"


# --------------------------------------------------------------------------- wrap


def phase_wrap(services: Services, project: Project) -> str:
    config = services.config
    log.agent("historian", "digesting the project into memory …")
    plan = project.load_plan()
    progress = plan.progress()
    digest_source = "\n\n".join(
        [
            f"# Mission\n{project.mission()}",
            f"# Decisions\n{project.decisions.render_markdown()}",
            f"# Activity\n{recent_activity(project.journal, 120)}",
        ]
    )
    digest = summarize(
        services.client,
        config.models.utility,
        digest_source,
        "Digest this project session for the record.",
        max_tokens=700,
    )
    lessons_kept = 0
    try:
        extract = one_shot_structured(
            services.caller(project.journal, "historian"),
            model_for_agent(config, "historian"),
            DigestExtract,
            load_prompt("historian.md"),
            [Section(name="Session digest", text=digest_source, priority=1, truncate="middle")],
        )
        summary_text = extract.summary or digest
        for lesson in extract.lessons:
            services.memory.save(MemoryItem(kind="lesson", project=project.root.name,
                                            title=shorten(lesson, 90), body=lesson))
            lessons_kept += 1
        for convention in extract.conventions:
            services.memory.save(MemoryItem(kind="convention", project="",
                                            title=shorten(convention, 90), body=convention))
            lessons_kept += 1
    except StructuredError:
        summary_text = digest

    services.memory.save(
        MemoryItem(
            kind="project_card",
            project=project.root.name,
            title=f"Project: {project.meta.title}",
            body=summary_text,
            tags=["wrap"],
        )
    )
    project.journal.append(Kind.MEMORY_SAVED, kinds="lessons+card", count=lessons_kept + 1)

    done = progress.get("done", 0)
    failed = progress.get("failed", 0) + progress.get("dropped", 0)
    report = "\n".join(
        [
            f"# Final report — {project.meta.title}",
            f"_{iso_now()}_",
            "",
            f"Work items: {done} done, {failed} failed/dropped, {progress.get('total', 0)} total.",
            "",
            "## Summary",
            summary_text,
            "",
            "## Decisions",
            project.decisions.render_markdown(),
        ]
    )
    project.artifacts.write("report.md", report)
    if hasattr(services.memory, "curate") and config.memory.curate_with_agent:
        services.memory.curate(summary_text)
    if hasattr(services.memory, "sync"):
        services.memory.sync()

    project.set_phase("done")
    project.set_state("done", reason="mission wrapped")
    return f"project wrapped: {done}/{progress.get('total', 0)} items done — report.md written"


PHASES = {
    "scout": phase_scout,
    "charter": phase_charter,
    "design": phase_design,
    "plan": phase_plan,
    "build": phase_build,
    "wrap": phase_wrap,
}
