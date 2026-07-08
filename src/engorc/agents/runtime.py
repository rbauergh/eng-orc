"""Agent runtimes: the tool loop and one-shot document calls.

The tool loop speaks a deliberately primitive protocol optimized for small
local models (mini-SWE-agent lineage):

    <a short paragraph of reasoning>

    ACTION: tool_name {"small": "scalar args"}
    ```payload
    raw text when the tool takes content (file body, patch, command)
    ```

Exactly one ACTION per turn, regex-parsed; malformed turns get the error
quoted back and cost a turn (self-repairing, never crashing). Code payloads
are never JSON-escaped. The loop owns everything the model is bad at:
repetition detection, observation shaping, history compaction, turn/token
budgets, and recitation of the current task at the end of every message.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from ..artifacts import Handoff
from ..config import Config, RoleModel
from ..events import Journal, Kind
from ..llm.budget import ContextPacker, Section
from ..llm.client import LLMClient, LLMUsage
from ..llm.structured import StructuredCaller, strip_thinking
from ..obs.console import log
from ..util import approx_tokens, iso_now, shorten, truncate_middle
from .toolbox import Tool, ToolContext, render_tool_docs

_ACTION_RE = re.compile(r"^[ \t]*ACTION:[ \t]*([A-Za-z_][\w]*)[ \t]*(\{.*\})?[ \t]*$", re.MULTILINE)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\n(.*?)```", re.DOTALL)

FORMAT_CONTRACT = """\
## How to act
Reply with a SHORT paragraph of reasoning, then EXACTLY ONE action:

ACTION: tool_name {"arg": "value"}
```payload
raw text here, only when the tool needs content
```

The ACTION line and its payload fence must be in the SAME reply — announcing
an action without its payload does nothing. A complete edit looks like:

ACTION: edit_file {"path": "app.py"}
```payload
<<<<<<< SEARCH
def greet():
    return "hi"
=======
def greet():
    return "hello"
>>>>>>> REPLACE
```

Rules:
- One ACTION per reply. Never two. Never zero.
- Args are a small one-line JSON object of scalars. File contents, patches,
  and shell commands go in the fenced payload as plain text — NEVER inside JSON.
- Each command runs fresh (no shell state persists); chain with && if needed.
- Every command already runs AT THE PROJECT ROOT. Use relative paths
  ("connect4/renderer.py", "./test_x.py"). Never cd to or search absolute
  paths you have not seen in an observation — they do not exist here.
- Observations are truncated; ask for specific files/lines rather than dumps.
- In long sessions your earliest turns may be replaced by a summary labeled
  "Progress so far". Details absent from it are gone — re-read files rather
  than trusting memory of earlier turns.
- When the task's acceptance criteria are met and verification passes, use:
  ACTION: finish {"status": "done"}   (with a handoff note in the payload)
- If you are truly unable to proceed, finish with status "failed" and a
  payload explaining exactly what blocked you.
"""


class FormatError(Exception):
    pass


# args keys that models mistakenly use instead of the fenced payload block
# (checked against every tool's legitimate args — no collisions)
_PAYLOAD_ARG_KEYS = ("payload", "content", "contents", "file_content", "file_contents",
                     "new_content", "command", "cmd", "script", "code", "body", "text",
                     "data", "source", "handoff", "note", "message", "summary", "report")


@dataclass
class ParsedAction:
    thought: str
    tool: str
    args: dict
    payload: str


def parse_action(text: str) -> ParsedAction:
    text = strip_thinking(text)
    if not text.strip():
        raise FormatError(
            "your reply had no visible content — if you were reasoning, you likely "
            "spent the whole output budget on it. Answer with a ONE-sentence thought "
            "and the ACTION line immediately."
        )
    matches = list(_ACTION_RE.finditer(text))
    if len(matches) == 0:
        raise FormatError(
            "no ACTION line found. Reply with exactly one line like: "
            'ACTION: tool_name {"arg": "value"} (plus a fenced payload when needed).'
        )
    if len(matches) > 1:
        raise FormatError(f"found {len(matches)} ACTION lines; provide EXACTLY ONE action per reply.")
    match = matches[0]
    tool = match.group(1)
    args: dict = {}
    if match.group(2):
        try:
            args = json.loads(match.group(2))
        except json.JSONDecodeError as exc:
            raise FormatError(
                f"the args after ACTION: {tool} are not valid JSON ({exc}). "
                "Keep args to a small one-line JSON object of scalars."
            ) from exc
        if not isinstance(args, dict):
            raise FormatError("ACTION args must be a JSON object")
    payload_match = _FENCE_RE.search(text, match.end())
    payload = payload_match.group(1) if payload_match else ""
    if payload.endswith("\n"):
        payload = payload[:-1]
    thought = text[: match.start()].strip()
    return ParsedAction(thought=thought, tool=tool, args=args, payload=payload)


class AttemptResult(BaseModel):
    status: Literal["done", "failed", "stuck", "blocked_on_user", "error"]
    summary: str = ""
    handoff_md: str = ""
    gate_id: str | None = None
    turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    transcript: str = ""  # artifact path relative to the project root
    touched_files: list[str] = []


@dataclass
class _Turn:
    assistant: str
    observation: str


class ToolLoop:
    def __init__(
        self,
        client: LLMClient,
        config: Config,
        role_name: str,
        role_model: RoleModel,
        tools: list[Tool],
        ctx: ToolContext,
        journal: Journal,
        system_prompt: str,
    ):
        self.client = client
        self.config = config
        self.role_name = role_name
        self.role_model = role_model
        self.tools = {tool.name: tool for tool in tools}
        self.ctx = ctx
        self.journal = journal
        self.system_prompt = (
            system_prompt.rstrip()
            + "\n\n"
            + FORMAT_CONTRACT
            + "\n## Tools\n"
            + render_tool_docs(tools)
        )

    # -- loop ---------------------------------------------------------------
    def run(self, brief: str, task_recitation: str, max_turns: int) -> AttemptResult:
        turns: list[_Turn] = []
        usage_total = LLMUsage()
        touched: list[str] = []
        consecutive_format_errors = 0
        repeat_count = 0
        last_signature = ""
        result_status: str | None = None
        result_handoff = ""
        result_gate: str | None = None
        summary_of_compacted = ""
        token_budget = self.role_model.max_output_tokens

        # Dynamic budget: max_turns is the BASE; an agent that keeps producing
        # earns extension up to a hard ceiling, while one that stalls is cut
        # long before any cap. "Producing" = NEW ground: a successful action
        # not executed before (novel reads ARE work — discovery phases read a
        # lot before writing), or a re-run command whose output changed.
        # Repetition — same reads, same failing run verbatim — is the stall.
        stall_window = max(4, self.config.run.stall_turns)
        hard_cap = max_turns * 2
        last_productive = 0
        last_probe_output = ""
        seen_signatures: set[str] = set()

        for turn_no in range(1, hard_cap + 1):
            # the honest deadline: the stall axe or the hard ceiling,
            # whichever comes first — progress pushes it forward
            turns_left = max(1, min(hard_cap, last_productive + stall_window) - turn_no + 1)
            messages = self._build_messages(brief, turns, task_recitation, turns_left,
                                            touched, summary_of_compacted)
            try:
                response = self.client.chat(self.role_model, messages, max_tokens=token_budget)
            except Exception as exc:
                self.journal.append(Kind.ERROR, actor=self.role_name, item=self.ctx.item_id,
                                    error=f"llm call failed: {exc}")
                return self._finalize("error", f"LLM call failed: {exc}", turns, usage_total, touched)
            usage_total += response.usage

            raw_reply = response.text
            if not strip_thinking(raw_reply).strip() and response.reasoning.strip():
                # the model put its whole reply in the reasoning channel —
                # the ACTION line is usually in there
                raw_reply = response.reasoning
            try:
                action = parse_action(raw_reply)
                consecutive_format_errors = 0
            except FormatError as exc:
                consecutive_format_errors += 1
                observation = f"FORMAT ERROR: {exc}"
                if response.finish_reason == "length":
                    # reasoning ate the whole budget before an ACTION appeared —
                    # a repair prompt alone cannot fix a budget problem
                    token_budget = int(token_budget * 1.5)
                    observation += (
                        " Your reply hit the output token budget before an ACTION line "
                        "appeared — think less and act sooner. The budget is now larger."
                    )
                # record what the model actually emitted (raw_reply includes the
                # reasoning-channel recovery), not a blank content field
                turns.append(_Turn(assistant=raw_reply, observation=observation))
                self._journal_turn(turn_no, "(format-error)", False, response.usage)
                if consecutive_format_errors >= 3:
                    return self._finalize(
                        "stuck", "three malformed replies in a row", turns, usage_total, touched
                    )
                continue

            # A model that puts content in a JSON arg instead of the fence made
            # a formatting mistake, not a semantic one — the string already
            # survived JSON parsing, so use it (a whole attempt died repeating
            # this exact mistake against a pedantic rejection).
            salvage_note = ""
            if not action.payload.strip():
                for key in _PAYLOAD_ARG_KEYS:
                    value = action.args.get(key)
                    if isinstance(value, str) and value.strip():
                        action.payload = value
                        action.args = {k: v for k, v in action.args.items() if k != key}
                        salvage_note = (
                            f"\n\nNOTE: you passed {key!r} inside the JSON args; content "
                            "belongs in the fenced ```payload block. Accepted this time — "
                            "use the fence next time."
                        )
                        break

            signature = f"{action.tool}|{json.dumps(action.args, sort_keys=True)}|{hash(action.payload)}"
            if signature == last_signature:
                repeat_count += 1
            else:
                repeat_count = 0
                last_signature = signature

            tool = self.tools.get(action.tool)
            if tool is None:
                observation = (
                    f"unknown tool {action.tool!r}. Available: {', '.join(sorted(self.tools))}"
                )
                turns.append(_Turn(assistant=response.text, observation=observation))
                self._journal_turn(turn_no, action.tool, False, response.usage)
                continue

            result = tool.run(self.ctx, action.args, action.payload)
            log.debug(
                f"{self.role_name} turn {turn_no}/{hard_cap}: {action.tool}"
                + ("" if result.ok else " (failed)")
            )
            if result.ok and result.data.get("path") and result.data.get("action") in ("write", "edit"):
                path = str(result.data["path"])
                if path not in touched:
                    touched.append(path)

            novel = signature not in seen_signatures
            seen_signatures.add(signature)
            if (result.ok and novel) or (
                action.tool in ("run", "run_tests") and result.output != last_probe_output
            ):
                last_productive = turn_no
            if action.tool in ("run", "run_tests"):
                last_probe_output = result.output

            terminal = result.data.get("terminal") if result.data else None
            observation = result.shaped(self.config.run.max_tool_output_chars) + salvage_note
            if (not result.ok and not action.payload.strip()
                    and response.finish_reason == "length"):
                # the reply was cut off after the ACTION line, before the fence:
                # a payload-needing tool then fails on emptiness — grow the
                # budget so the resend can carry its payload
                token_budget = int(token_budget * 1.5)
                observation += (
                    "\n\nNOTE: your reply hit the output token budget before the fenced "
                    "payload appeared — the budget is now larger; resend this action WITH "
                    "its payload block."
                )
            if repeat_count == 1:
                observation += (
                    "\n\nNOTE: you repeated the exact same action. If it did not work "
                    "before, it will not work now — change your approach."
                )
            stall_kind = "no new information, file changes, or command results"
            if turn_no - last_productive == stall_window - 2:
                observation += (
                    f"\n\nNOTE: {stall_kind} in {turn_no - last_productive} turns — you "
                    "appear stuck. Change your approach now, or finish with status "
                    "\"failed\" and state exactly what blocks you."
                )
            target = str(action.args.get("path") or action.args.get("pattern") or "")
            if not target and action.tool in ("run", "run_tests"):
                target = shorten(" ".join(action.payload.split()), 48)
            turns.append(_Turn(assistant=raw_reply, observation=observation))
            self._journal_turn(turn_no, action.tool, result.ok, response.usage,
                               detail="" if result.ok else result.output, target=target)

            if terminal in ("done", "failed"):
                result_status = terminal
                result_handoff = str(result.data.get("handoff_md", ""))
                break
            if terminal == "blocked_on_user":
                result_status = "blocked_on_user"
                result_gate = result.data.get("gate_id")
                result_handoff = observation
                break
            if repeat_count >= 2:
                return self._finalize(
                    "stuck", f"repeated the same action three times: {action.tool}", turns, usage_total, touched
                )
            if turn_no - last_productive >= stall_window:
                return self._finalize(
                    "stuck",
                    f"stalled: {stall_kind} in the last {turn_no - last_productive} turns",
                    turns, usage_total, touched,
                )

            # Compact when the conversation actually nears the model's window,
            # measured by the server's own prompt_tokens for this call (exact,
            # not estimated). Use the space: 90% — but always fire BEFORE the
            # window squeezer (context_budget - 400) starts hard-truncating,
            # which is the lossy cliff compaction exists to avoid. The turn
            # counter survives only as a fallback for servers that report no
            # usage numbers.
            context_budget = self.role_model.context_window - self.role_model.max_output_tokens
            threshold = min(int(0.90 * context_budget), context_budget - 600)
            near_window = 0 < threshold < response.usage.prompt_tokens
            if near_window or (response.usage.prompt_tokens == 0
                               and len(turns) >= self.config.run.compact_after_turns):
                summary_of_compacted, turns = self._compact(summary_of_compacted, turns)

        if result_status is None:
            if hard_cap - last_productive < stall_window:
                # progressing right up to the ceiling: the work is real, the
                # item is just bigger than one attempt — say so for triage
                return self._finalize(
                    "stuck",
                    f"hit the turn ceiling ({hard_cap}) while still making progress — "
                    "the item is likely too big for one attempt",
                    turns, usage_total, touched,
                )
            return self._finalize(
                "stuck", f"ran out of turns ({max_turns}) without finishing", turns, usage_total, touched
            )
        summary = shorten(result_handoff.replace("\n", " "), 240) if result_handoff else result_status
        final = self._finalize(result_status, summary, turns, usage_total, touched)
        final.handoff_md = result_handoff
        final.gate_id = result_gate
        return final

    # -- message assembly ----------------------------------------------------
    def _build_messages(
        self,
        brief: str,
        turns: list[_Turn],
        task_recitation: str,
        turns_left: int,
        touched: list[str],
        summary_of_compacted: str,
    ) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
        opening = brief
        if summary_of_compacted:
            opening += f"\n\n## Progress so far (earlier turns, compacted)\n{summary_of_compacted}"
        messages.append({"role": "user", "content": opening})
        for i, turn in enumerate(turns):
            messages.append({"role": "assistant", "content": turn.assistant})
            observation = turn.observation
            if i == len(turns) - 1:
                observation += self._recitation(task_recitation, turns_left, touched)
            messages.append({"role": "user", "content": observation})
        self._enforce_window(messages)
        return messages

    @staticmethod
    def _recitation(task_recitation: str, turns_left: int, touched: list[str]) -> str:
        parts = [f"\n\n## Current task (reminder)\n{task_recitation}"]
        if touched:
            parts.append("Files you changed so far: " + ", ".join(touched[-10:]))
        parts.append(f"Turns remaining: {turns_left}.")
        if turns_left <= 3:
            # convergence pressure: 30 turns of work + a written report beats
            # 32 turns of work that dies silently at the ceiling
            parts.append(
                "FINAL TURNS: finish NOW with what you have — report your findings "
                "with an honest status. Partial results are useful; running out of "
                "turns with nothing written is not."
            )
        return "\n".join(parts)

    def _enforce_window(self, messages: list[dict]) -> None:
        budget = self.role_model.context_window - self.role_model.max_output_tokens - 400
        total = sum(approx_tokens(m["content"]) for m in messages)
        # squeeze oldest observations first; the brief and the last turns stay whole
        idx = 2
        while total > budget and idx < len(messages) - 2:
            content = messages[idx]["content"]
            shrunk = truncate_middle(content, 800)
            total -= approx_tokens(content) - approx_tokens(shrunk)
            messages[idx]["content"] = shrunk
            idx += 1

    # -- compaction --------------------------------------------------------------
    def _compact(self, existing_summary: str, turns: list[_Turn]) -> tuple[str, list[_Turn]]:
        keep_tail = 3
        if len(turns) <= keep_tail + 2:
            return existing_summary, turns
        old, recent = turns[:-keep_tail], turns[-keep_tail:]
        rendered = "\n\n".join(
            f"[assistant]\n{truncate_middle(t.assistant, 1200)}\n[result]\n{truncate_middle(t.observation, 800)}"
            for t in old
        )
        if existing_summary:
            rendered = f"(previous summary)\n{existing_summary}\n\n{rendered}"
        from ..context.summarizer import summarize  # local import: avoids a module cycle

        summary = summarize(
            self.client,
            self.config.models.utility,
            rendered,
            "Summarize this coding-agent progress: what was tried, what worked, what failed, "
            "current state of the files. Keep exact file names and error messages.",
            max_tokens=600,
        )
        return summary, recent

    # -- bookkeeping ----------------------------------------------------------
    def _journal_turn(self, turn: int, tool: str, ok: bool, usage: LLMUsage,
                      detail: str = "", target: str = "") -> None:
        self.journal.append(
            Kind.AGENT_TURN,
            actor=self.role_name,
            item=self.ctx.item_id,
            turn=turn,
            tool=tool,
            ok=ok,
            target=shorten(target, 80) if target else "",
            detail=shorten(detail, 160) if detail else "",
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )

    def _finalize(
        self,
        status: str,
        summary: str,
        turns: list[_Turn],
        usage: LLMUsage,
        touched: list[str],
    ) -> AttemptResult:
        salvage = ""
        if status in ("stuck", "error") and turns:
            # the proximate cause travels with the outcome: "ran out of turns"
            # alone tells a dashboard reader nothing
            head = next(iter(turns[-1].observation.strip().splitlines()), "")
            if head:
                summary = f"{summary} — last observation: {shorten(head, 140)}"
            if len(turns) >= 3:
                # a dead attempt's KNOWLEDGE must not die with it: distill the
                # transcript so the next attempt inherits what this one learned
                # instead of re-reading everything from scratch
                rendered = "\n\n".join(
                    f"[assistant]\n{truncate_middle(t.assistant, 800)}\n[result]\n"
                    f"{truncate_middle(t.observation, 600)}"
                    for t in turns
                )
                from ..context.summarizer import summarize

                salvage = summarize(
                    self.client,
                    self.config.models.utility,
                    rendered,
                    "This agent attempt died before finishing. Distill what it LEARNED for "
                    "its successor: files read and the key facts in them, changes made, what "
                    "was in progress, and what remained. Exact file names; no narrative.",
                    max_tokens=300,
                )
        transcript = self._render_transcript(turns, status, summary)
        subdir = f"attempts/{self.ctx.item_id or 'phase'}"
        name = f"{self.role_name}-{iso_now().replace(':', '')}.md"
        path = self.ctx.project.artifacts.write(name, transcript, subdir=subdir)
        rel = str(path.relative_to(self.ctx.project.root))
        log.agent(self.role_name, f"{status}: {shorten(summary, 100)}")
        return AttemptResult(
            status=status,  # type: ignore[arg-type]
            summary=summary,
            handoff_md=salvage,
            turns=len(turns),
            tokens_in=usage.prompt_tokens,
            tokens_out=usage.completion_tokens,
            transcript=rel,
            touched_files=touched,
        )

    def _render_transcript(self, turns: list[_Turn], status: str, summary: str) -> str:
        parts = [f"# {self.role_name} attempt — {status}", f"_{iso_now()}_", "", f"**Summary:** {summary}", ""]
        for i, turn in enumerate(turns, 1):
            parts += [f"## Turn {i}", "### Model", turn.assistant.strip(),
                      "### Observation", turn.observation.strip(), ""]
        return "\n".join(parts)


def build_handoff(role: str, item_id: str | None, result: AttemptResult) -> Handoff:
    return Handoff(
        from_role=role,
        item=item_id,
        summary=result.summary,
        state_of_work=result.handoff_md,
        touched_files=result.touched_files,
    )


# -- one-shot runtimes ----------------------------------------------------------


def one_shot_structured(
    caller: StructuredCaller,
    role_model: RoleModel,
    schema,
    system: str,
    sections: list[Section],
    reserve_output: int | None = None,
):
    """Pack prioritized sections into one user message and demand a schema."""
    packer = ContextPacker(
        context_window=role_model.context_window,
        reserve_output=reserve_output or role_model.max_output_tokens,
    )
    packed = packer.pack(sections, fixed_tokens=approx_tokens(system))
    if packed.dropped:
        log.debug(f"context sections dropped to fit budget: {packed.dropped}")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": packed.text},
    ]
    return caller.call(role_model, schema, messages)


def one_shot_prose(
    client: LLMClient,
    role_model: RoleModel,
    system: str,
    sections: list[Section],
    max_tokens: int | None = None,
    retries: int = 2,
) -> tuple[str, LLMUsage]:
    """One freeform document call with the same self-healing the structured
    path has: an answer stranded in the reasoning channel is recovered, and a
    reply whose thinking ate the whole output budget is retried with a grown
    budget instead of surfacing as an empty document."""
    packer = ContextPacker(
        context_window=role_model.context_window,
        reserve_output=max_tokens or role_model.max_output_tokens,
    )
    packed = packer.pack(sections, fixed_tokens=approx_tokens(system))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": packed.text},
    ]
    effective_max = max_tokens or role_model.max_output_tokens
    usage = LLMUsage()
    length_failures = 0
    for _ in range(retries + 1):
        # second budget death → thinking off for the retry: longer budgets
        # buy a thinking model longer rumination, not more document
        override = ({"chat_template_kwargs": {"enable_thinking": False,
                                              "reasoning_effort": "low"}}
                    if length_failures >= 2 else None)
        result = client.chat(role_model, messages, max_tokens=effective_max,
                             extra_body=override)
        usage += result.usage
        text = result.text
        if not strip_thinking(text).strip() and result.reasoning.strip():
            text = result.reasoning
        if role_model.thinking:
            text = strip_thinking(text)
        if text.strip():
            return text.strip(), usage
        if result.finish_reason == "length":
            length_failures += 1
            effective_max = int(effective_max * 1.5)
    return "", usage
