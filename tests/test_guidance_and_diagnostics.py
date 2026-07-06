"""Stuck-item guidance flow and the model-file preflight from the bug report."""

import pytest

from engorc.agents.runtime import FormatError, parse_action
from engorc.bugreport import _llama_swap_model_files
from engorc.orchestrator.phases import _absorb_supervisor_guidance
from engorc.orchestrator.supervisor import next_phase
from engorc.plan import AttemptRecord, Plan, WorkItem
from engorc.registry import Registry
from engorc.util import iso_now


def test_supervisor_answer_resets_stuck_items_with_guidance(config):
    project = Registry(config).create("mission", title="M")
    item = WorkItem(title="stubborn thing", status="failed")
    for _ in range(3):
        item.attempts.append(AttemptRecord(role="implementer", outcome="fail",
                                           ended=iso_now(), summary="hit the same wall"))
    plan = Plan(items=[item])
    project.save_plan(plan)
    gate = project.gates.open("I am stuck: ...", from_role="supervisor", phase="build")
    project.gates.answer(gate.id, "drop the CLI part, just make greet() work")

    assert _absorb_supervisor_guidance(project, plan)
    refreshed = project.load_plan().get(item.id)
    assert refreshed.status == "todo"
    assert refreshed.attempts == []
    assert any("user guidance: drop the CLI part" in note for note in refreshed.notes)
    assert any("earlier attempt (implementer)" in note for note in refreshed.notes)
    assert gate.id in project.consumed_gate_ids()
    # a second pass with nothing new is a no-op
    assert not _absorb_supervisor_guidance(project, project.load_plan())


def test_supervisor_answers_do_not_reroute_to_charter(config):
    project = Registry(config).create("mission two", title="M2")
    from engorc.fsio import atomic_write_yaml

    atomic_write_yaml(project.charter_path, {"ready_to_build": True})
    project.design_path.write_text("# design\n")
    project.save_plan(Plan(items=[WorkItem(title="x")]))
    gate = project.gates.open("stuck", from_role="supervisor")
    project.gates.answer(gate.id, "simplify")
    assert next_phase(project) == "build"  # not a charter revision
    charter_gate = project.gates.open("web or cli?", from_role="charterer")
    project.gates.answer(charter_gate.id, "cli")
    assert next_phase(project) == "charter"


def test_empty_reply_gets_thinking_budget_hint():
    with pytest.raises(FormatError) as exc:
        parse_action("<think>endless pondering that never terminates")
    assert "output budget" in str(exc.value)


def test_llama_swap_model_file_parsing(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "macros:\n"
        '  srv: "${env.HOME}/llama.cpp/build/bin/llama-server --port ${PORT}"\n'
        '  models: "' + str(tmp_path) + '"\n'
        "models:\n"
        "  present:\n"
        '    cmd: "${srv} -m ${models}/real.gguf --jinja"\n'
        "  missing:\n"
        '    cmd: "${srv} -m ${models}/ghost.gguf --jinja"\n'
    )
    (tmp_path / "real.gguf").write_bytes(b"gguf")
    pairs = dict(_llama_swap_model_files(config))
    assert pairs["present"].exists()
    assert not pairs["missing"].exists()
    assert pairs["missing"].name == "ghost.gguf"
