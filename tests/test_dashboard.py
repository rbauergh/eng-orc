"""Dashboard snapshot/render: correct content, bracket-safe, no server needed."""

from rich.console import Console

from engorc.events import Kind
from engorc.llm.fake import FakeLLM
from engorc.obs.dashboard import gather_snapshot, render, run_dashboard
from engorc.orchestrator.services import Services
from engorc.plan import AttemptRecord, Plan, WorkItem
from engorc.registry import Registry


def _render_text(services) -> str:
    console = Console(record=True, width=140, force_terminal=False)
    console.print(render(gather_snapshot(services)))
    return console.export_text()


def test_snapshot_covers_projects_now_and_activity(config):
    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    registry = Registry(config)
    project = registry.create("build a [weird] thing", title="Weird [title]")
    item = WorkItem(title="implement the [core] module")
    item.status = "in_progress"
    item.attempts.append(AttemptRecord(role="implementer", model="coder"))
    project.save_plan(Plan(items=[item]))
    project.journal.append(Kind.AGENT_TURN, actor="implementer", item=item.id, turn=3,
                           tool="run_tests", ok=False)
    project.journal.append(Kind.STEP, phase="build", note="working on the [core] module")
    project.gates.open("pick a [database]?", from_role="charterer")

    snapshot = gather_snapshot(services)
    assert snapshot.profile == config.models.profile
    assert snapshot.open_gates == 1
    assert any(row[0] == project.root.name for row in snapshot.projects)
    assert snapshot.now and "implement the [core]" in snapshot.now[0].text
    assert any("run_tests" in line and "FAILED" in line for line in snapshot.activity)

    text = _render_text(services)
    assert project.root.name in text
    assert "implement the [core]" in text        # brackets survive escaping
    assert "question(s) waiting" in text
    assert str(config.projects_dir) in text


def test_dashboard_once_mode_prints_and_exits(config):
    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    Registry(config).create("simple mission", title="Simple")
    console = Console(record=True, width=140, force_terminal=False)
    run_dashboard(services, once=True, console=console)
    out = console.export_text()
    assert "eng-orc" in out and "projects" in out
