"""Project intake conversations and natural continuation via orc request."""

import json

from engorc.llm.fake import FakeLLM
from engorc.orchestrator.phases import plan_request
from engorc.orchestrator.services import Services
from engorc.plan import Plan, WorkItem
from engorc.registry import Registry


def _intake_brain():
    """Round 1 asks a question; round 2 honors a deferred answer by deciding."""

    def brain(messages, response_format, role_model):
        name = (response_format or {}).get("json_schema", {}).get("name", "")
        assert name == "IntakeTurn"
        deferred = "User answered: whatever you want" in messages[-1]["content"]
        if not deferred:
            return json.dumps({
                "reasoning": "need the platform decision",
                "title": "Note keeper",
                "spec_markdown": "# Note keeper\n## Objective\nKeep notes.\n## Open points\n- language",
                "question": "What language should this be written in?",
                "ready": False,
            })
        return json.dumps({
            "reasoning": "user deferred; deciding",
            "title": "Note keeper",
            "spec_markdown": ("# Note keeper\n## Objective\nKeep notes.\n## Technical notes\n"
                              "- Python 3.12 (chosen: fits the toolchain, user deferred)\n"),
            "question": "",
            "ready": True,
        })

    return brain


def test_intake_defers_to_model_judgment(config, monkeypatch):
    import engorc.intake as intake_module
    from engorc.intake import create_project_from_intake, run_intake

    services = Services.build(config, client=FakeLLM(_intake_brain()))
    replies = iter(["whatever you want", "y"])
    monkeypatch.setattr(intake_module, "read_answer", lambda *a, **k: next(replies))
    result = run_intake(services)
    assert result is not None
    assert "Python 3.12 (chosen" in result.spec_markdown  # the model decided
    project = create_project_from_intake(services, result)
    assert "Python 3.12" in project.mission()
    assert project.artifacts.exists("intake-conversation.md")
    assert "whatever you want" in project.artifacts.read("intake-conversation.md")


def test_intake_quit_creates_nothing(config, monkeypatch):
    import engorc.intake as intake_module
    from engorc.intake import run_intake

    services = Services.build(config, client=FakeLLM(_intake_brain()))
    monkeypatch.setattr(intake_module, "read_answer", lambda *a, **k: "quit")
    assert run_intake(services) is None
    assert services.registry.slugs() == []


def _request_brain():
    def brain(messages, response_format, role_model):
        name = (response_format or {}).get("json_schema", {}).get("name", "")
        assert name == "PlanDraft"
        assert "New request from the user" in messages[-1]["content"]
        return json.dumps({
            "reasoning": "one focused fix",
            "goal_recap": "handle empty input",
            "items": [{
                "title": "Fix crash on empty input",
                "kind": "fix",
                "description": "guard the empty-string path",
                "acceptance": ["empty input returns a friendly message"],
                "verify_commands": [],
                "depends_on": [],
                "files_hint": ["script.py"],
                "size": "S",
                "test_first": True,
            }],
        })

    return brain


def test_request_extends_plan_and_reactivates_done_project(config):
    services = Services.build(config, client=FakeLLM(_request_brain()))
    project = Registry(config).create("original mission", title="Orig")
    done_item = WorkItem(title="ship v1", status="done")
    project.save_plan(Plan(items=[done_item]))
    project.set_phase("done")
    project.set_state("done", reason="wrapped")

    note = plan_request(services, project, "bug: it crashes when I pass an empty string")
    assert "queued 1 new item" in note
    plan = project.load_plan()
    assert len(plan.items) == 2
    new_item = next(i for i in plan.items if i.status == "todo")
    assert new_item.title.startswith("Fix crash")
    assert any(n.startswith("request:") for n in new_item.notes)
    assert "test_first" in new_item.notes  # TDD applies to requests too
    meta = project.meta
    assert meta.phase == "build" and meta.state == "active"
