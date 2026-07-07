"""Role definitions: which persona runs on which model slot with which tools.

Two shapes of role:
- tool-loop roles (scout, implementer, tester) run the ACTION protocol;
- one-shot roles (charterer, architect, planner, reviewer, historian) produce
  a single document or structured verdict from a packed brief.

Reviewer runs on the coder slot deliberately: same weights, different persona
and a structured verdict schema — v0's mistake was not the shared model but
the vague prompt and prose-parsed routing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources


@dataclass(frozen=True)
class RoleSpec:
    name: str
    kind: str  # "loop" | "oneshot"
    model_role: str  # coder | planner | utility
    prompt_file: str
    tools: list[str] = field(default_factory=list)
    max_turns: int = 0
    description: str = ""


LOOP_TOOLS_FULL = [
    "read_file", "write_file", "edit_file", "list_dir", "grep", "search",
    "run", "run_tests", "git_status", "git_diff", "ask_architect", "finish", "ask_user",
]
LOOP_TOOLS_READONLY = ["read_file", "list_dir", "grep", "search", "git_log", "git_status", "finish"]

ROLES: dict[str, RoleSpec] = {
    spec.name: spec
    for spec in (
        RoleSpec(
            name="scout",
            kind="loop",
            model_role="coder",
            prompt_file="scout.md",
            tools=LOOP_TOOLS_READONLY,
            max_turns=12,
            description="explores an existing codebase and writes the codebase report",
        ),
        RoleSpec(
            name="charterer",
            kind="oneshot",
            model_role="planner",
            prompt_file="charterer.md",
            description="turns a mission into a charter: assumptions, criteria, blocking questions",
        ),
        RoleSpec(
            name="architect",
            kind="oneshot",
            model_role="planner",
            prompt_file="architect.md",
            description="writes the living design document",
        ),
        RoleSpec(
            name="planner",
            kind="oneshot",
            model_role="planner",
            prompt_file="planner.md",
            description="breaks the design into a DAG of verifiable work items",
        ),
        RoleSpec(
            name="implementer",
            kind="loop",
            model_role="coder",
            prompt_file="implementer.md",
            tools=LOOP_TOOLS_FULL,
            max_turns=40,
            description="builds one work item to its acceptance criteria",
        ),
        RoleSpec(
            name="tester",
            kind="loop",
            model_role="coder",
            prompt_file="tester.md",
            tools=LOOP_TOOLS_FULL,
            max_turns=25,
            description="writes failing tests that encode a work item's acceptance criteria",
        ),
        RoleSpec(
            name="reviewer",
            kind="oneshot",
            model_role="coder",
            prompt_file="reviewer.md",
            description="reviews a diff against the item and design; classifies findings",
        ),
        RoleSpec(
            name="historian",
            kind="oneshot",
            model_role="utility",
            prompt_file="historian.md",
            description="digests the journal into lessons and a project card at wrap-up",
        ),
    )
}


def role(name: str) -> RoleSpec:
    return ROLES[name]


def load_prompt(prompt_file: str) -> str:
    return (resources.files("engorc.agents.prompts") / prompt_file).read_text(encoding="utf-8")


# Review-panel lenses: each panelist hunts a different failure mode, so extra
# seats add coverage instead of redundant opinions. Appended to reviewer.md.
REVIEW_LENSES: dict[str, str] = {
    "correctness": (
        "Primary lens: CORRECTNESS and completeness. Does the diff actually satisfy every "
        "acceptance criterion? Trace the logic on realistic inputs, including the edges the "
        "tests skip."
    ),
    "adversarial": (
        "Primary lens: ADVERSARIAL. Assume this diff is wrong and try to prove it. Construct "
        "concrete inputs or sequences that break it; hunt for silent failure paths, swallowed "
        "errors, off-by-ones, and 'passes the tests but not the intent' shortcuts. If you "
        "cannot break it after honest effort, approve."
    ),
    "security": (
        "Primary lens: SECURITY. Injection, path traversal, unsafe deserialization, secrets in "
        "code or logs, unvalidated external input, subprocess/shell hazards."
    ),
    "tests": (
        "Primary lens: TESTS. Do the tests genuinely encode the acceptance criteria, or do "
        "they only mirror the implementation? Find the untested branch that matters most; flag "
        "assertions weak enough to pass wrong behavior."
    ),
    "architecture": (
        "Primary lens: ARCHITECTURE. Does the change respect the design document — right "
        "module, right layer, no duplicated logic, no leaked abstractions that will hurt the "
        "next work item?"
    ),
}
