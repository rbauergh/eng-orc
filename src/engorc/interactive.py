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

import select
import sys
import threading
import time
from collections.abc import Callable
from typing import TypeVar

from rich.console import Console
from rich.markup import escape

from .events import Kind
from .llm.timeline import normalize_states
from .obs.console import log
from .util import human_age, shorten

T = TypeVar("T")

BLOCK_MARK = '"""'


def read_answer(console: Console, prompt: str = "› ") -> str:
    """Read one answer that survives multi-line pastes.

    On a real terminal this uses prompt_toolkit's BRACKETED PASTE support:
    the terminal marks paste start/end explicitly, pasted newlines become
    buffer content instead of submissions, and Enter sends the whole thing —
    immune to ConPTY/WSL paste chunking, no timing heuristics. Off-terminal
    (pipes, tests) it falls back to readline with a buffered-burst drain.
    A lone triple-quote line opens and closes an explicit block for
    deliberately TYPED multi-line input in both modes."""
    if sys.stdin.isatty():
        try:
            from prompt_toolkit import prompt as pt_prompt

            try:
                text = pt_prompt(prompt)
            except EOFError:
                return ""
            if text.strip() == BLOCK_MARK:
                lines: list[str] = []
                while True:
                    try:
                        line = pt_prompt("… ")
                    except EOFError:
                        break
                    if line.strip() == BLOCK_MARK:
                        break
                    lines.append(line)
                return "\n".join(lines).strip()
            return text.strip()
        except ImportError:
            pass  # prompt_toolkit missing: the readline fallback still works
    console.print(f"[bold]{escape(prompt)}[/bold]", end="")
    first = sys.stdin.readline()
    if not first:
        return ""
    first = first.rstrip("\n")
    if first.strip() == BLOCK_MARK:
        lines = []
        while True:
            line = sys.stdin.readline()
            if not line or line.rstrip("\n").strip() == BLOCK_MARK:
                break
            lines.append(line.rstrip("\n"))
        return "\n".join(lines).strip()
    lines = [first]
    try:
        while select.select([sys.stdin], [], [], 0.08)[0]:
            line = sys.stdin.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
    except Exception:
        pass  # non-selectable stdin (pipes on exotic platforms): single line
    return "\n".join(lines).strip()


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
        console.print(
            "[dim]type your answer (multi-line paste ok; \"\"\" opens a block) — or an option "
            "number, '? <question>' to ask about this first, 's' to skip, 'q' to quit[/dim]"
        )
        dialogue: list[tuple[str, str]] = []
        while True:
            raw = read_answer(console)
            if raw.startswith("?"):
                question = raw.lstrip("?").strip()
                if not question:
                    continue
                reply = _gate_chat_reply(services, project, gate, question, dialogue)
                dialogue.append((question, reply))
                console.print(f"[cyan]{escape(reply)}[/cyan]")
                console.print("[dim]answer when ready — or ask another '?' question[/dim]")
                continue
            break
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


def _gate_chat_reply(services, project, gate, question: str,
                     dialogue: list[tuple[str, str]]) -> str:
    """One architect-grade reply to a clarifying question about an open gate —
    the user interrogates the context BEFORE committing an answer."""
    from .agents.runtime import one_shot_prose
    from .llm.budget import Section
    from .llm.catalog import model_for_agent

    gate_text = gate.question + (f"\n\nContext: {gate.context}" if gate.context else "")
    sections = [
        Section(name="The orchestrator's question to the user", text=gate_text, priority=1),
        Section(name="The user's clarifying question", text=question, priority=1),
    ]
    if dialogue:
        chat = "\n".join(f"User: {q}\nYou: {a}" for q, a in dialogue)
        sections.append(Section(name="Clarification chat so far", text=chat,
                                priority=2, truncate="tail"))
    try:
        plan = project.load_plan()
        if gate.item:
            from .orchestrator.briefs import item_task_text

            item = plan.get(gate.item)
            sections.append(Section(name="The work item in question",
                                    text=item_task_text(item), priority=2))
            if item.notes:
                sections.append(Section(name="Item notes (failure history)",
                                        text="\n".join(item.notes[-6:]),
                                        priority=3, truncate="tail"))
    except Exception:
        pass
    if project.design_path.exists():
        sections.append(Section(name="Design document",
                                text=project.design_path.read_text(encoding="utf-8"),
                                priority=4, truncate="middle"))

    def work() -> str:
        text, _usage = one_shot_prose(
            services.client,
            model_for_agent(services.config, "architect"),
            ("# Gate clarification\nThe orchestrator asked the user a question; before "
             "answering, the user asks YOU something about it. Answer concretely in at "
             "most 5 sentences from the project context. When asked for a recommendation, "
             "give a specific answer they could type verbatim."),
            sections,
            # The reply is capped at 5 sentences, but a thinking architect
            # spends most of this budget in the reasoning channel first — 400
            # was routinely eaten whole, leaving no room for the answer.
            max_tokens=1200,
        )
        return text

    try:
        return call_with_progress(services, work, label="thinking about your question") \
            or "(no answer came back — go with your judgment)"
    except Exception as exc:
        return f"(the architect could not answer: {shorten(str(exc), 120)})"


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


def _gpu_wait_line(services, label: str, elapsed: float) -> str:
    """One status line from live server state; degrades to plain elapsed time."""
    try:
        running = services.swap.running_models()
        if running is None:  # server unreachable — nothing to narrate
            return f"{label} … {elapsed:.0f}s"
        services.timeline.observe(running)  # every wait doubles as an observer
        states = normalize_states(running)
        loading = sorted(name for name, state in states.items() if state == "loading")
        if loading:
            name = loading[0]
            return services.timeline.describe_loading(
                name, _loading_for(services.timeline, name, elapsed))
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
