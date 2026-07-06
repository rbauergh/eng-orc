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

    project.journal.append(
        Kind.REVIEW, item=item.id, verdict="request_changes", findings=3, lens="adversarial",
        model="coder-fast",
        blockers=["BUG/blocker: crashes on empty input → guard it",
                  "BUG/major: wrong exit code → return 1",
                  "SECURITY/major: path traversal → jail the path"],
    )
    project.journal.append(
        Kind.ATTEMPT_FINISHED, actor="tester", item=item.id, status="done",
        summary="tests written",
        handoff=["test_empty_name encodes 'greet handles empty input'",
                 "test_exit_code encodes 'nonzero on failure'"],
    )

    snapshot = gather_snapshot(services)
    assert snapshot.profile == config.models.profile
    assert snapshot.open_gates == 1
    assert any(row[0] == project.root.name for row in snapshot.projects)
    assert snapshot.now and "implement the [core]" in snapshot.now[0].text
    assert any("run_tests" in line and "FAILED" in line for line in snapshot.activity)

    # review blockers and handoff summaries indent under their event lines
    assert any("crashes on empty input" in line for line in snapshot.activity)
    assert any("test_empty_name encodes" in line for line in snapshot.activity)
    # default view caps expansion at 2 and points at --details
    assert any("1 more (orc dashboard --details)" in line for line in snapshot.activity)
    detailed = gather_snapshot(services, details=True)
    assert any("path traversal" in line for line in detailed.activity)

    text = _render_text(services)
    assert project.root.name in text
    assert "implement the [core]" in text        # brackets survive escaping
    assert "question(s) waiting" in text
    # the path itself may be ellipsized on narrow consoles; the data and the
    # label must be present
    assert snapshot.projects_dir == str(config.projects_dir)
    assert "projects live in" in text


def test_dashboard_once_mode_prints_and_exits(config):
    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    Registry(config).create("simple mission", title="Simple")
    console = Console(record=True, width=140, force_terminal=False)
    run_dashboard(services, once=True, console=console)
    out = console.export_text()
    assert "eng-orc" in out and "projects" in out
    assert "gpu timeline" in out


def test_live_layout_shows_project_rows_at_fixed_height(config):
    """Regression: the projects region must be tall enough for its data rows —
    a one-project table was rendering header-only (blank rows cropped)."""
    from engorc.obs.dashboard import _REGIONS, _build_layout

    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    project = Registry(config).create("visible mission", title="Visible")
    snapshot = gather_snapshot(services)
    layout = _build_layout(snapshot)
    for name, builder in _REGIONS.items():
        layout[name].update(builder(snapshot)[1])
    console = Console(record=True, width=120, height=40, force_terminal=False)
    console.print(layout)
    out = console.export_text()
    assert project.root.name in out          # the data row is actually visible
    assert "charter" in out                   # phase column rendered too
