"""Interactive project definition (`orc new -i`).

A short conversation with the planner model builds a spec document turn by
turn; the finished spec becomes the project's mission.md — which the
charter/design/plan pipeline already consumes far better than a one-line
goal. Deferred answers ("whatever you want") are decided by the model and
recorded with rationale, per the same judgment rules the charterer lives by.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt

from .agents import load_prompt
from .agents.schemas import IntakeTurn
from .events import Kind
from .llm.budget import Section
from .llm.catalog import model_for_agent
from .llm.structured import StructuredCaller
from .obs.console import log
from .util import iso_now, shorten


@dataclass
class IntakeResult:
    title: str
    spec_markdown: str
    transcript_markdown: str


def _sections(seed: str | None, spec: str, dialogue: list[tuple[str, str]]) -> list[Section]:
    sections = []
    if seed:
        sections.append(Section(name="Seed document from the user", text=seed, priority=1,
                                truncate="middle"))
    if spec:
        sections.append(Section(name="Current spec draft (carry forward and improve)",
                                text=spec, priority=1))
    if dialogue:
        lines = []
        for question, answer in dialogue:
            lines.append(f"You asked: {question}")
            lines.append(f"User answered: {answer}")
        sections.append(Section(name="Conversation so far", text="\n".join(lines), priority=2,
                                truncate="tail"))
    if not seed and not spec and not dialogue:
        sections.append(Section(name="Situation", text="The user wants to define a new project "
                                "but has provided nothing yet. Ask your first question.",
                                priority=1))
    return sections


def run_intake(services, seed: str | None = None,
               console: Console | None = None) -> IntakeResult | None:
    """Returns the finished spec, or None when the user quits."""
    console = console or log.console
    config = services.config
    caller = StructuredCaller(services.client, journal=None, actor="intake")
    role_model = model_for_agent(config, "planner")
    system = load_prompt("intake.md")

    spec = ""
    title = "untitled project"
    dialogue: list[tuple[str, str]] = []
    console.print("[bold]Project intake[/bold] — answer, defer ('whatever you want'), "
                  "'show' prints the spec, 'done' finalizes, 'quit' aborts.")

    for round_no in range(1, config.run.intake_rounds + 1):
        forced_final = round_no == config.run.intake_rounds
        sections = _sections(seed, spec, dialogue)
        if forced_final:
            sections.insert(0, Section(
                name="Instruction",
                text="Round budget reached: finalize NOW — decide every open point "
                     "yourself, record the decisions, set ready=true, no question.",
                priority=1,
            ))
        try:
            turn = caller.call(role_model, IntakeTurn, [
                {"role": "system", "content": system},
                {"role": "user", "content": "\n\n".join(s.header() + s.text for s in sections)},
            ])
        except Exception as exc:
            log.error(f"intake model call failed: {exc}")
            return None
        if turn.spec_markdown.strip():
            spec = turn.spec_markdown.strip()
            title = turn.title.strip() or title
        console.print(f"[dim]spec updated · {len(spec.splitlines())} lines · "
                      f"“{escape(shorten(title, 50))}”[/dim]")

        if turn.ready or forced_final or not turn.question.strip():
            console.print()
            console.print(spec, markup=False)
            console.print()
            choice = Prompt.ask("[bold]Create this project?[/bold] (y)es / (t)alk more / (q)uit",
                                console=console, default="y").strip().lower()
            if choice.startswith("y"):
                return IntakeResult(title=title, spec_markdown=spec,
                                    transcript_markdown=_transcript(dialogue, spec))
            if choice.startswith("q"):
                return None
            guidance = Prompt.ask("[bold]What should change?[/bold]", console=console).strip()
            dialogue.append(("Is the spec ready to build?", guidance or "keep refining"))
            continue

        console.print(f"\n[bold cyan]?[/bold cyan] {escape(turn.question)}")
        while True:
            answer = Prompt.ask("[bold]›[/bold]", console=console).strip()
            if answer.lower() == "show":
                console.print(spec or "(no spec yet)", markup=False)
                continue
            break
        if answer.lower() == "quit":
            return None
        if answer.lower() == "done":
            dialogue.append((turn.question, "finalize now — decide anything still open yourself"))
            continue
        dialogue.append((turn.question, answer or "whatever you think is best"))

    return IntakeResult(title=title, spec_markdown=spec,
                        transcript_markdown=_transcript(dialogue, spec))


def _transcript(dialogue: list[tuple[str, str]], spec: str) -> str:
    lines = [f"# Intake conversation — {iso_now()}", ""]
    for question, answer in dialogue:
        lines += [f"**Q:** {question}", f"**A:** {answer}", ""]
    lines += ["---", "", "Final spec:", "", spec]
    return "\n".join(lines)


def create_project_from_intake(services, result: IntakeResult, **create_kwargs) -> object:
    project = services.registry.create(result.spec_markdown, title=result.title, **create_kwargs)
    project.write_mission(f"# Mission\n\n{result.spec_markdown}\n\n_Defined via intake, {iso_now()}_\n")
    project.artifacts.write("intake-conversation.md", result.transcript_markdown)
    project.journal.append(Kind.USER_NOTE, actor="user", note="mission defined via intake conversation")
    return project
