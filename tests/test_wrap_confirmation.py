"""Wrap sign-off: a model may DROP an item, but only the user closes a
mission that ships without it. Pins the gate, the affirmative path (honest
final report), and the revive path (guidance returns items to the queue)."""

import json

from engorc.llm.fake import FakeLLM
from engorc.orchestrator.phases import WRAP_CONFIRMED_NOTE, phase_wrap
from engorc.orchestrator.services import Services
from engorc.orchestrator.supervisor import next_phase
from engorc.plan import Plan, WorkItem
from engorc.registry import Registry


def _wrap_brain(messages, response_format, role_model):
    name = (response_format or {}).get("json_schema", {}).get("name", "")
    if name == "DigestExtract":
        return json.dumps({"reasoning": "", "summary": "shipped the mission",
                           "lessons": [], "conventions": []})
    return "a fine session"


def _project_with_drop(config):
    from engorc.fsio import atomic_write_yaml

    project = Registry(config).create("wrap mission", title="W")
    atomic_write_yaml(project.charter_path, {"ready_to_build": True})
    project.design_path.write_text("# design\n")
    dropped = WorkItem(title="the clock template", status="dropped")
    project.save_plan(Plan(items=[WorkItem(title="the app", status="done"), dropped]))
    return project, dropped


def test_wrap_parks_for_signoff_on_dropped_items(config):
    project, _ = _project_with_drop(config)
    services = Services.build(config, client=FakeLLM(lambda *a: "must not be called"))

    note = phase_wrap(services, project)
    assert "need your sign-off" in note
    gates = project.gates.open_gates()
    assert len(gates) == 1 and "the clock template" in gates[0].question
    assert gates[0].options == ["finish without them", "revive them and continue"]
    assert project.meta.state == "blocked_on_user"
    assert not services.client.calls  # no digest before the human decides

    # a second pass reuses the open gate instead of stacking duplicates
    note = phase_wrap(services, project)
    assert "need your sign-off" in note
    assert len(project.gates.open_gates()) == 1


def test_affirmative_signoff_wraps_with_honest_report(config):
    project, dropped = _project_with_drop(config)
    services = Services.build(config, client=FakeLLM(_wrap_brain))
    phase_wrap(services, project)
    gate = project.gates.open_gates()[0]
    project.gates.answer(gate.id, "finish without them")
    project.set_state("active", reason="user answered")

    note = phase_wrap(services, project)
    assert "1 dropped" in note
    assert project.meta.state == "done"
    refreshed = project.load_plan().get(dropped.id)
    assert refreshed.status == "dropped"
    assert WRAP_CONFIRMED_NOTE in refreshed.notes
    report = project.artifacts.read("report.md")
    assert "1 dropped" in report and "the clock template" in report


def test_guidance_revives_dropped_items(config):
    project, dropped = _project_with_drop(config)
    services = Services.build(config, client=FakeLLM(_wrap_brain))
    phase_wrap(services, project)
    gate = project.gates.open_gates()[0]
    project.gates.answer(gate.id, "the clock is the whole point — build templates/index.html")
    project.set_state("active", reason="user answered")

    note = phase_wrap(services, project)
    assert "revived 1" in note
    refreshed = project.load_plan().get(dropped.id)
    assert refreshed.status == "todo"
    assert refreshed.attempts == []
    assert any("user guidance: the clock is the whole point" in n for n in refreshed.notes)
    assert next_phase(project) == "build"  # plan incomplete again → back to work
    assert not project.gates.open_gates()


def test_drop_worded_answers_confirm_the_wrap():
    """Regression: the user answered 'Drop them' at the wrap gate and the
    parser read it as revive-guidance — dropping IS finishing-without."""
    from engorc.orchestrator.phases import _is_affirmative

    assert _is_affirmative("Drop them")
    assert _is_affirmative("skip these, the replacements delivered")
    assert _is_affirmative("finish without them")
    assert not _is_affirmative("revive them and continue")
    assert not _is_affirmative("the clock template matters — build it")


def test_wrap_rebuilds_shipped_artifacts_every_cycle(config):
    """The 'builder' stage: build_commands re-run at wrap (the run IS the
    regeneration), so a bug fix can never ship a stale dist/."""
    from engorc.fsio import atomic_write_yaml

    project = Registry(config).create("shipped mission", title="S")
    atomic_write_yaml(project.charter_path, {
        "ready_to_build": True,
        "build_commands": ["printf built > dist.out"],
    })
    project.design_path.write_text("# design\n")
    project.save_plan(Plan(items=[WorkItem(title="only item", status="done")]))
    services = Services.build(config, client=FakeLLM(_wrap_brain))
    note = phase_wrap(services, project)
    assert "1/1 items done" in note
    assert (project.workroom / "dist.out").read_text() == "built"  # regenerated


def test_failing_build_pushes_back_to_the_implementer(config):
    from engorc.fsio import atomic_write_yaml
    from engorc.orchestrator.supervisor import next_phase

    project = Registry(config).create("broken build mission", title="B")
    atomic_write_yaml(project.charter_path, {
        "ready_to_build": True,
        "build_commands": ["false"],
    })
    project.design_path.write_text("# design\n")
    project.save_plan(Plan(items=[WorkItem(title="only item", status="done")]))
    services = Services.build(config, client=FakeLLM(lambda *a: "must not be called"))
    note = phase_wrap(services, project)
    assert "build failed at wrap" in note and "Fix the build" in note
    plan = project.load_plan()
    fix = next(i for i in plan.items if i.title == "Fix the build")
    assert fix.kind == "fix" and fix.verify_commands == ["false"]
    assert "exit 0" in fix.acceptance[0]
    assert next_phase(project) == "build"  # the cycle reopens

    # a second wrap visit does NOT stack duplicate fix items
    note = phase_wrap(services, project)
    assert "already queued" in note
    assert sum(i.title == "Fix the build" for i in project.load_plan().items) == 1


def test_wrap_without_drops_needs_no_signoff(config):
    project = Registry(config).create("clean mission", title="C")
    project.save_plan(Plan(items=[WorkItem(title="only item", status="done")]))
    services = Services.build(config, client=FakeLLM(_wrap_brain))
    note = phase_wrap(services, project)
    assert "1/1 items done" in note and "dropped" not in note
    assert project.meta.state == "done"
