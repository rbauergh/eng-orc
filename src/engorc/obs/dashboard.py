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
from datetime import timedelta

from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..events import Event, Kind
from ..util import human_age, human_duration, shorten, utc_now


def _nowrap(markup: str) -> Text:
    """Fixed-height panels must never wrap: a wrapped line would push the
    newest content out of the cropped region. Ellipsize instead."""
    text = Text.from_markup(markup)
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


def _turn_line(e: Event) -> str:
    suffix = "" if e.payload.get("ok") else " · FAILED"
    return f"{e.actor}: turn {e.payload.get('turn')} · {e.payload.get('tool')}{suffix}"


EVENT_LINES = {
    Kind.STEP: lambda e: f"step [{e.payload.get('phase')}] {e.payload.get('note', '')}",
    Kind.AGENT_TURN: _turn_line,
    Kind.ATTEMPT_STARTED: lambda e: f"{e.actor} started on {e.item}",
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
    Kind.ERROR: lambda e: (
        f"ERROR[{e.actor}]: {shorten(str(e.payload.get('error', '')), 80)}"
        if e.actor != "system" else f"ERROR: {shorten(str(e.payload.get('error', '')), 80)}"
    ),
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
    gpu: str = ""
    memory_backend: str = ""
    gpu_current: list[dict] = field(default_factory=list)  # model/state/for_seconds
    gpu_events: list[str] = field(default_factory=list)  # described timeline entries
    gpu_live: str = ""  # "generating (~N tok)" | "idle"
    gpu_io: str = ""  # token I/O over the last 5 minutes
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


def gather_snapshot(services, details: bool = False) -> Snapshot:
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

    # -- gpu: residency, swap history, and live token flow ------------------
    services.observe_gpu()
    snapshot.gpu_current = services.timeline.current()
    snapshot.gpu_events = [services.timeline.describe(e) for e in services.timeline.recent(4)]
    busy_bits: list[str] = []
    for entry in snapshot.gpu_current:
        if entry["state"] != "ready":
            continue
        activity = services.swap.slot_activity(entry["model"])
        if activity and activity[0] > 0:
            busy_bits.append(f"{entry['model']}: generating (~{activity[1]} tok in flight)")
    snapshot.gpu_live = " · ".join(busy_bits) or "idle"

    tokens_in = tokens_out = 0
    cutoff = (utc_now() - timedelta(minutes=5)).isoformat(timespec="seconds")

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
        for event in project.journal.iter_events(
            kinds=[Kind.AGENT_TURN, Kind.STRUCTURED_CALL], since_ts=cutoff
        ):
            tokens_in += int(event.payload.get("prompt_tokens", 0) or 0)
            tokens_out += int(event.payload.get("completion_tokens", 0) or 0)

    snapshot.gpu_io = f"last 5m: {tokens_in:,} tok in / {tokens_out:,} tok out"
    merged.sort(key=lambda entry: entry[0])
    expand_limit = None if details else 2
    for ts, slug, event in merged[-16:]:
        formatter = EVENT_LINES.get(event.kind)
        if formatter is None:
            continue
        snapshot.activity.append(f"{ts[11:19]} {slug} · {formatter(event)}")
        # reviews and finished attempts carry their substance in the payload —
        # findings and handoff summaries indent under the event line
        sublines: list[str] = []
        if event.kind == Kind.REVIEW:
            sublines = list(event.payload.get("blockers") or [])
        elif event.kind == Kind.ATTEMPT_FINISHED:
            sublines = list(event.payload.get("handoff") or [])
        shown = sublines if expand_limit is None else sublines[:expand_limit]
        for subline in shown:
            snapshot.activity.append(f"         ↳ {shorten(str(subline), 150 if details else 110)}")
        if expand_limit is not None and len(sublines) > expand_limit:
            snapshot.activity.append(
                f"         ↳ … {len(sublines) - expand_limit} more (orc dashboard --details)"
            )
    snapshot.activity = snapshot.activity[-(22 if details else 14):]
    return snapshot


# ---------------------------------------------------------------------- panels
# Each builder returns (change_key, renderable). The key is compared between
# frames; the panel is only repainted when it differs.


def _header_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    resident = ", ".join(
        f"{e['model']} ({e['state']} {human_duration(e['for_seconds'])})" for e in s.gpu_current
    ) or "(nothing loaded)"
    bits = [
        f"profile [bold]{escape(s.profile)}[/bold]",
        ("[green]server ok[/green]" if s.server_ok
         else f"[red]server DOWN[/red] {escape(s.server_url)}"),
        f"resident: {escape(resident)}",
    ]
    if s.gpu:
        bits.append(escape(s.gpu))
    # residency in the key is minute-coarse so the header repaints ~once a
    # minute while idle instead of every tick
    key = (s.profile, s.server_ok, s.gpu,
           tuple((e["model"], e["state"], int(e["for_seconds"] // 60)) for e in s.gpu_current))
    return key, Panel(_nowrap(" · ".join(bits)), title="eng-orc", title_align="left")


def _gpu_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    lines = [f"[bold]now[/bold]: {escape(s.gpu_live)} · {escape(s.gpu_io)}"]
    lines += [escape(line) for line in s.gpu_events]
    if len(lines) == 1 and not s.gpu_events:
        lines.append("[dim](no swaps observed yet)[/dim]")
    key = (s.gpu_live, s.gpu_io, tuple(s.gpu_events))
    return key, Panel(_nowrap("\n".join(lines)), title="gpu timeline", title_align="left")


def _projects_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    if not s.projects:
        return ("empty",), Panel('no projects — orc new "<goal>"', title="projects", title_align="left")
    # box=None: a header-separator line would not fit the fixed region height;
    # no_wrap columns: a wrapped row would crop the rows below it
    table = Table(expand=True, box=None, pad_edge=False)
    for column in ("project", "pri", "phase", "state", "plan", "gates", "active"):
        table.add_column(column, no_wrap=True, overflow="ellipsis")
    for row in s.projects:
        table.add_row(*(escape(cell) for cell in row))
    return tuple(s.projects), Panel(table, title="projects", title_align="left")


def _idle_reason(s: Snapshot) -> str:
    """An idle GPU should always come with its explanation."""
    if s.open_gates:
        return (f"[yellow]idle — waiting on YOU: {s.open_gates} open question(s) "
                f"(orc inbox → orc answer)[/yellow]")
    if not s.projects:
        return '[dim]idle — no projects (orc new "<goal>")[/dim]'
    states = {row[3] for row in s.projects}
    if "active" not in states:
        return f"[dim]idle — no active projects ({', '.join(sorted(states))})[/dim]"
    return "[dim]idle — active projects but no attempt in flight (is `orc run --watch` running?)[/dim]"


def _now_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    text = "\n".join(
        f"[bold]{escape(line.slug)}[/bold] · {escape(line.text)}" for line in s.now
    ) or _idle_reason(s)
    key = tuple((line.slug, line.text) for line in s.now) or (text,)
    return key, Panel(_nowrap(text), title="working now", title_align="left")


def _activity_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    text = "\n".join(escape(line) for line in s.activity) or "[dim](no activity yet)[/dim]"
    return tuple(s.activity), Panel(_nowrap(text), title="recent activity", title_align="left")


def _footer_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    # ordered by importance: the ellipsis eats from the right, so the
    # truncatable path goes last and the call-to-action goes first
    bits = []
    if s.open_gates:
        bits.append(f"[yellow]{s.open_gates} question(s) waiting — orc inbox[/yellow]")
    bits.append(f"memory: {escape(shorten(s.memory_backend, 40))}")
    bits.append(f"projects live in {escape(s.projects_dir)}")
    key = (s.projects_dir, s.open_gates, s.memory_backend)
    return key, Panel(_nowrap(" · ".join(bits) + " · Ctrl-C exits"), title_align="left")


_REGIONS = {
    "header": _header_panel,
    "gpu": _gpu_panel,
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
        Layout(name="gpu", size=7),  # now-line + up to 4 timeline entries
        Layout(name="projects", size=_projects_size(snapshot)),
        Layout(name="now", size=_now_size(snapshot)),
        Layout(name="activity"),  # takes the remaining height
        Layout(name="footer", size=3),
    )
    return layout


def _projects_size(s: Snapshot) -> int:
    # borders (2) + column headers (1) + rows + one slack line
    return min(max(len(s.projects), 1), 10) + 4


def _now_size(s: Snapshot) -> int:
    return min(max(len(s.now), 1), 4) + 2


def run_dashboard(services, interval: float = 2.0, once: bool = False,
                  details: bool = False, console: Console | None = None) -> None:
    console = console or Console()
    if once or not console.is_terminal:
        console.print(render(gather_snapshot(services, details=details)))
        return

    snapshot = gather_snapshot(services, details=details)
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
                if apply(gather_snapshot(services, details=details)):
                    live.refresh()
    except KeyboardInterrupt:
        pass
    console.print("dashboard closed — the orchestrator keeps running")
