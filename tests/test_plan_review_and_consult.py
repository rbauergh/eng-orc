"""Plan review (a second model checks the DAG before building) and the
implementer's scope machinery: the full-plan brief section and ask_architect."""

import json

from engorc.agents.toolbox import ALL_TOOLS, ToolContext
from engorc.events import Journal
from engorc.llm.fake import FakeLLM
from engorc.orchestrator.phases import phase_plan
from engorc.orchestrator.services import Services
from engorc.plan import Plan, WorkItem
from engorc.registry import Registry


def _draft_payload(with_deps: bool) -> str:
    return json.dumps({
        "reasoning": "two items",
        "goal_recap": "build the thing",
        "items": [
            {"title": "Scaffold", "kind": "chore", "description": "structure",
             "acceptance": ["dirs exist"], "verify_commands": ["test -d src"],
             "depends_on": [], "files_hint": [], "size": "S", "test_first": False},
            {"title": "Build & Polish", "kind": "chore", "description": "package it",
             "acceptance": ["dist exists"], "verify_commands": ["test -d dist"],
             "depends_on": [0] if with_deps else [], "files_hint": [], "size": "S",
             "test_first": False},
        ],
    })


def _ready_project(config):
    from engorc.fsio import atomic_write_yaml

    project = Registry(config).create("plan mission", title="P")
    atomic_write_yaml(project.charter_path, {"ready_to_build": True})
    project.design_path.write_text("# design\n")
    return project


def test_plan_review_approves_and_records(config):
    def brain(messages, response_format, role_model):
        name = (response_format or {}).get("json_schema", {}).get("name", "")
        if name == "PlanDraft":
            return _draft_payload(with_deps=True)
        assert name == "PlanReviewVerdict"
        return json.dumps({"reasoning": "clean", "findings": [], "verdict": "approve"})

    project = _ready_project(config)
    services = Services.build(config, client=FakeLLM(brain))
    note = phase_plan(services, project)
    assert "review approved by" in note
    reviews = [e for e in project.journal.iter_events(kinds=["review"])
               if e.payload.get("lens") == "plan"]
    assert reviews and reviews[0].payload["verdict"] == "approve"


def test_plan_review_findings_drive_a_replan(config):
    state = {"drafts": 0, "reviews": 0}

    def brain(messages, response_format, role_model):
        name = (response_format or {}).get("json_schema", {}).get("name", "")
        if name == "PlanDraft":
            state["drafts"] += 1
            # first draft has the classic defect; the redraft fixes it
            return _draft_payload(with_deps=state["drafts"] > 1)
        state["reviews"] += 1
        if state["reviews"] == 1:
            return json.dumps({
                "reasoning": "found the missing edge",
                "findings": ["Build & Polish: no dependencies — it would be "
                             "scheduled first → depend on Scaffold"],
                "verdict": "request_changes",
            })
        return json.dumps({"reasoning": "fixed", "findings": [], "verdict": "approve"})

    project = _ready_project(config)
    services = Services.build(config, client=FakeLLM(brain))
    note = phase_plan(services, project)
    assert "review approved by" in note
    assert state["drafts"] == 2 and state["reviews"] == 2
    plan = project.load_plan()
    build = next(i for i in plan.items if i.title == "Build & Polish")
    scaffold = next(i for i in plan.items if i.title == "Scaffold")
    assert build.depends_on == [scaffold.id]


def test_unavailable_plan_reviewer_never_wedges(config):
    def brain(messages, response_format, role_model):
        name = (response_format or {}).get("json_schema", {}).get("name", "")
        if name == "PlanDraft":
            return _draft_payload(with_deps=True)
        return "not json, ever"  # reviewer model is broken

    project = _ready_project(config)
    services = Services.build(config, client=FakeLLM(brain))
    note = phase_plan(services, project)
    assert note.startswith("planned 2 work item(s)")
    errors = [e for e in project.journal.iter_events(kinds=["error"])
              if "plan review unavailable" in str(e.payload)]
    assert errors


def test_item_brief_carries_the_full_plan(config, project):
    from engorc.orchestrator.briefs import item_brief

    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    current = WorkItem(title="Core Logic")
    other = WorkItem(title="Renderer", depends_on=[current.id])
    plan = Plan(items=[current, other])
    project.save_plan(plan)
    sections, _ = item_brief(services, project, plan, current, config)
    overview = next(s for s in sections if "full plan" in s.name)
    assert "Core Logic  ← YOUR ITEM" in overview.text
    assert "Renderer" in overview.text
    assert "OUT OF SCOPE" in overview.text


def test_ask_architect_tool_consults_and_journals(config, project):
    ctx = ToolContext(project=project, config=config,
                      journal=Journal(project.root / "journal"),
                      item_id="wi_x", role="implementer",
                      extras={"consult_architect": lambda q: f"answer to: {q}"})
    result = ALL_TOOLS["ask_architect"].run(ctx, {}, "is the renderer my job?")
    assert result.ok and result.output.startswith("ARCHITECT: answer to: is the renderer")
    consults = [e for e in ctx.journal.iter_events(kinds=["decision"])
                if e.payload.get("title") == "architect consult"]
    assert consults

    # no consult wired (e.g. a bespoke context): graceful, not a crash
    bare = ToolContext(project=project, config=config,
                       journal=Journal(project.root / "journal"))
    result = ALL_TOOLS["ask_architect"].run(bare, {}, "anyone there?")
    assert not result.ok and "not available" in result.output
