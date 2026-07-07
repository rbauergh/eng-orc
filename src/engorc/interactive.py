"""Interactive gate answering: questions become prompts, not homework.

No gate ids to copy: open questions are presented one at a time with their
context and numbered options; type an answer (or an option number), skip, or
quit. Used by `orc inbox` on a TTY and inline by `orc run --interactive`
whenever the scheduler would otherwise idle on open questions.

Also home to `call_with_progress`: interactive commands wrap their blocking
LLM calls in it so a cold model swap shows as a progress bar (measured
against the model's own load history) instead of a dead prompt.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt

from .events import Kind
from .llm.timeline import normalize_states
from .obs.console import log
from .util import human_age, shorten

T = TypeVar("T")


def _open_gates(services, skipped: set[str]):
    pairs = []
    for project in services.registry.all_projects():
        for gate in project.gates.open_gates():
            if gate.id not in skipped:
                pairs.append((project, gate))
    pairs.sort(key=lambda pair: pair[1].ts)
    return pairs


def prompt_gates(services, console: Console | None = None, limit: int | None = None) -> int:
    """Present open gates as prompts; returns how many were answered."""
    console = console or log.console
    skipped: set[str] = set()
    answered = 0
    while True:
        if limit is not None and answered >= limit:
            break
        pairs = _open_gates(services, skipped)
        if not pairs:
            break
        project, gate = pairs[0]
        remaining = len(pairs)
        console.print()
        console.rule(
            f"[bold]{escape(project.root.name)}[/bold] · {escape(gate.from_role)} · "
            f"{human_age(gate.ts)} ago · {remaining} open"
        )
        console.print(gate.question, markup=False)
        if gate.context:
            console.print(f"[dim]{escape(shorten(gate.context, 300))}[/dim]")
        if gate.options:
            for index, option in enumerate(gate.options, 1):
                console.print(f"  {index}. {escape(option)}")
        console.print("[dim]type your answer — or an option number, 's' to skip, 'q' to quit[/dim]")
        raw = Prompt.ask("[bold]›[/bold]", console=console).strip()
        if raw.lower() == "q":
            break
        if not raw or raw.lower() == "s":
            skipped.add(gate.id)
            continue
        if raw.isdigit() and gate.options and 1 <= int(raw) <= len(gate.options):
            raw = gate.options[int(raw) - 1]
        project.gates.answer(gate.id, raw)
        project.journal.append(Kind.GATE_ANSWERED, actor="user", answer=raw, question=gate.question)
        log.success(f"answered — {project.root.name} resumes on the next pass")
        answered += 1
    return answered


# ------------------------------------------------------------- model-wait progress


def call_with_progress(services, work: Callable[[], T], label: str = "thinking") -> T:
    """Run a blocking LLM call on a worker thread while the console narrates
    what the GPU is actually doing: a swap-in shows as a progress bar against
    that model's typical load time, then live token counts once it generates.
    Off-terminal it is a plain passthrough."""
    console = log.console
    if not console.is_terminal:
        return work()

    outcome: list[T] = []
    failure: list[BaseException] = []

    def runner() -> None:
        try:
            outcome.append(work())
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            failure.append(exc)

    worker = threading.Thread(target=runner, daemon=True)
    started = time.monotonic()
    worker.start()
    with console.status(f"{label} …", spinner="dots") as status:
        while worker.is_alive():
            worker.join(0.5)
            status.update(_gpu_wait_line(services, label, time.monotonic() - started))
    if failure:
        raise failure[0]
    return outcome[0]


_BAR_WIDTH = 20


def _gpu_wait_line(services, label: str, elapsed: float) -> str:
    """One status line from live server state; degrades to plain elapsed time."""
    try:
        running = services.swap.running_models()
        services.timeline.observe(running)  # every wait doubles as an observer
        states = normalize_states(running)
        loading = sorted(name for name, state in states.items() if state == "loading")
        if loading:
            name = loading[0]
            loading_for = _loading_for(services.timeline, name, elapsed)
            typical = services.timeline.typical_load_seconds(name)
            if typical:
                fraction = min(loading_for / typical, 0.99)
                filled = int(fraction * _BAR_WIDTH)
                bar = "━" * filled + "╌" * (_BAR_WIDTH - filled)
                return (f"loading {name} {bar} {fraction:.0%} "
                        f"({loading_for:.0f}s of ~{typical:.0f}s typical)")
            return f"loading {name} … {loading_for:.0f}s (first load — learning its timing)"
        resident = sorted(name for name, state in states.items() if state == "ready")
        if resident:
            name = resident[0]
            activity = services.swap.slot_activity(name)
            if activity:
                busy, decoded, prompt = activity
                if busy and decoded:
                    return f"{label} … {elapsed:.0f}s · {name} generating ({decoded} tokens)"
                if busy and prompt:
                    return f"{label} … {elapsed:.0f}s · {name} reading the prompt"
    except Exception:
        pass
    return f"{label} … {elapsed:.0f}s"


def _loading_for(timeline, model: str, fallback: float) -> float:
    """How long the model has been loading per the shared on-disk record —
    correct even when the swap began before this command started waiting."""
    for entry in timeline.current():
        if entry["model"] == model and entry["state"] == "loading":
            return entry["for_seconds"]
    return fallback
