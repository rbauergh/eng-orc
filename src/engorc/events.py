"""Append-only project journal.

Every meaningful occurrence — agent turns, tool calls, verification runs,
decisions, gate traffic, model swaps, errors — is appended as one JSON line.
The journal is the project's ground truth for "what happened": resume briefs,
wrap-up digests, and long-term memory extraction are all folds over it.
Shards are monthly so files stay greppable as projects grow.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from .fsio import append_jsonl, ensure_dir, iter_jsonl
from .util import iso_now, new_id


class Kind:
    PROJECT_CREATED = "project_created"
    PROJECT_STATE = "project_state"
    PHASE_ENTERED = "phase_entered"
    STEP = "step"
    AGENT_TURN = "agent_turn"
    STRUCTURED_CALL = "structured_call"
    TOOL_CALL = "tool_call"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_FINISHED = "attempt_finished"
    ITEM_STATUS = "item_status"
    VERIFY_RUN = "verify_run"
    REVIEW = "review"
    GATE_OPENED = "gate_opened"
    GATE_ANSWERED = "gate_answered"
    DECISION = "decision"
    COMMIT = "commit"
    MODEL_SWAP = "model_swap"
    INDEX_REFRESH = "index_refresh"
    MEMORY_SAVED = "memory_saved"
    MEMORY_RECALLED = "memory_recalled"
    RESUME = "resume"
    USER_NOTE = "user_note"
    ERROR = "error"


class Event(BaseModel):
    id: str
    ts: str
    kind: str
    actor: str = "system"
    item: str | None = None  # work-item id, when the event belongs to one
    payload: dict = Field(default_factory=dict)


class Journal:
    def __init__(self, dirpath: Path):
        self.dir = ensure_dir(dirpath)

    def _shard_for(self, ts: str) -> Path:
        return self.dir / f"events-{ts[:7]}.jsonl"  # YYYY-MM

    def _shards(self) -> list[Path]:
        return sorted(self.dir.glob("events-*.jsonl"))

    def append(self, kind: str, actor: str = "system", item: str | None = None, **payload) -> Event:
        event = Event(id=new_id("ev"), ts=iso_now(), kind=kind, actor=actor, item=item, payload=payload)
        append_jsonl(self._shard_for(event.ts), event.model_dump(exclude_none=True))
        return event

    def iter_events(
        self,
        kinds: Sequence[str] | None = None,
        item: str | None = None,
        since_ts: str | None = None,
    ) -> Iterator[Event]:
        for shard in self._shards():
            if since_ts and shard.name < f"events-{since_ts[:7]}.jsonl":
                continue
            for raw in iter_jsonl(shard):
                if kinds and raw.get("kind") not in kinds:
                    continue
                if item and raw.get("item") != item:
                    continue
                if since_ts and raw.get("ts", "") < since_ts:
                    continue
                yield Event.model_validate(raw)

    def tail(self, n: int = 50, kinds: Sequence[str] | None = None) -> list[Event]:
        buf: list[Event] = []
        for event in self.iter_events(kinds=kinds):
            buf.append(event)
            if len(buf) > n * 4:  # keep memory bounded while folding large journals
                buf = buf[-n:]
        return buf[-n:]

    def last(self, kind: str) -> Event | None:
        found = None
        for event in self.iter_events(kinds=[kind]):
            found = event
        return found

    def count(self) -> int:
        return sum(1 for _ in self.iter_events())
