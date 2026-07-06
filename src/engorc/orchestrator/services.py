"""Service container: one place that wires config, LLM client, memory,
registry, and per-project context machinery together.

Constructed once per process (CLI entry or test); everything downstream
receives it explicitly. Tests swap the client for a FakeLLM and the whole
stack runs GPU-less.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING

from ..config import Config
from ..events import Journal
from ..llm.client import LLMClient
from ..llm.server import SwapServer
from ..llm.structured import StructuredCaller
from ..memory.store import CompositeMemory, NullMemory, open_memory
from ..obs.console import log
from ..registry import Registry

if TYPE_CHECKING:  # pragma: no cover
    from ..context.indexer import CodebaseIndex
    from ..context.repomap import RepoMap
    from ..context.retriever import HybridRetriever
    from ..project import Project


@dataclass
class ProjectContext:
    """Lazy per-project context machinery, built on first use and cached."""

    repomap: RepoMap
    retriever: HybridRetriever
    index: CodebaseIndex | None


@dataclass
class Services:
    config: Config
    client: LLMClient
    registry: Registry
    memory: CompositeMemory | NullMemory
    swap: SwapServer
    _project_contexts: dict[str, ProjectContext] = dataclass_field(default_factory=dict)

    @classmethod
    def build(cls, config: Config, client: LLMClient | None = None) -> Services:
        client = client or LLMClient(config.server)
        return cls(
            config=config,
            client=client,
            registry=Registry(config),
            memory=open_memory(config),
            swap=SwapServer(config.server),
        )

    def caller(self, journal: Journal, actor: str) -> StructuredCaller:
        return StructuredCaller(self.client, journal=journal, actor=actor)

    def context_for(self, project: Project) -> ProjectContext:
        slug = project.root.name
        if slug in self._project_contexts:
            return self._project_contexts[slug]
        from ..context.indexer import CodebaseIndex
        from ..context.repomap import RepoMap
        from ..context.retriever import HybridRetriever

        repomap = RepoMap(
            workroom=project.workroom,
            cache_dir=project.index_dir,
            ignore=self.config.index.ignore,
            max_kb=self.config.index.max_file_kb,
        )
        index: CodebaseIndex | None = CodebaseIndex(project, self.config, self.client)
        ok, reason = index.status()
        if not ok:
            log.debug(f"semantic index disabled for {slug}: {reason}")
            index = None
        retriever = HybridRetriever(
            workroom=project.workroom,
            config=self.config,
            repomap=repomap,
            index=index,
        )
        ctx = ProjectContext(repomap=repomap, retriever=retriever, index=index)
        self._project_contexts[slug] = ctx
        return ctx

    def refresh_index(self, project: Project) -> None:
        """Best-effort semantic index sync; the index is an accelerator, never a gate."""
        ctx = self.context_for(project)
        if ctx.index is None:
            return
        try:
            ctx.index.refresh(journal=project.journal)
        except Exception as exc:
            log.debug(f"index refresh skipped: {exc}")
