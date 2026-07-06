"""Live terminal dashboard (`orc dashboard`): what the orchestrator is doing,
at a glance, refreshed in place like top.

A pure reader: everything comes from the filesystem (journals, plans, gates)
plus two cheap probes (llama-swap /running for resident/loading models,
nvidia-smi for GPU load). Run it in a second terminal next to
`orc run --watch`; it holds no state and can't interfere with the work.

Flicker discipline: the live view runs on the alternate screen with fixed
region geometry, auto-refresh off, and per-panel change keys — a panel is
repainted only when its content actually changed, so an idle system draws
nothing at all.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field

from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from ..events import Event, Kind
from ..util import human_age, shorten


def _turn_line(e: Event) -> str:
    suffix = "" if e.payload.get("ok") else " · FAILED"
    return f"{e.actor}: turn {e.payload.get('turn')} · {e.payload.get('tool')}{suffix}"


EVENT_LINES = {
    Kind.STEP: lambda e: f"step [{e.payload.get('phase')}] {e.payload.get('note', '')}",
    Kind.AGENT_TURN: _turn_line,
    Kind.ATTEMPT_STARTED: lambda e: f"{e.payload.get('role')} started on {e.item}",
    Kind.ATTEMPT_FINISHED: lambda e: (
        f"{e.actor} {e.payload.get('status')}: {shorten(e.payload.get('summary', ''), 70)}"
    ),
    Kind.VERIFY_RUN: lambda e: f"verify: {'PASS' if e.payload.get('passed') else 'FAIL'}",
    Kind.REVIEW: lambda e: (
        f"review[{e.payload.get('lens', '?')}] {e.payload.get('verdict')} "
        f"({e.payload.get('findings', 0)} findings)"
    ),
    Kind.ITEM_STATUS: lambda e: f"item {e.item} → {e.payload.get('status')}",
    Kind.COMMIT: lambda e: f"commit {e.payload.get('sha', '')} {shorten(e.payload.get('message', ''), 50)}",
    Kind.GATE_OPENED: lambda e: f"ASKED: {shorten(e.payload.get('question', ''), 70)}",
    Kind.GATE_ANSWERED: lambda e: f"answered: {shorten(e.payload.get('answer', ''), 60)}",
    Kind.PHASE_ENTERED: lambda e: f"phase → {e.payload.get('phase')}",
    Kind.PROJECT_STATE: lambda e: f"state → {e.payload.get('state')}",
    Kind.INDEX_REFRESH: lambda e: f"index refreshed ({e.payload.get('nodes_upserted', 0)} chunks)",
    Kind.ERROR: lambda e: f"ERROR: {shorten(str(e.payload.get('error', '')), 80)}",
}


@dataclass
class NowLine:
    slug: str
    text: str


@dataclass
class Snapshot:
    profile: str
    home: str
    projects_dir: str
    server_ok: bool
    server_url: str
    resident: list[str] = field(default_factory=list)  # "model (state)"
    gpu: str = ""
    memory_backend: str = ""
    projects: list[tuple[str, ...]] = field(default_factory=list)
    now: list[NowLine] = field(default_factory=list)
    activity: list[str] = field(default_factory=list)
    open_gates: int = 0


def _gpu_line() -> str:
    if shutil.which("nvidia-smi") is None:
        return ""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        parts = [p.strip() for p in proc.stdout.split(",")]
        if len(parts) >= 3:
            used_gib = float(parts[1]) / 1024
            total_gib = float(parts[2]) / 1024
            return f"GPU {parts[0]}% · VRAM {used_gib:.1f}/{total_gib:.1f} GiB"
    except Exception:
        pass
    return ""


def _current_attempt_line(project, plan) -> NowLine | None:
    active = [item for item in plan.items if item.status == "in_progress"]
    if not active:
        return None
    item = active[0]
    attempt = item.last_attempt()
    turns = [e for e in project.journal.tail(60, kinds=[Kind.AGENT_TURN]) if e.item == item.id]
    last_turn = turns[-1] if turns else None
    parts = [f"{attempt.role if attempt else '?'} on “{shorten(item.title, 46)}”"]
    if attempt:
        parts.append(f"model {attempt.model}")
        parts.append(f"attempt {len(item.attempts)}")
    if last_turn is not None:
        parts.append(
            f"turn {last_turn.payload.get('turn')} · {last_turn.payload.get('tool')}"
            + ("" if last_turn.payload.get("ok") else " (failed)")
        )
    return NowLine(slug=project.root.name, text=" · ".join(parts))


def gather_snapshot(services) -> Snapshot:
    config = services.config
    snapshot = Snapshot(
        profile=config.models.profile,
        home=str(config.home),
        projects_dir=str(config.projects_dir),
        server_ok=services.client.health(),
        server_url=config.server.base_url,
        gpu=_gpu_line(),
    )
    try:
        snapshot.memory_backend = services.memory.health()[1]
    except Exception:
        snapshot.memory_backend = "unknown"
    for entry in services.swap.running_models():
        model = entry.get("model")
        if model:
            state = (entry.get("state") or "").lower() or "ready"
            snapshot.resident.append(f"{model} ({state})")

    merged: list[tuple[str, str, Event]] = []
    for project in services.registry.all_projects():
        try:
            meta = project.meta
        except FileNotFoundError:
            continue
        plan = project.load_plan()
        progress = plan.progress()
        gates = project.gates.open_gates()
        snapshot.open_gates += len(gates)
        snapshot.projects.append((
            meta.slug,
            str(meta.priority),
            meta.phase,
            meta.state,
            f"{progress.get('done', 0)}/{progress.get('total', 0)}" if progress.get("total") else "—",
            str(len(gates)) if gates else "-",
            human_age(meta.last_active),
        ))
        now_line = _current_attempt_line(project, plan)
        if now_line is not None and meta.state == "active":
            snapshot.now.append(now_line)
        for event in project.journal.tail(20):
            merged.append((event.ts, meta.slug, event))

    merged.sort(key=lambda entry: entry[0])
    for ts, slug, event in merged[-14:]:
        formatter = EVENT_LINES.get(event.kind)
        if formatter is None:
            continue
        snapshot.activity.append(f"{ts[11:19]} {slug} · {formatter(event)}")
    return snapshot


# ---------------------------------------------------------------------- panels
# Each builder returns (change_key, renderable). The key is compared between
# frames; the panel is only repainted when it differs.


def _header_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    resident = ", ".join(s.resident) or "(nothing loaded)"
    bits = [
        f"profile [bold]{escape(s.profile)}[/bold]",
        ("[green]server ok[/green]" if s.server_ok
         else f"[red]server DOWN[/red] {escape(s.server_url)}"),
        f"resident: {escape(resident)}",
    ]
    if s.gpu:
        bits.append(escape(s.gpu))
    key = (s.profile, s.server_ok, tuple(s.resident), s.gpu)
    return key, Panel(" · ".join(bits), title="eng-orc", title_align="left")


def _projects_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    if not s.projects:
        return ("empty",), Panel('no projects — orc new "<goal>"', title="projects", title_align="left")
    table = Table(expand=True, show_edge=False, pad_edge=False)
    for column in ("project", "pri", "phase", "state", "plan", "gates", "active"):
        table.add_column(column)
    for row in s.projects:
        table.add_row(*(escape(cell) for cell in row))
    return tuple(s.projects), Panel(table, title="projects", title_align="left")


def _now_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    text = "\n".join(
        f"[bold]{escape(line.slug)}[/bold] · {escape(line.text)}" for line in s.now
    ) or "[dim]idle — no attempt in flight[/dim]"
    key = tuple((line.slug, line.text) for line in s.now)
    return key, Panel(text, title="working now", title_align="left")


def _activity_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    text = "\n".join(escape(line) for line in s.activity) or "[dim](no activity yet)[/dim]"
    return tuple(s.activity), Panel(text, title="recent activity", title_align="left")


def _footer_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    bits = [f"projects live in {escape(s.projects_dir)}"]
    if s.open_gates:
        bits.append(f"[yellow]{s.open_gates} question(s) waiting — orc inbox[/yellow]")
    bits.append(f"memory: {escape(shorten(s.memory_backend, 50))}")
    key = (s.projects_dir, s.open_gates, s.memory_backend)
    return key, Panel(" · ".join(bits) + " · Ctrl-C exits", title_align="left")


_REGIONS = {
    "header": _header_panel,
    "projects": _projects_panel,
    "now": _now_panel,
    "activity": _activity_panel,
    "footer": _footer_panel,
}


def render(snapshot: Snapshot) -> Group:
    """One-shot rendering (orc dashboard --once, tests, non-tty fallback)."""
    return Group(*(builder(snapshot)[1] for builder in _REGIONS.values()))


def _build_layout(snapshot: Snapshot) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="projects", size=_projects_size(snapshot)),
        Layout(name="now", size=_now_size(snapshot)),
        Layout(name="activity"),  # takes the remaining height
        Layout(name="footer", size=3),
    )
    return layout


def _projects_size(s: Snapshot) -> int:
    return min(max(len(s.projects), 1), 10) + 3  # rows + header line + borders


def _now_size(s: Snapshot) -> int:
    return min(max(len(s.now), 1), 4) + 2


def run_dashboard(services, interval: float = 2.0, once: bool = False,
                  console: Console | None = None) -> None:
    console = console or Console()
    if once or not console.is_terminal:
        console.print(render(gather_snapshot(services)))
        return

    snapshot = gather_snapshot(services)
    layout = _build_layout(snapshot)
    keys: dict[str, tuple] = {}
    sizes = (_projects_size(snapshot), _now_size(snapshot))

    def apply(s: Snapshot) -> bool:
        nonlocal sizes
        changed = False
        new_sizes = (_projects_size(s), _now_size(s))
        if new_sizes != sizes:  # geometry changes are rare (project count moved)
            sizes = new_sizes
            layout["projects"].size, layout["now"].size = new_sizes
            keys.clear()  # force full repaint into the new geometry
            changed = True
        for name, builder in _REGIONS.items():
            key, renderable = builder(s)
            if keys.get(name) != key:
                keys[name] = key
                layout[name].update(renderable)
                changed = True
        return changed

    apply(snapshot)
    try:
        # Alternate screen + manual refresh: repaint happens only when a panel's
        # content key changed — an idle dashboard is a perfectly still picture.
        with Live(layout, console=console, screen=True, auto_refresh=False) as live:
            live.refresh()
            while True:
                time.sleep(max(0.5, interval))
                if apply(gather_snapshot(services)):
                    live.refresh()
    except KeyboardInterrupt:
        pass
    console.print("dashboard closed — the orchestrator keeps running")
