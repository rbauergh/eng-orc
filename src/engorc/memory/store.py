"""Memory backend protocol, composite store, and factory.

The composite keeps the local SQLite store as the durable superset of all
memory (writes always land there) and layers Letta on top for semantic
recall. Letta being down never blocks work: writes queue in an outbox and
`sync()` reconciles when it returns.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..config import Config
from ..obs.console import log
from .local_store import LocalMemoryStore
from .schema import MemoryHit, MemoryItem


@runtime_checkable
class MemoryStore(Protocol):
    name: str

    def save(self, item: MemoryItem) -> str: ...

    def search(
        self,
        query: str,
        k: int = 5,
        kinds: list[str] | None = None,
        project: str | None = None,
    ) -> list[MemoryHit]: ...

    def get_block(self, label: str) -> str: ...

    def set_block(self, label: str, value: str) -> None: ...

    def health(self) -> tuple[bool, str]: ...


class NullMemory:
    """backend: off — everything is a cheap no-op."""

    name = "off"

    def save(self, item: MemoryItem) -> str:
        return item.id

    def search(self, query, k=5, kinds=None, project=None) -> list[MemoryHit]:
        return []

    def get_block(self, label: str) -> str:
        return ""

    def set_block(self, label: str, value: str) -> None:
        pass

    def health(self) -> tuple[bool, str]:
        return True, "memory disabled"


class CompositeMemory:
    name = "composite"

    def __init__(self, local: LocalMemoryStore, letta=None):
        self.local = local
        self.letta = letta

    def save(self, item: MemoryItem) -> str:
        self.local.save(item)
        if self.letta is not None:
            try:
                self.letta.save(item)
            except Exception as exc:
                log.debug(f"letta save failed, queued for sync: {exc}")
                self.local.outbox_add(item.id)
        return item.id

    def search(
        self,
        query: str,
        k: int = 5,
        kinds: list[str] | None = None,
        project: str | None = None,
    ) -> list[MemoryHit]:
        if self.letta is not None:
            try:
                hits = self.letta.search(query, k=k, kinds=kinds, project=project)
                if hits:
                    return hits
            except Exception as exc:
                log.debug(f"letta search failed, falling back to local: {exc}")
        return self.local.search(query, k=k, kinds=kinds, project=project)

    def get_block(self, label: str) -> str:
        if self.letta is not None:
            try:
                value = self.letta.get_block(label)
                if value:
                    self.local.set_block(label, value)  # mirror for offline reads
                    return value
            except Exception:
                pass
        return self.local.get_block(label)

    def set_block(self, label: str, value: str) -> None:
        self.local.set_block(label, value)
        if self.letta is not None:
            try:
                self.letta.set_block(label, value)
            except Exception as exc:
                log.debug(f"letta block update failed: {exc}")

    def health(self) -> tuple[bool, str]:
        ok_local, msg_local = self.local.health()
        if self.letta is None:
            return ok_local, f"{msg_local}; letta: not configured"
        ok_letta, msg_letta = self.letta.health()
        return ok_local, f"{msg_local}; {msg_letta}"

    def sync(self) -> int:
        """Push outbox items to Letta; returns how many synced."""
        if self.letta is None:
            return 0
        synced = 0
        for item in self.local.outbox_items():
            try:
                self.letta.save(item)
                self.local.outbox_remove(item.id)
                synced += 1
            except Exception as exc:
                log.debug(f"letta sync stopped: {exc}")
                break
        return synced

    def curate(self, digest: str) -> str:
        if self.letta is not None:
            try:
                return self.letta.curate(digest)
            except Exception as exc:
                log.debug(f"letta curation skipped: {exc}")
        return ""


def open_memory(config: Config) -> CompositeMemory | NullMemory:
    if config.memory.backend == "off":
        return NullMemory()
    local = LocalMemoryStore(config.home / "memory.db")
    letta = None
    if config.memory.backend in ("auto", "letta"):
        from .letta_store import LettaMemoryStore

        candidate = LettaMemoryStore(config)
        ok, reason = candidate.health()
        if ok:
            letta = candidate
        elif config.memory.backend == "letta":
            log.warn(f"memory backend 'letta' requested but unavailable ({reason}); using local store")
        else:
            log.debug(f"letta not reachable ({reason}); memory is local-only")
    return CompositeMemory(local, letta)
