"""The LangGraph phase machine.

One graph invocation = one unit of work: START → route → <one phase> → END.
The route decision reads only the filesystem (supervisor.next_phase), so the
graph can be pointed at any project directory in any state — including one
that was hand-edited, interrupted, or created by an older version — and do
the right next thing. Checkpoints (SQLite, one thread per project) give a
durable, inspectable timeline of every step ever taken; losing them costs
history, never correctness.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from ..events import Kind
from ..project import Project
from .phases import PHASES
from .services import Services
from .supervisor import next_phase


class StepState(TypedDict):
    slug: str
    note: str


def build_graph(services: Services) -> StateGraph:
    builder = StateGraph(StepState)

    def route(state: StepState) -> dict:
        return {}

    def decide(state: StepState) -> str:
        project = services.registry.get(state["slug"])
        return next_phase(project)

    def make_node(phase_name: str):
        def node(state: StepState) -> dict:
            project = services.registry.get(state["slug"])
            if phase_name != "scout" and project.meta.phase != phase_name:
                project.set_phase(phase_name)  # keep status displays truthful
            note = PHASES[phase_name](services, project)
            project.journal.append(Kind.STEP, phase=phase_name, note=note)
            return {"note": note}

        return node

    builder.add_node("route", route)
    for name in PHASES:
        builder.add_node(name, make_node(name))
        builder.add_edge(name, END)
    builder.add_edge(START, "route")
    builder.add_conditional_edges(
        "route",
        decide,
        {**{name: name for name in PHASES}, "done": END},
    )
    return builder


def run_step(services: Services, project: Project) -> str:
    """Execute exactly one phase unit for this project, checkpointed."""
    builder = build_graph(services)
    slug = project.root.name
    with SqliteSaver.from_conn_string(str(project.checkpoint_db)) as saver:
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": slug}, "recursion_limit": 10}
        result = graph.invoke({"slug": slug, "note": ""}, config)
    return result.get("note", "") or "(nothing to do)"
