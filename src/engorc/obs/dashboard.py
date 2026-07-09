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
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..events import Event, Kind
from ..sessions import active_sessions
from ..util import human_age, human_duration, progress_bar, shorten, utc_now


def _nowrap(markup: str) -> Text:
    """Fixed-height panels must never wrap: a wrapped line would push the
    newest content out of the cropped region. Ellipsize instead."""
    text = Text.from_markup(markup)
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


def _turn_line(e: Event) -> str:
    target = str(e.payload.get("target", "")).strip()
    head = (f"{e.actor}: turn {e.payload.get('turn')} · {e.payload.get('tool')}"
            + (f" {target}" if target else ""))
    if e.payload.get("ok"):
        return head
    # a failed turn says WHY — 'FAILED' alone is a question, not information
    detail = str(e.payload.get("detail", "")).replace("\n", " ").strip()
    return head + " · FAILED" + (f" — {shorten(detail, 90)}" if detail else "")


EVENT_LINES = {
    Kind.STEP: lambda e: f"step [{e.payload.get('phase')}] {e.payload.get('note', '')}",
    Kind.AGENT_TURN: _turn_line,
    Kind.ATTEMPT_STARTED: lambda e: f"{e.actor} started on {e.item}",
    Kind.ATTEMPT_FINISHED: lambda e: (
        f"{e.actor} {e.payload.get('status')}: {shorten(e.payload.get('summary', ''), 120)}"
    ),
    Kind.VERIFY_RUN: lambda e: (
        "verify: PASS" if e.payload.get("passed")
        else f"verify: FAIL — {shorten(str(e.payload.get('summary', '')), 90)}"
    ),
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


# What each phase's step is doing, for the "working now" panel. Build only
# appears here for the gap between picking an item and its first agent turn;
# a running attempt gets the richer per-item line instead.
PHASE_WORK = {
    "scout": "scout surveying the existing codebase",
    "charter": "charterer drafting charter.yaml",
    "design": "architect drafting design.md",
    "plan": "planner decomposing the design into work items",
    "request": "scout investigating + planner extending the plan for your request",
    "build": "supervisor picking the next work item",
    "wrap": "historian digesting the project into memory",
}


def _phase_step_line(services, config) -> NowLine | None:
    """A phase unit in flight that has no in-progress work item (charter,
    design, plan, wrap) is still work: the GPU lease says who is stepping."""
    from ..fsio import lock_holder

    holder = lock_holder(config.gpu_lock_path)
    if not holder or not holder.get("label"):
        return None
    slug = holder["label"]
    try:
        project = services.registry.get(slug)
        from ..orchestrator.supervisor import next_phase

        phase = next_phase(project)
    except Exception:
        return None
    text = f"{phase} phase · {PHASE_WORK.get(phase, 'working')}"
    since = holder.get("since", "")
    if since:
        try:
            from ..util import parse_iso

            text += f" · step running {human_duration((utc_now() - parse_iso(since)).total_seconds())}"
        except ValueError:
            pass
    return NowLine(slug=slug, text=text)


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
    gpu_stats: dict | None = None  # util %, VRAM used/total GiB
    gpu_procs: list[tuple[str, float]] = field(default_factory=list)  # (name, GiB)
    gpu_spark: str = ""  # utilization sparkline across recent refreshes
    gpu_loading: list[str] = field(default_factory=list)  # load progress bars
    projects: list[tuple[str, ...]] = field(default_factory=list)
    now: list[NowLine] = field(default_factory=list)
    plan_slug: str = ""  # project whose plan is shown (the one being worked)
    plan_rows: list[tuple[bool, str]] = field(default_factory=list)  # (is_current, line)
    activity: list[str] = field(default_factory=list)  # generous buffer; render trims
    activity_capacity: int = 30  # rows the activity panel may fill (set per tick)
    open_gates: int = 0
    sessions: list[dict] = field(default_factory=list)  # live interactive intakes


def _gpu_stats() -> dict | None:
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        parts = [p.strip() for p in proc.stdout.split(",")]
        if len(parts) >= 3:
            return {
                "util": int(float(parts[0])),
                "used_gib": float(parts[1]) / 1024,
                "total_gib": float(parts[2]) / 1024,
            }
    except Exception:
        pass
    return None


def _gpu_processes() -> list[tuple[str, float]]:
    """VRAM consumers, largest first, as (process name, GiB)."""
    if shutil.which("nvidia-smi") is None:
        return []
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=process_name,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        rows: list[tuple[str, float]] = []
        for line in proc.stdout.splitlines():
            name, _, mem = line.rpartition(",")
            name, mem = name.strip(), mem.strip()
            if not name or not mem:
                continue
            rows.append((Path(name).name, float(mem) / 1024))
        rows.sort(key=lambda row: -row[1])
        return rows
    except Exception:
        return []


_SPARK_CHARS = "▁▂▃▄▅▆▇█"
_UTIL_HISTORY: deque[int] = deque(maxlen=24)


def _sparkline(values: deque[int]) -> str:
    return "".join(_SPARK_CHARS[min(7, v * 8 // 100)] for v in values)


def _current_attempt_line(project, plan, max_attempts: int) -> NowLine | None:
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
        parts.append(item.attempt_label(max_attempts))
    if last_turn is not None:
        parts.append(
            f"turn {last_turn.payload.get('turn')} · {last_turn.payload.get('tool')}"
            + ("" if last_turn.payload.get("ok") else " (failed)")
        )
    return NowLine(slug=project.root.name, text=" · ".join(parts))


def gather_snapshot(services, details: bool = False) -> Snapshot:
    config = services.config
    stats = _gpu_stats()
    snapshot = Snapshot(
        profile=config.models.profile,
        home=str(config.home),
        projects_dir=str(config.projects_dir),
        server_ok=services.client.health(),
        server_url=config.server.base_url,
        gpu=(f"GPU {stats['util']}% · VRAM {stats['used_gib']:.1f}/{stats['total_gib']:.1f} GiB"
             if stats else ""),
    )
    snapshot.gpu_stats = stats
    if stats:
        _UTIL_HISTORY.append(stats["util"])
        snapshot.gpu_spark = _sparkline(_UTIL_HISTORY)
        snapshot.gpu_procs = _gpu_processes()
    try:
        snapshot.memory_backend = services.memory.health()[1]
    except Exception:
        snapshot.memory_backend = "unknown"
    snapshot.sessions = active_sessions(config.home)

    # -- gpu: residency, swap history, and live token flow ------------------
    services.observe_gpu()
    snapshot.gpu_current = services.timeline.current()
    snapshot.gpu_events = [services.timeline.describe(e) for e in services.timeline.recent(3)]
    snapshot.gpu_loading = [
        services.timeline.describe_loading(entry["model"], entry["for_seconds"])
        for entry in snapshot.gpu_current
        if entry["state"] == "loading"
    ]
    busy_bits: list[str] = []
    for entry in snapshot.gpu_current:
        if entry["state"] != "ready":
            continue
        activity = services.swap.slot_activity(entry["model"])
        if activity and activity[0] > 0:
            _, decoded, prompt = activity
            if decoded > 0:
                busy_bits.append(f"{entry['model']}: generating (~{decoded} tok)")
            elif prompt > 0:
                busy_bits.append(f"{entry['model']}: prefilling (~{prompt} ctx tok read)")
            else:
                busy_bits.append(f"{entry['model']}: busy (prefill or long generation)")
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
        now_line = _current_attempt_line(project, plan, config.run.max_attempts_per_item)
        if now_line is not None and meta.state == "active":
            snapshot.now.append(now_line)
        for event in project.journal.tail(50):
            merged.append((event.ts, meta.slug, event))
        for event in project.journal.iter_events(
            kinds=[Kind.AGENT_TURN, Kind.STRUCTURED_CALL], since_ts=cutoff
        ):
            tokens_in += int(event.payload.get("prompt_tokens", 0) or 0)
            tokens_out += int(event.payload.get("completion_tokens", 0) or 0)

    if not snapshot.now:
        phase_line = _phase_step_line(services, config)
        if phase_line is not None:
            snapshot.now.append(phase_line)

    # the worked project's plan, in full: the current item in context
    if snapshot.now:
        try:
            focus = services.registry.get(snapshot.now[0].slug)
            focus_plan = focus.load_plan()
            if focus_plan.items:
                snapshot.plan_slug = snapshot.now[0].slug
                snapshot.plan_rows = _plan_rows(focus_plan, config.run.max_attempts_per_item)
        except Exception:
            pass

    snapshot.gpu_io = f"completed calls last 5m: {tokens_in:,} tok in / {tokens_out:,} tok out"
    # Interactive intake calls belong to no project journal: without this
    # note, "generating" next to zero project I/O reads as a stall.
    if snapshot.sessions and snapshot.gpu_live != "idle":
        snapshot.gpu_io += " · serving the interactive session"
    # A single long call journals nothing until it finishes — say so instead
    # of letting "busy" and "0 tokens" sit next to each other unexplained.
    if merged and snapshot.gpu_live != "idle":
        last_ts = max(entry[0] for entry in merged)
        try:
            from ..util import parse_iso

            silent = (utc_now() - parse_iso(last_ts)).total_seconds()
            if silent > 45:
                snapshot.gpu_io += f" · current call running ≥{human_duration(silent)}"
        except ValueError:
            pass
    merged.sort(key=lambda entry: entry[0])
    expand_limit = None if details else 2
    for ts, slug, event in merged[-80:]:
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
    # keep a generous tail; the activity PANEL trims to the terminal's actual
    # capacity at render time (a tall terminal deserves a full feed)
    snapshot.activity = snapshot.activity[-150:]
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
    lines: list[str] = []
    if s.gpu_stats:
        st = s.gpu_stats
        util_gauge = progress_bar(st["util"] / 100, 20, "█", "░")
        lines.append(f"GPU  ▕{util_gauge}▏ {st['util']:>3}%  {escape(s.gpu_spark)}")
        vram_fraction = st["used_gib"] / st["total_gib"] if st["total_gib"] else 0.0
        vram_gauge = progress_bar(vram_fraction, 20, "█", "░")
        vram = f"VRAM ▕{vram_gauge}▏ {st['used_gib']:.1f}/{st['total_gib']:.1f} GiB"
        if s.gpu_procs:
            name, gib = s.gpu_procs[0]
            vram += f"  {escape(shorten(name, 24))} {gib:.1f} GiB"
            rest = s.gpu_procs[1:]
            if rest:
                vram += f" (+{len(rest)} more, {sum(g for _, g in rest):.1f} GiB)"
        lines.append(vram)
    lines.append(f"[bold]now[/bold]: {escape(s.gpu_live)} · {escape(s.gpu_io)}")
    lines += [f"[cyan]{escape(line)}[/cyan]" for line in s.gpu_loading]
    lines += [escape(line) for line in s.gpu_events]
    if not s.gpu_events:
        lines.append("[dim](no swaps observed yet)[/dim]")
    key = (
        s.gpu_stats["util"] if s.gpu_stats else None,
        round(s.gpu_stats["used_gib"], 1) if s.gpu_stats else None,
        s.gpu_spark, tuple(s.gpu_procs),
        s.gpu_live, s.gpu_io, tuple(s.gpu_loading), tuple(s.gpu_events),
    )
    return key, Panel(_nowrap("\n".join(lines)), title="gpu", title_align="left")


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


_STATUS_GLYPH = {"done": "✔", "dropped": "⊘", "in_progress": "▶",
                 "failed": "✖", "blocked": "◌", "review": "◔"}
_PLAN_MAX_ROWS = 13
_PLAN_TITLE_COL = 56


def _plan_rows(plan, max_attempts: int) -> list[tuple[bool, str]]:
    """The plan as a git-log-style dependency tree: every item hangs under
    the dependency it is actually waiting for, so 'blocked' is conveyed by
    position and a dim dot instead of prose. Ready-to-run items get ○, the
    in-flight item ▶; extra parents beyond the drawn one show as '⇠ +N'."""
    from ..plan import TERMINAL_STATUSES

    by_id = plan.by_id()

    depth_cache: dict[str, int] = {}

    def depth(item_id: str, seen: tuple = ()) -> int:
        if item_id in depth_cache:
            return depth_cache[item_id]
        item = by_id.get(item_id)
        deps = [d for d in (item.depends_on if item else [])
                if d in by_id and d not in seen]
        value = 1 + max((depth(d, (*seen, item_id)) for d in deps), default=-1)
        depth_cache[item_id] = value
        return value

    # each item is drawn under its DEEPEST dependency — the one that actually
    # gates it; remaining parents become the ⇠ +N suffix
    children: dict[str | None, list] = {}
    for item in plan.items:
        deps = [d for d in item.depends_on if d in by_id]
        parent = max(deps, key=depth) if deps else None
        children.setdefault(parent, []).append(item)

    def glyph(item) -> str:
        if item.status == "todo":
            return "○" if plan.deps_satisfied(item) else "·"
        return _STATUS_GLYPH.get(item.status, "·")

    def meta(item) -> str:
        bits = []
        # budget display: only failures burn it — a clean run stays quiet,
        # and "N✗" creeping toward the cap signals triage proximity
        fails = item.open_attempt_count()
        if fails:
            bits.append(f"{fails}✗/{max_attempts}")
        if item.status != "todo":
            bits.append(human_age(item.updated))
        extra = len([d for d in item.depends_on if d in by_id]) - 1
        if extra > 0:
            bits.append(f"⇠ +{extra}")
        return " · ".join(bits)

    rows: list[tuple[bool, str, str]] = []  # (is_current, status, line)

    def walk(parent_id: str | None, prefix: str) -> None:
        kids = children.get(parent_id, [])
        for idx, item in enumerate(kids):
            last = idx == len(kids) - 1
            connector = "" if parent_id is None else ("└─" if last else "├─")
            head = f"{prefix}{connector}{glyph(item)} "
            width = max(12, _PLAN_TITLE_COL - len(head))
            line = f"{head}{shorten(item.title, width):<{width}} {meta(item)}".rstrip()
            rows.append((item.status == "in_progress", item.status, line))
            walk(item.id, prefix + ("" if parent_id is None else ("   " if last else "│  ")))

    walk(None, "")

    out = [(current, line) for current, _, line in rows]
    if len(out) > _PLAN_MAX_ROWS:
        finished = 0
        for _, status, _ in rows:
            if status in TERMINAL_STATUSES:
                finished += 1
            else:
                break
        if finished > 1:
            out = [(False, f"✔ {finished} earlier item(s) finished")] + out[finished:]
        if len(out) > _PLAN_MAX_ROWS:
            hidden = len(out) - (_PLAN_MAX_ROWS - 1)
            out = out[:_PLAN_MAX_ROWS - 1] + [(False, f"… {hidden} more item(s)")]
    return out


def _plan_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    if not s.plan_rows:
        return ("hidden",), Panel("[dim](no plan in flight)[/dim]",
                                  title="plan", title_align="left")
    lines = [
        f"[bold cyan]▸ {escape(text)}[/bold cyan]" if current else f"  {escape(text)}"
        for current, text in s.plan_rows
    ]
    key = (s.plan_slug, tuple(s.plan_rows))
    return key, Panel(_nowrap("\n".join(lines)),
                      title=f"plan — {escape(s.plan_slug)}", title_align="left")


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


def _session_line(record: dict) -> str:
    parts = [record.get("kind", "session"), f"({record.get('detail', '')})"]
    status = record.get("status", "")
    if status:
        parts.append(f"— {status}")
    started = record.get("started", "")
    if started:
        parts.append(f"· started {human_age(started)} ago")
    return " ".join(parts)


def _now_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    lines = [
        f"[bold magenta]interactive[/bold magenta] · {escape(_session_line(record))} "
        "· scheduled runs are yielding"
        for record in s.sessions
    ]
    lines += [f"[bold]{escape(line.slug)}[/bold] · {escape(line.text)}" for line in s.now]
    text = "\n".join(lines) or _idle_reason(s)
    key = (
        tuple((r.get("kind"), r.get("detail"), r.get("status")) for r in s.sessions),
        tuple((line.slug, line.text) for line in s.now),
    ) if lines else (text,)
    return key, Panel(_nowrap(text), title="working now", title_align="left")


def _activity_panel(s: Snapshot) -> tuple[tuple, RenderableType]:
    shown = s.activity[-max(3, s.activity_capacity):]
    text = "\n".join(escape(line) for line in shown) or "[dim](no activity yet)[/dim]"
    return tuple(shown), Panel(_nowrap(text), title="recent activity", title_align="left")


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
    "plan": _plan_panel,
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
        Layout(name="gpu", size=_gpu_size(snapshot)),
        Layout(name="projects", size=_projects_size(snapshot)),
        Layout(name="now", size=_now_size(snapshot)),
        Layout(name="plan", size=max(_plan_size(snapshot), 3)),
        Layout(name="activity"),  # takes the remaining height
        Layout(name="footer", size=3),
    )
    layout["plan"].visible = bool(snapshot.plan_rows)
    return layout


def _plan_size(s: Snapshot) -> int:
    return (min(len(s.plan_rows), _PLAN_MAX_ROWS) + 2) if s.plan_rows else 0


def _gpu_size(s: Snapshot) -> int:
    # borders (2) + gauges (2, GPU boxes only) + now-line + load bars + events
    return (2 + (2 if s.gpu_stats else 0) + 1 + len(s.gpu_loading)
            + max(len(s.gpu_events), 1))


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
    sizes = (_gpu_size(snapshot), _projects_size(snapshot), _now_size(snapshot))

    def apply(s: Snapshot) -> bool:
        nonlocal sizes
        changed = False
        new_sizes = (_gpu_size(s), _projects_size(s), _now_size(s), _plan_size(s))
        if new_sizes != sizes:  # geometry changes are rare (project count moved)
            sizes = new_sizes
            layout["gpu"].size, layout["projects"].size, layout["now"].size = new_sizes[:3]
            layout["plan"].size = max(new_sizes[3], 3)
            layout["plan"].visible = bool(s.plan_rows)
            keys.clear()  # force full repaint into the new geometry
            changed = True
        # the activity feed fills whatever the terminal actually offers:
        # total height minus header/footer (3 each), the sized panels, and
        # the activity panel's own borders (2)
        fixed = 3 + 3 + 2 + new_sizes[0] + new_sizes[1] + new_sizes[2]
        if s.plan_rows:
            fixed += new_sizes[3]
        s.activity_capacity = max(3, console.size.height - fixed)
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
