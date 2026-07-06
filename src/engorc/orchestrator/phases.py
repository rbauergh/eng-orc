"""Phase implementations: each function performs ONE unit of work for a
project and records everything it did on disk before returning.

A phase function returns a short human-readable note (surfaced in status and
the scheduler log). Phases never block on the user: questions become gates
and the project parks itself.
"""

from __future__ import annotations

from ..agents import load_prompt, role
from ..agents.runtime import AttemptResult, ToolLoop, one_shot_prose, one_shot_structured
from ..agents.schemas import (
    Charter,
    DesignExtract,
    DigestExtract,
    PlanDraft,
    ReviewVerdict,
)
from ..agents.toolbox import ToolContext, ensure_repo, run_verification, tools_named
from ..artifacts import Handoff
from ..config import RoleModel
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


def _draft_to_plan(draft: PlanDraft) -> Plan:
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
        if entry.test_first:
            items[index].notes.append("test_first")
    plan = Plan(goal_recap=draft.goal_recap, items=items)
    return plan


def phase_plan(services: Services, project: Project) -> str:
    config = services.config
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
        plan = _draft_to_plan(draft)
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


# --------------------------------------------------------------------------- build


def _needs_tester(item: WorkItem) -> bool:
    if "test_first" not in item.notes:
        return False
    return not any(a.role == "tester" and a.outcome == "success" for a in item.attempts)


def _run_item_loop(
    services: Services,
    project: Project,
    plan: Plan,
    item: WorkItem,
    role_name: str,
) -> tuple[AttemptRecord, AttemptResult]:
    config = services.config
    spec = role(role_name)
    role_model = model_for_agent(config, role_name)
    attempt = AttemptRecord(role=role_name, model=role_model.model)
    item.attempts.append(attempt)
    item.status = "in_progress"
    item.touch()
    project.save_plan(plan)
    project.journal.append(Kind.ATTEMPT_STARTED, actor=role_name, item=item.id, attempt=attempt.id)

    ctx = ToolContext(
        project=project,
        config=config,
        journal=project.journal,
        item_id=item.id,
        role=role_name,
        index=services.context_for(project).index,
        extras={"phase": "build", "verify_commands": item.verify_commands},
    )
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
    project.journal.append(
        Kind.ATTEMPT_FINISHED,
        actor=role_name,
        item=item.id,
        status=result.status,
        summary=shorten(result.summary, 300),
    )
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


def _review_item(services: Services, project: Project, item: WorkItem, diff: str) -> ReviewVerdict:
    sections = [
        Section(name="Work item", text=briefs.item_task_text(item), priority=1),
        Section(name="Diff under review", text=diff or "(no diff captured)", priority=1, truncate="middle"),
        Section(
            name="Design (excerpt)",
            text=project.design_path.read_text(encoding="utf-8") if project.design_path.exists() else "",
            priority=4,
        ),
    ]
    return one_shot_structured(
        services.caller(project.journal, "reviewer"),
        model_for_agent(services.config, "reviewer"),
        ReviewVerdict,
        load_prompt("reviewer.md"),
        sections,
    )


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
        if exhausted and not project.gates.open_gates():
            summary = "; ".join(
                f"{i.title}: {shorten((i.last_attempt().summary if i.last_attempt() else ''), 120)}"
                for i in exhausted[:3]
            )
            project.gates.open(
                question=(
                    "I am stuck: these work items failed repeatedly and block progress. "
                    "Advise (simplify, drop, or give direction): " + summary
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
        plan.set_status(item.id, "todo")
        project.save_plan(plan)
        if attempt.outcome == "success":
            return f"tests written for '{shorten(item.title, 50)}'; implementation is next"
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
        attempt.outcome = "fail"
        attempt.test_summary = truncate_middle(report.summary() + "\n" + report.failure_detail(), 2000)
        item.notes.append(f"verification failed after 'done': {shorten(report.failure_detail(800), 300)}")
        plan.set_status(item.id, "todo")
        project.save_plan(plan)
        return f"verification failed on '{shorten(item.title, 50)}'; feedback recorded for the next attempt"
    attempt.test_summary = report.summary()

    if config.run.review_required:
        diff = diff_since(project.workroom, sha_before)
        try:
            verdict = _review_item(services, project, item, truncate_middle(diff, 24000))
        except StructuredError as exc:
            project.journal.append(Kind.ERROR, actor="reviewer", error=str(exc)[:400])
            verdict = None
        if verdict is not None:
            project.journal.append(
                Kind.REVIEW, item=item.id, verdict=verdict.verdict, findings=len(verdict.findings)
            )
            review_md = [f"# Review — {verdict.verdict}", "", verdict.summary, ""]
            for finding in verdict.findings:
                review_md.append(
                    f"- **{finding.category}/{finding.severity}** {finding.description}"
                    f" → {finding.recommendation}"
                )
            project.artifacts.write("review.md", "\n".join(review_md), subdir=f"attempts/{item.id}")
            if verdict.verdict == "request_changes" and verdict.blockers():
                for finding in verdict.blockers():
                    item.notes.append(
                        f"review {finding.category}/{finding.severity}: {finding.description} "
                        f"→ {finding.recommendation}"
                    )
                attempt.outcome = "fail"
                attempt.summary = f"review requested changes: {shorten(verdict.summary, 160)}"
                plan.set_status(item.id, "todo")
                project.save_plan(plan)
                return f"review requested changes on '{shorten(item.title, 50)}'"

    sha = _integrate_item(services, project, plan, item)
    suffix = f" (commit {sha})" if sha else ""
    return f"completed '{shorten(item.title, 50)}'{suffix}"


# --------------------------------------------------------------------------- wrap


def phase_wrap(services: Services, project: Project) -> str:
    config = services.config
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
