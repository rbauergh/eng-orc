"""Structured document schemas produced by one-shot agents.

Every schema puts a free-text reasoning field FIRST: under grammar-constrained
decoding the model plans in that field before committing to answers (skipping
it measurably degrades small-model output). All fields are required — optional
fields invite degenerate omissions from small models.

These are working documents, not archives: charters become charter.yaml,
plan drafts become plan.yaml work items, verdicts drive routing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Assumption(BaseModel):
    text: str
    confidence: float = Field(ge=0.0, le=1.0, description="0..1 that this assumption is what the user wants")
    basis: str = Field(description="why this default is reasonable (convention, mission text, prior decision)")


class BlockingQuestion(BaseModel):
    question: str
    why_blocking: str = Field(description="how the answer changes the architecture or scope")
    options: list[str] = Field(description="2-4 concrete choices the user can pick from")


class Charter(BaseModel):
    reasoning: str = Field(description="think here first: what is really being asked, what can be assumed")
    objective: str = Field(description="one paragraph: what will exist when this project is done")
    context_summary: str = Field(description="relevant facts from the mission, codebase, and past answers")
    assumptions: list[Assumption]
    non_goals: list[str] = Field(description="explicitly out of scope")
    success_criteria: list[str] = Field(description="observable, checkable statements")
    risks: list[str]
    blocking_questions: list[BlockingQuestion] = Field(
        description="ONLY questions whose answers change the architecture; empty list means proceed"
    )
    build_commands: list[str] = Field(
        default_factory=list,
        description="commands that (re)build the project's SHIPPED artifacts (executable, "
        "dist/, package) — run from the project root (relative paths only) at every wrap "
        "so builds never go stale; empty when the project ships nothing built",
    )
    ready_to_build: bool


class DecisionExtract(BaseModel):
    title: str
    decision: str
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)


class DesignExtract(BaseModel):
    reasoning: str
    decisions: list[DecisionExtract] = Field(description="the consequential choices made in the design")
    stack: list[str] = Field(description="languages, frameworks, key libraries")
    components: list[str] = Field(description="major modules/files and their responsibilities, one line each")
    open_questions: list[str]


class PlanItemDraft(BaseModel):
    title: str
    kind: Literal["feature", "fix", "refactor", "test", "docs", "chore", "investigate", "integrate"]
    description: str = Field(description="what to build and how it fits the design; concrete enough to start")
    acceptance: list[str] = Field(description="checkable statements that define done")
    verify_commands: list[str] = Field(
        description="shell commands that must exit 0, run FROM THE PROJECT ROOT "
        "(e.g. 'python3 -m pytest -q'). Relative paths only — absolute roots like "
        "/workspace or /app do not exist here. empty = project default"
    )
    depends_on: list[int] = Field(description="indices (0-based) of items in this plan that must finish first")
    files_hint: list[str] = Field(description="files likely touched")
    size: Literal["S", "M", "L"] = Field(description="S: <1h of focused work, M: one sitting, L: should be split")
    test_first: bool = Field(description="true when tests should be written before the implementation")


class PlanDraft(BaseModel):
    reasoning: str
    goal_recap: str = Field(description="one paragraph restating what the plan achieves")
    items: list[PlanItemDraft]


FindingCategory = Literal["BUG", "TEST_GAP", "SPEC_GAP", "STYLE", "ARCHITECTURE", "SECURITY", "PERFORMANCE"]
FindingSeverity = Literal["blocker", "major", "minor"]


class Finding(BaseModel):
    category: FindingCategory
    severity: FindingSeverity
    description: str
    file: str = Field(description="most relevant file, or empty")
    recommendation: str = Field(description="the specific change that resolves this")


class ReviewVerdict(BaseModel):
    reasoning: str
    findings: list[Finding]
    verdict: Literal["approve", "request_changes"]
    summary: str = Field(description="one paragraph for the record")

    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in ("blocker", "major")]


class IntakeTurn(BaseModel):
    """One round of the project-definition conversation (orc new -i)."""

    reasoning: str
    title: str = Field(description="short working title for the project")
    spec_markdown: str = Field(
        description="the COMPLETE current spec document — carry all prior sections "
        "forward, updated with what was just learned or decided"
    )
    question: str = Field(
        description="the single next question for the user; empty when ready. Only "
        "questions that materially shape the project — never trivia"
    )
    ready: bool = Field(description="true when the spec suffices to charter the project")


class ItemTriage(BaseModel):
    item_id: str
    action: Literal["revise", "split", "drop", "retry", "ask_user"]
    diagnosis: str = Field(description="what actually went wrong, citing the evidence")
    guidance: str = Field(description="concrete direction for the next attempt (revise/retry); empty otherwise")
    new_description: str = Field(description="revise: replacement description; empty otherwise")
    new_acceptance: list[str] = Field(description="revise: replacement acceptance criteria; empty = keep")
    new_verify_commands: list[str] = Field(
        description="revise: replacement verify commands, run from the project root "
        "with relative paths only; empty = keep"
    )
    split_items: list[PlanItemDraft] = Field(
        description="split: the smaller replacement items, in order. depends_on indices "
        "refer to positions WITHIN this list; the parent's dependencies are inherited "
        "and consecutive items are chained automatically"
    )
    question: str = Field(description="ask_user: the specific decision only a human can make")


class PlanReviewVerdict(BaseModel):
    reasoning: str
    findings: list[str] = Field(
        description="blocking problems only, each 'item: problem → fix' — dependency-graph "
        "gaps, ambiguous or overlapping items, unverifiable acceptance, missing charter work"
    )
    verdict: Literal["approve", "request_changes"]


class DependencyFix(BaseModel):
    item_id: str
    depends_on: list[str] = Field(
        description="the item's corrected FULL dependency list (existing item ids)"
    )
    reason: str = Field(description="the evidence this repairs, e.g. 'build item ran "
                        "before the code it packages existed'")


class TriageReport(BaseModel):
    reasoning: str
    items: list[ItemTriage]
    dependency_fixes: list[DependencyFix] = Field(
        default_factory=list,
        description="plan-graph repairs: items whose depends_on is wrong or missing "
        "(an empty depends_on schedules an item IMMEDIATELY; a dependency on a "
        "dropped item counts as satisfied). May fix ANY item in the plan, not "
        "just the failing ones",
    )
    systemic_notes: list[str] = Field(
        description="problems beyond any single item (a reviewer model failing to load, "
        "a broken environment) worth surfacing to the user"
    )


class CommitMessageDraft(BaseModel):
    reasoning: str
    message: str = Field(description="conventional-commit style subject line, <=72 chars, imperative")


class DigestExtract(BaseModel):
    reasoning: str
    summary: str = Field(description="what happened this session, one tight paragraph")
    lessons: list[str] = Field(description="durable insights worth remembering across projects; empty if none")
    conventions: list[str] = Field(description="user preferences observed (style, tools, workflow); empty if none")
