"""The wrap-note livelock: a dropped dependency froze its dependents, the
build phase claimed "wrapping up" without checking plan completeness, and the
scheduler happily spun on the identical note forever. Each layer now has its
own guard; these tests pin all of them."""

import json

from engorc.llm.fake import FakeLLM
from engorc.orchestrator.phases import phase_build
from engorc.orchestrator.scheduler import Scheduler
from engorc.orchestrator.services import Services
from engorc.plan import Plan, WorkItem
from engorc.registry import Registry


def test_dropped_dependency_unblocks_dependents():
    dropped = WorkItem(title="abandoned approach", status="dropped")
    dependent = WorkItem(title="the follow-up", depends_on=[dropped.id])
    plan = Plan(items=[dropped, dependent])
    assert plan.deps_satisfied(dependent)
    assert [i.id for i in plan.ready_items()] == [dependent.id]

    blocked_dep = WorkItem(title="still failing", status="failed")
    frozen = WorkItem(title="waits on the failure", depends_on=[blocked_dep.id])
    plan = Plan(items=[blocked_dep, frozen])
    assert not plan.deps_satisfied(frozen)


def test_stuck_plan_parks_with_gate_instead_of_wrap_loop(config):
    """Nothing runnable, nothing exhausted, plan incomplete: the old tail set
    phase=wrap and returned a note — but the router re-derived build from the
    incomplete plan, producing an infinite LLM-free spin."""
    project = Registry(config).create("stuck mission", title="S")
    zombie = WorkItem(title="unrunnable leftover", status="blocked")
    project.save_plan(Plan(items=[WorkItem(title="finished", status="done"), zombie]))
    services = Services.build(config, client=FakeLLM(lambda *a: "must not be called"))

    note = phase_build(services, project)
    assert "plan is stuck" in note
    gates = project.gates.open_gates()
    assert len(gates) == 1 and "unrunnable leftover" in gates[0].question
    assert project.meta.state == "blocked_on_user"
    assert not services.client.calls

    # a second pass sees the open gate and parks again — never the wrap note
    project.set_state("active", reason="scheduler pass")
    note = phase_build(services, project)
    assert "waiting on your answers" in note
    assert project.meta.state == "blocked_on_user"


def test_verified_but_unchanged_item_is_marked_done(config):
    """Verification passed but the attempt changed no files: the acceptance was
    already met (e.g. a sibling item's commit swept the file in). The old
    empty-diff guard failed the attempt, re-queuing an item that could never
    produce a diff again — triage diagnosed exactly this in the bug report."""
    from engorc.agents.toolbox.git import commit_all, ensure_repo

    project = Registry(config).create("already done mission", title="A")
    ensure_repo(project.workroom)
    (project.workroom / "index.html").write_text("Hello, world!\n")
    commit_all(project.workroom, "feat: sibling item already produced the file")

    item = WorkItem(title="add the greeting page",
                    verify_commands=['grep -q "Hello, world!" index.html'])
    project.save_plan(Plan(items=[item]))

    finish = 'the file already exists and passes\n\nACTION: finish {"status": "done"}\n```payload\nnothing to change\n```\n'
    client = FakeLLM(lambda *a: finish)
    services = Services.build(config, client=client)

    note = phase_build(services, project)
    assert "already satisfied" in note
    refreshed = project.load_plan().get(item.id)
    assert refreshed.status == "done"
    assert any("already satisfied" in n for n in refreshed.notes)
    # no diff means nothing for the panel to judge — no reviewer calls made
    assert not any(c["schema"] == "ReviewVerdict" for c in client.calls)


def test_verification_failure_still_fails_empty_attempts(config):
    """The other half of the discriminator: no changes AND verification fails
    stays a failed attempt (the 'claimed done but did nothing' case)."""
    from engorc.agents.toolbox.git import commit_all, ensure_repo

    project = Registry(config).create("lazy mission", title="L")
    ensure_repo(project.workroom)
    (project.workroom / "README.md").write_text("stub\n")
    commit_all(project.workroom, "chore: baseline")

    item = WorkItem(title="write the script", verify_commands=["test -f script.py"])
    project.save_plan(Plan(items=[item]))
    finish = 'done!\n\nACTION: finish {"status": "done"}\n```payload\nall set\n```\n'
    services = Services.build(config, client=FakeLLM(lambda *a: finish))

    note = phase_build(services, project)
    assert "verification failed" in note
    refreshed = project.load_plan().get(item.id)
    assert refreshed.status == "todo"
    assert refreshed.attempts[-1].outcome == "fail"


def test_idle_explanation_names_wrapped_and_parked_projects(config, monkeypatch):
    """`orc run` with every project terminal must say WHY it is idle and how
    to proceed, not silently exit ('orc run now does nothing')."""
    import engorc.orchestrator.scheduler as scheduler_module

    messages: list[str] = []
    monkeypatch.setattr(scheduler_module.log, "info", messages.append)
    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    registry = Registry(config)
    done_project = registry.create("finished mission", title="F")
    done_project.set_state("done", reason="mission wrapped")

    Scheduler(services).run(watch=False)
    assert any("wrapped" in m and "orc request" in m for m in messages)

    parked = registry.create("parked mission", title="P")
    parked.set_state("blocked_on_user", reason="loop guard tripped")
    messages.clear()
    Scheduler(services).run(watch=False)
    assert any("parked (loop guard tripped)" in m and "orc resume" in m for m in messages)


def test_scheduler_loop_guard_parks_repeating_project(config, monkeypatch):
    """Belt and braces: whatever future bug produces an identical step note
    forever, the scheduler parks the project instead of spinning."""
    import engorc.orchestrator.scheduler as scheduler_module

    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    project = Registry(config).create("spinning mission", title="Spin")
    monkeypatch.setattr(scheduler_module, "run_step",
                        lambda services, project: "same note every time")

    steps = Scheduler(services).run(watch=False, interactive=False)

    assert steps == 4  # the first note plus three identical repeats
    assert project.meta.state == "blocked_on_user"
    gates = project.gates.open_gates()
    assert len(gates) == 1 and "looping without making progress" in gates[0].question
    errors = [e for e in project.journal.iter_events(kinds=["error"])
              if "loop guard" in json.dumps(e.payload)]
    assert errors
