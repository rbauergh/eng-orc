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
    assert "gpu" in out


def _snapshot(**overrides):
    from engorc.obs.dashboard import Snapshot

    base = dict(profile="p", home="h", projects_dir="d", server_ok=True, server_url="u",
                gpu_live="idle", gpu_io="completed calls last 5m: 0 tok in / 0 tok out")
    base.update(overrides)
    return Snapshot(**base)


def test_gpu_panel_draws_gauges_procs_and_load_bars():
    from engorc.obs.dashboard import _gpu_panel, _gpu_size

    snapshot = _snapshot(
        gpu_stats={"util": 42, "used_gib": 9.2, "total_gib": 12.0},
        gpu_procs=[("llama-server", 8.9), ("Xwayland", 0.3)],
        gpu_spark="▂▄█",
        gpu_loading=["loading nemotron ━━━━━━━━━╌╌╌╌╌╌╌╌╌╌╌ 48% (21s of ~44s typical)"],
        gpu_events=["10:41:03 qwen loaded in 25s"],
    )
    _, panel = _gpu_panel(snapshot)
    text = panel.renderable.plain
    assert "GPU  ▕████████░░░░░░░░░░░░▏  42%" in text and "▂▄█" in text
    assert "VRAM ▕███████████████░░░░░▏ 9.2/12.0 GiB" in text
    assert "llama-server 8.9 GiB" in text and "+1 more, 0.3 GiB" in text
    assert "loading nemotron" in text and "48%" in text
    # region height: borders 2 + gauges 2 + now 1 + 1 load bar + 1 event
    assert _gpu_size(snapshot) == 7
    # a GPU-less box (tests, CI) skips the gauges and shrinks accordingly
    assert _gpu_size(_snapshot(gpu_events=["e"])) == 4


def test_snapshot_builds_load_progress_from_the_timeline(config):
    services = Services.build(config, client=FakeLLM(lambda *a: "unused"))
    # seed history so the load in progress gets a percent bar, then a live load
    for _ in range(2):
        services.timeline.observe([{"model": "qwen", "state": "starting"}])
        services.timeline.observe([{"model": "qwen", "state": "ready"}])
        services.timeline.observe([])
    import json

    (config.home / "gpu-timeline.jsonl").write_text(json.dumps(
        {"ts": "2026-07-07T10:00:00+00:00", "model": "qwen",
         "event": "ready", "load_seconds": 30.0}) + "\n")
    services.timeline.observe([{"model": "qwen", "state": "starting"}])

    snapshot = gather_snapshot(services)
    assert snapshot.gpu_loading and "loading qwen" in snapshot.gpu_loading[0]
    assert "of ~30s typical" in snapshot.gpu_loading[0]


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
