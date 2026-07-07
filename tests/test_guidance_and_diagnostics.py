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


def test_diff_since_sees_new_files_and_unborn_head(tmp_path):
    """Regression: reviewers were judging empty diffs — untracked new files
    never appear in `git diff`, and a fresh repo has no HEAD at all."""
    from engorc.agents.toolbox.git import commit_all, diff_since, ensure_repo, head_sha

    workroom = tmp_path / "workroom"
    workroom.mkdir()
    assert ensure_repo(workroom)
    (workroom / "script.py").write_text("print('hello')\n")

    diff = diff_since(workroom, head_sha(workroom))  # unborn HEAD → ""
    assert "script.py" in diff and "print('hello')" in diff

    ok, sha = commit_all(workroom, "feat: first")
    assert ok
    (workroom / "script.py").write_text("print('changed')\n")
    (workroom / "brand_new.py").write_text("x = 1\n")
    diff = diff_since(workroom, sha)
    assert "brand_new.py" in diff and "print('changed')" in diff


def _triage_brain(payload: dict):
    import json

    def brain(messages, response_format, role_model):
        name = (response_format or {}).get("json_schema", {}).get("name", "")
        assert name == "TriageReport", f"unexpected schema {name}"
        return json.dumps(payload)

    return brain


def test_triage_revise_feeds_new_direction(config):
    from engorc.llm.fake import FakeLLM
    from engorc.orchestrator.phases import _run_triage
    from engorc.orchestrator.services import Services

    project = Registry(config).create("triage mission", title="T")
    item = WorkItem(title="broken thing", status="failed", description="old desc")
    for _ in range(3):
        item.attempts.append(AttemptRecord(role="implementer", outcome="fail",
                                           ended=iso_now(), summary="ImportError: script"))
    plan = Plan(items=[item])
    project.save_plan(plan)

    payload = {
        "reasoning": "the import error shows a wrong module layout",
        "items": [{
            "item_id": item.id, "action": "revise",
            "diagnosis": "attempts failed on ImportError — module layout is wrong",
            "guidance": "put greet() in greet.py at the repo root",
            "new_description": "create greet.py at the root with greet()",
            "new_acceptance": ["import greet works"],
            "new_verify_commands": [], "split_items": [], "question": "",
        }],
        "systemic_notes": ["review model deep-reasoner failed to load"],
    }
    services = Services.build(config, client=FakeLLM(_triage_brain(payload)))
    note = _run_triage(services, project, plan, [item])
    assert "adjusted 1" in note
    refreshed = project.load_plan().get(item.id)
    assert refreshed.status == "todo"
    assert refreshed.attempts == []
    assert refreshed.description.startswith("create greet.py")
    assert any(n.startswith("triage#1") for n in refreshed.notes)
    assert any("triage guidance" in n for n in refreshed.notes)
    systemic = [e for e in project.journal.iter_events(kinds=["error"])
                if "systemic" in str(e.payload)]
    assert systemic


def test_triage_split_rewires_dependents_to_the_tail(config):
    """Regression: a dropped parent counts as a satisfied dependency, so
    items waiting on it ran before the split-off replacements existed — and
    the replacements themselves jumped ahead of the rest of the plan."""
    from engorc.llm.fake import FakeLLM
    from engorc.orchestrator.phases import _run_triage
    from engorc.orchestrator.services import Services

    project = Registry(config).create("split mission", title="S")
    upstream = WorkItem(title="scaffold", status="done")
    parent = WorkItem(title="build & polish", status="failed", depends_on=[upstream.id])
    dependent = WorkItem(title="release notes", depends_on=[parent.id])
    for _ in range(3):
        parent.attempts.append(AttemptRecord(role="implementer", outcome="stuck", ended=iso_now()))
    plan = Plan(items=[upstream, parent, dependent])
    project.save_plan(plan)

    payload = {
        "reasoning": "too big",
        "items": [{
            "item_id": parent.id, "action": "split",
            "diagnosis": "combines assets, spec, build, and verification",
            "guidance": "", "new_description": "", "new_acceptance": [],
            "new_verify_commands": [],
            "split_items": [
                {"title": "generate assets", "kind": "feature", "description": "",
                 "acceptance": [], "verify_commands": [], "depends_on": [],
                 "files_hint": [], "size": "S", "test_first": False},
                {"title": "build executable", "kind": "feature", "description": "",
                 "acceptance": [], "verify_commands": [], "depends_on": [],
                 "files_hint": [], "size": "S", "test_first": False},
            ],
            "question": "",
        }],
        "systemic_notes": [],
    }
    services = Services.build(config, client=FakeLLM(_triage_brain(payload)))
    _run_triage(services, project, plan, [parent])

    refreshed = project.load_plan()
    by_title = {i.title: i for i in refreshed.items}
    assets, build = by_title["generate assets"], by_title["build executable"]
    assert by_title["build & polish"].status == "dropped"
    assert assets.depends_on == [upstream.id]          # inherits the parent's deps
    assert set(build.depends_on) == {upstream.id, assets.id}  # chained after its sibling
    assert by_title["release notes"].depends_on == [build.id]  # rewired to the tail
    # the split work is gated behind the same upstream the parent waited on
    ready = [i.title for i in refreshed.ready_items()]
    assert "generate assets" in ready and "release notes" not in ready


def test_triage_ask_user_opens_informed_gate(config):
    from engorc.llm.fake import FakeLLM
    from engorc.orchestrator.phases import _run_triage
    from engorc.orchestrator.services import Services

    project = Registry(config).create("triage mission 2", title="T2")
    item = WorkItem(title="ambiguous thing", status="failed")
    item.attempts.append(AttemptRecord(role="implementer", outcome="fail", ended=iso_now()))
    plan = Plan(items=[item])
    project.save_plan(plan)
    payload = {
        "reasoning": "requirements contradict",
        "items": [{
            "item_id": item.id, "action": "ask_user",
            "diagnosis": "acceptance wants both CSV and JSON as the only output",
            "guidance": "", "new_description": "", "new_acceptance": [],
            "new_verify_commands": [], "split_items": [],
            "question": "Should the exporter default to CSV or JSON?",
        }],
        "systemic_notes": [],
    }
    services = Services.build(config, client=FakeLLM(_triage_brain(payload)))
    note = _run_triage(services, project, plan, [item])
    assert "question(s) need you" in note
    gates = project.gates.open_gates()
    assert len(gates) == 1
    assert "CSV or JSON" in gates[0].question
    assert "Triage diagnosis" in gates[0].question
    assert project.meta.state == "blocked_on_user"


def test_prompt_gates_answers_by_option_number(config, monkeypatch):
    import engorc.interactive as interactive_module
    from engorc.interactive import prompt_gates
    from engorc.llm.fake import FakeLLM
    from engorc.orchestrator.services import Services

    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    project = Registry(config).create("interactive mission", title="I")
    gate = project.gates.open("CSV or JSON?", from_role="supervisor",
                              options=["CSV", "JSON"])
    other = project.gates.open("second question", from_role="charterer")

    replies = iter(["2", "q"])
    monkeypatch.setattr(interactive_module.Prompt, "ask",
                        staticmethod(lambda *a, **k: next(replies)))
    answered = prompt_gates(services)
    assert answered == 1
    assert project.gates.get(gate.id).answer == "JSON"
    assert project.gates.get(other.id).status == "open"  # quit before it


def test_missing_model_with_near_miss_name_is_identified(tmp_path):
    from engorc.bugreport import _similar_files

    wanted = tmp_path / "Nemotron-3-Nano-30B-A3B-UD-IQ4_NL.gguf"  # config's stale name
    (tmp_path / "Nemotron-3-Nano-30B-A3B-IQ4_NL.gguf").write_bytes(b"gguf")  # what exists
    similar = _similar_files(wanted)
    assert similar == ["Nemotron-3-Nano-30B-A3B-IQ4_NL.gguf"]
    assert _similar_files(tmp_path / "totally-unrelated.gguf") == []


def test_profile_config_drift_detection(tmp_path, config):
    from engorc.bugreport import _profile_config_drift

    live = tmp_path / "live.yaml"
    profile = tmp_path / "profile.yaml"
    profile.write_text("models: {}\n")
    live.write_text("models: {}\n")
    ok, _ = _profile_config_drift(config, live_path=live, profile_path=profile)
    assert ok is True
    live.write_text("models: {stale: true}\n")
    ok, detail = _profile_config_drift(config, live_path=live, profile_path=profile)
    assert ok is False and "DRIFTED" in detail
    ok, _ = _profile_config_drift(config, live_path=tmp_path / "absent.yaml", profile_path=profile)
    assert ok is None


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
