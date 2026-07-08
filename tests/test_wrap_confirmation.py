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


def test_wrap_without_drops_needs_no_signoff(config):
    project = Registry(config).create("clean mission", title="C")
    project.save_plan(Plan(items=[WorkItem(title="only item", status="done")]))
    services = Services.build(config, client=FakeLLM(_wrap_brain))
    note = phase_wrap(services, project)
    assert "1/1 items done" in note and "dropped" not in note
    assert project.meta.state == "done"
