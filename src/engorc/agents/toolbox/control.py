"""Loop-control tools: finishing an attempt and parking on a user question.

Both are ordinary actions from the model's point of view but terminate the
tool loop via ToolResult.data["terminal"]. ask_user is the anti-pattern-fix
for v0's blocking input(): the question becomes a gate in the project inbox
and the scheduler moves on to other work.
"""

from __future__ import annotations

from .base import Tool, ToolContext, ToolResult

FINISH_STATUSES = ("done", "failed")


def finish(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    status = str(args.get("status", "done")).lower()
    if status not in FINISH_STATUSES:
        return ToolResult(
            ok=False,
            output=f'finish status must be one of {FINISH_STATUSES}, got {status!r}',
        )
    if not payload.strip():
        return ToolResult(
            ok=False,
            output=(
                "finish needs a handoff note in the fenced payload: what you did, "
                "the state of the work, anything the next person must know."
            ),
        )
    return ToolResult(
        ok=True,
        output=f"finishing with status={status}",
        data={"terminal": status, "handoff_md": payload.strip()},
    )


def ask_architect(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    question = payload.strip() or str(args.get("question", "")).strip()
    if not question:
        return ToolResult(ok=False, output="ask_architect needs the question in the fenced payload")
    consult = ctx.extras.get("consult_architect")
    if not callable(consult):
        return ToolResult(ok=False, output=(
            "the architect is not available in this context — decide like an "
            "engineer and record the assumption in your handoff"
        ))
    answer = str(consult(question)).strip() or "(no answer — proceed on your best judgment)"
    from ...events import Kind

    ctx.journal.append(Kind.DECISION, actor=ctx.role, item=ctx.item_id,
                       title="architect consult",
                       diagnosis=f"Q: {question[:160]} → A: {answer[:240]}")
    return ToolResult(ok=True, output=f"ARCHITECT: {answer}")


def ask_user(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    question = payload.strip() or str(args.get("question", "")).strip()
    if not question:
        return ToolResult(ok=False, output="ask_user needs the question in the fenced payload")
    gate = ctx.project.gates.open(
        question=question,
        from_role=ctx.role,
        phase=str(ctx.extras.get("phase", "")),
        item=ctx.item_id,
        context=str(args.get("context", "")),
    )
    from ...events import Kind

    ctx.journal.append(Kind.GATE_OPENED, actor=ctx.role, item=ctx.item_id, question=question)
    return ToolResult(
        ok=True,
        output=f"question parked for the user (gate {gate.id}); work pauses here",
        data={"terminal": "blocked_on_user", "gate_id": gate.id},
    )


CONTROL_TOOLS = [
    Tool(
        name="finish",
        summary="End this task. Use status done only when acceptance criteria are met.",
        args_doc='{"status": "done"}',
        payload_doc="handoff note: what changed, state of work, warnings, next steps",
        handler=finish,
    ),
    Tool(
        name="ask_architect",
        summary=("Ask the project architect one clarification (scope, design intent, whether "
                 "something is another item's job). Instant and cheap — prefer this over ask_user."),
        args_doc="{}",
        payload_doc="the question, specific, answerable from the design and plan",
        handler=ask_architect,
    ),
    Tool(
        name="ask_user",
        summary="Park a question the user MUST answer before work can continue. Last resort: it stalls this task.",
        args_doc='{"context": "what you already assumed"}',
        payload_doc="the question, specific and answerable in one line",
        handler=ask_user,
    ),
]
