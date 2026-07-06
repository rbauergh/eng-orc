"""Asynchronous user-interaction gates.

Agents never block on stdin. When an agent has a genuinely blocking question
(one whose answer changes the architecture), it opens a gate; the project is
parked in blocked_on_user state and the scheduler moves on to other projects.
The user sees gates in `orc inbox` and answers with `orc answer`, which
unparks the project on its next scheduling pass.

Storage is an append-only fold (gates.jsonl of opened/answered/dismissed
records) so gate history survives crashes and is greppable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .fsio import append_jsonl, iter_jsonl
from .util import iso_now, new_id

GateStatus = Literal["open", "answered", "dismissed"]


class Gate(BaseModel):
    id: str = Field(default_factory=lambda: new_id("gate"))
    ts: str = Field(default_factory=iso_now)
    from_role: str = "system"
    phase: str = ""
    item: str | None = None
    question: str = ""
    context: str = ""  # what the agent already assumed / why this matters
    options: list[str] = Field(default_factory=list)
    status: GateStatus = "open"
    answer: str = ""
    answered_ts: str | None = None


class GateBook:
    def __init__(self, path: Path):
        self.path = path

    def _fold(self) -> dict[str, Gate]:
        gates: dict[str, Gate] = {}
        for raw in iter_jsonl(self.path):
            record_type = raw.pop("record", "opened")
            if record_type == "opened":
                gate = Gate.model_validate(raw)
                gates[gate.id] = gate
            elif record_type in ("answered", "dismissed"):
                gate = gates.get(raw.get("id", ""))
                if gate is None:
                    continue
                gate.status = "answered" if record_type == "answered" else "dismissed"
                gate.answer = raw.get("answer", "")
                gate.answered_ts = raw.get("ts")
        return gates

    def open(
        self,
        question: str,
        from_role: str = "system",
        phase: str = "",
        item: str | None = None,
        context: str = "",
        options: list[str] | None = None,
    ) -> Gate:
        gate = Gate(
            question=question,
            from_role=from_role,
            phase=phase,
            item=item,
            context=context,
            options=options or [],
        )
        payload = gate.model_dump(exclude_none=True)
        payload["record"] = "opened"
        append_jsonl(self.path, payload)
        return gate

    def answer(self, gate_id: str, answer: str) -> Gate:
        gates = self._fold()
        gate_id = self._resolve(gates, gate_id)
        gate = gates[gate_id]
        if gate.status != "open":
            raise ValueError(f"gate {gate_id} is already {gate.status}")
        append_jsonl(self.path, {"record": "answered", "id": gate_id, "answer": answer, "ts": iso_now()})
        gate.status = "answered"
        gate.answer = answer
        gate.answered_ts = iso_now()
        return gate

    def dismiss(self, gate_id: str) -> Gate:
        gates = self._fold()
        gate_id = self._resolve(gates, gate_id)
        append_jsonl(self.path, {"record": "dismissed", "id": gate_id, "ts": iso_now()})
        gate = gates[gate_id]
        gate.status = "dismissed"
        return gate

    @staticmethod
    def _resolve(gates: dict[str, Gate], gate_id: str) -> str:
        """Accepts full ids or unambiguous prefixes (CLI convenience)."""
        if gate_id in gates:
            return gate_id
        matches = [gid for gid in gates if gid.startswith(gate_id)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"gate not found: {gate_id}")
        raise KeyError(f"ambiguous gate id prefix {gate_id!r}: {matches}")

    def all(self) -> list[Gate]:
        return sorted(self._fold().values(), key=lambda g: g.ts)

    def open_gates(self) -> list[Gate]:
        return [g for g in self.all() if g.status == "open"]

    def answered_unconsumed(self, consumed_ids: set[str]) -> list[Gate]:
        return [g for g in self.all() if g.status == "answered" and g.id not in consumed_ids]

    def get(self, gate_id: str) -> Gate:
        gates = self._fold()
        return gates[self._resolve(gates, gate_id)]
