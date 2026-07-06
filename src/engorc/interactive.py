"""Interactive gate answering: questions become prompts, not homework.

No gate ids to copy: open questions are presented one at a time with their
context and numbered options; type an answer (or an option number), skip, or
quit. Used by `orc inbox` on a TTY and inline by `orc run --interactive`
whenever the scheduler would otherwise idle on open questions.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt

from .events import Kind
from .obs.console import log
from .util import human_age, shorten


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
