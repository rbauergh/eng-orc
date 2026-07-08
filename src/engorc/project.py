"""The Project aggregate: one directory owning all state for one mission.

Layout under <home>/projects/<slug>/:

    project.json        meta: phase, state, priority, counters
    mission.md          the goal as given, plus dated user amendments
    charter.yaml        objective, assumptions(+confidence), success criteria
    design.md           living design document (versioned via artifacts/)
    plan.yaml           the work-item DAG (human-editable)
    decisions.jsonl     ADR-lite log
    gates.jsonl         async user questions (open/answered fold)
    journal/            append-only event shards
    artifacts/          handoffs, reviews, transcripts, reports, doc versions
    index/              vector store + repo-map cache
    state/              langgraph checkpoints, consumed-gate marks, scratch
    workroom/           the code being developed (its own git repo), unless
                        the project is attached to an external repository

The directory is the whole truth: deleting the process loses nothing, and
`orc resume` rebuilds working context from these files alone.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .artifacts import ArtifactStore
from .config import Config
from .decisions import DecisionLog
from .events import Journal, Kind
from .fsio import atomic_write_json, atomic_write_text, ensure_dir, read_json, read_yaml
from .gates import GateBook
from .plan import Plan, load_plan, save_plan
from .util import iso_now

Phase = Literal["charter", "design", "plan", "build", "wrap", "done"]
State = Literal["active", "paused", "blocked_on_user", "done", "abandoned"]

PHASE_ORDER: tuple[str, ...] = ("charter", "design", "plan", "build", "wrap", "done")
RUNNABLE_STATES: tuple[str, ...] = ("active",)


class ProjectMeta(BaseModel):
    slug: str
    title: str
    phase: Phase = "charter"
    state: State = "active"
    priority: int = 3  # 1 (urgent) .. 5 (someday)
    created: str = Field(default_factory=iso_now)
    updated: str = Field(default_factory=iso_now)
    last_active: str = Field(default_factory=iso_now)
    tags: list[str] = Field(default_factory=list)
    external_workroom: str | None = None  # absolute path when attached to an existing repo
    state_reason: str = ""
    counters: dict[str, int] = Field(default_factory=dict)

    def bump(self, **increments: int) -> None:
        for key, delta in increments.items():
            self.counters[key] = self.counters.get(key, 0) + delta
        self.updated = iso_now()


class Project:
    def __init__(self, root: Path, config: Config):
        self.root = root
        self.config = config

    # -- paths ---------------------------------------------------------------
    @property
    def meta_path(self) -> Path:
        return self.root / "project.json"

    @property
    def mission_path(self) -> Path:
        return self.root / "mission.md"

    @property
    def charter_path(self) -> Path:
        return self.root / "charter.yaml"

    @property
    def design_path(self) -> Path:
        return self.root / "design.md"

    @property
    def plan_path(self) -> Path:
        return self.root / "plan.yaml"

    @property
    def state_dir(self) -> Path:
        return ensure_dir(self.root / "state")

    @property
    def index_dir(self) -> Path:
        return ensure_dir(self.root / "index")

    @property
    def checkpoint_db(self) -> Path:
        return self.state_dir / "checkpoints.sqlite"

    @property
    def workroom(self) -> Path:
        meta = self.meta
        if meta.external_workroom:
            return Path(meta.external_workroom)
        return ensure_dir(self.root / "workroom")

    # -- stores ----------------------------------------------------------------
    @cached_property
    def journal(self) -> Journal:
        return Journal(self.root / "journal")

    @cached_property
    def gates(self) -> GateBook:
        return GateBook(self.root / "gates.jsonl")

    @cached_property
    def decisions(self) -> DecisionLog:
        return DecisionLog(self.root / "decisions.jsonl")

    @cached_property
    def artifacts(self) -> ArtifactStore:
        return ArtifactStore(self.root / "artifacts")

    # -- meta ------------------------------------------------------------------
    @property
    def meta(self) -> ProjectMeta:
        data = read_json(self.meta_path)
        if data is None:
            raise FileNotFoundError(f"not a project directory (missing project.json): {self.root}")
        return ProjectMeta.model_validate(data)

    def save_meta(self, meta: ProjectMeta) -> None:
        meta.updated = iso_now()
        atomic_write_json(self.meta_path, meta.model_dump(exclude_none=True))

    @property
    def slug(self) -> str:
        return self.meta.slug

    def touch_active(self) -> None:
        meta = self.meta
        meta.last_active = iso_now()
        self.save_meta(meta)

    def set_phase(self, phase: Phase) -> None:
        meta = self.meta
        if meta.phase != phase:
            meta.phase = phase
            self.save_meta(meta)
            self.journal.append(Kind.PHASE_ENTERED, phase=phase)

    def set_state(self, state: State, reason: str = "") -> None:
        meta = self.meta
        if meta.state != state or reason != meta.state_reason:
            meta.state = state
            meta.state_reason = reason
            self.save_meta(meta)
            self.journal.append(Kind.PROJECT_STATE, state=state, reason=reason)

    def is_runnable(self) -> bool:
        meta = self.meta
        if meta.state in RUNNABLE_STATES:
            return True
        # An answered gate unparks a blocked project on the next pass.
        if meta.state == "blocked_on_user" and self.unconsumed_answers():
            return True
        return False

    # -- mission -----------------------------------------------------------------
    def mission(self) -> str:
        if self.mission_path.exists():
            return self.mission_path.read_text(encoding="utf-8")
        return ""

    def write_mission(self, text: str) -> None:
        atomic_write_text(self.mission_path, text)

    def append_mission_note(self, note: str, author: str = "user") -> None:
        current = self.mission()
        stamped = f"\n\n---\n_{author} amendment, {iso_now()}:_\n\n{note.strip()}\n"
        atomic_write_text(self.mission_path, current + stamped)
        self.journal.append(Kind.USER_NOTE, actor=author, note=note)

    # -- change requests -----------------------------------------------------------
    # `orc request` writes intent and returns; the scheduler does the model
    # work (investigation, planning) under its own lease and visibility.
    @property
    def requests_path(self) -> Path:
        return self.root / "requests.jsonl"

    def queue_request(self, text: str) -> str:
        from .fsio import append_jsonl
        from .util import new_id

        request_id = new_id("req")
        append_jsonl(self.requests_path,
                     {"record": "queued", "id": request_id, "text": text, "ts": iso_now()})
        return request_id

    def pending_requests(self) -> list[dict]:
        from .fsio import iter_jsonl

        rows = list(iter_jsonl(self.requests_path))
        done = {row.get("id") for row in rows if row.get("record") == "done"}
        return [row for row in rows
                if row.get("record") == "queued" and row.get("id") not in done]

    def mark_request_done(self, request_id: str) -> None:
        from .fsio import append_jsonl

        append_jsonl(self.requests_path,
                     {"record": "done", "id": request_id, "ts": iso_now()})

    # -- charter / plan ------------------------------------------------------------
    def charter(self) -> dict | None:
        return read_yaml(self.charter_path, default=None)

    def load_plan(self) -> Plan:
        return load_plan(self.plan_path)

    def save_plan(self, plan: Plan) -> None:
        save_plan(self.plan_path, plan)

    # -- gate consumption -----------------------------------------------------------
    # Answers are injected into agent context exactly once; consumption marks
    # live in state/ so a crash between injection and next save re-injects
    # (idempotent) rather than losing the answer.
    @property
    def _consumed_gates_path(self) -> Path:
        return self.state_dir / "consumed_gates.json"

    def consumed_gate_ids(self) -> set[str]:
        return set(read_json(self._consumed_gates_path, default=[]))

    def mark_gates_consumed(self, gate_ids: list[str]) -> None:
        consumed = self.consumed_gate_ids() | set(gate_ids)
        atomic_write_json(self._consumed_gates_path, sorted(consumed))

    def unconsumed_answers(self):
        return self.gates.answered_unconsumed(self.consumed_gate_ids())
