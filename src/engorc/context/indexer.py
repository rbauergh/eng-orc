"""Per-project semantic code index (LlamaIndex + Chroma + local embeddings).

The index is an accelerator, never a dependency: every consumer must work
when this module reports unavailable (missing packages, embedding endpoint
down, indexing disabled). That keeps the orchestrator functional on a bare
machine and makes the semantic layer a pure upgrade.

Incremental refresh uses IngestionPipeline with a persisted docstore and
UPSERTS_AND_DELETE: unchanged files are skipped by content hash, changed
files re-embed only their own chunks, deleted files leave the index. All
heavyweight imports are deferred to call time.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..config import Config
from ..events import Journal, Kind
from ..llm.client import LLMClient
from .files import iter_source_files, language_for, read_capped

if TYPE_CHECKING:  # pragma: no cover
    from ..project import Project


@dataclass
class Snippet:
    path: str
    text: str
    score: float = 0.0
    source: str = "vector"
    span: tuple[int, int] | None = None


class IndexUnavailable(RuntimeError):
    pass


class CodebaseIndex:
    def __init__(self, project: Project, config: Config, client: LLMClient):
        self.project = project
        self.config = config
        self.client = client
        self.index_dir = project.index_dir
        self.chroma_dir = self.index_dir / "chroma"
        self.pipeline_dir = self.index_dir / "pipeline"

    # -- availability ---------------------------------------------------------
    def status(self) -> tuple[bool, str]:
        if not self.config.index.enabled:
            return False, "indexing disabled in config"
        try:
            import chromadb  # noqa: F401
            import llama_index.core  # noqa: F401
            from llama_index.embeddings.openai_like import OpenAILikeEmbedding  # noqa: F401
            from llama_index.vector_stores.chroma import ChromaVectorStore  # noqa: F401
        except ImportError as exc:
            return False, f"index packages not installed: {exc}"
        return True, "ok"

    def _require(self) -> None:
        ok, reason = self.status()
        if not ok:
            raise IndexUnavailable(reason)

    def has_data(self) -> bool:
        return self.chroma_dir.exists()

    # -- building blocks -------------------------------------------------------
    def _embed_model(self):
        from llama_index.embeddings.openai_like import OpenAILikeEmbedding

        server = self.config.server
        embedder = self.config.models.embedder
        return OpenAILikeEmbedding(
            model_name=embedder.model,
            api_base=(server.embeddings_url or server.base_url),
            api_key=server.api_key,
            embed_batch_size=embedder.batch_size,
            timeout=server.request_timeout,
            max_retries=3,
        )

    def _vector_store(self):
        import chromadb
        from llama_index.vector_stores.chroma import ChromaVectorStore

        client = chromadb.PersistentClient(path=str(self.chroma_dir))
        collection = client.get_or_create_collection("workroom")
        return ChromaVectorStore(chroma_collection=collection)

    def _splitter(self):
        """Per-document language dispatch: CodeSplitter when a tree-sitter
        grammar exists for the file's language, sentence splitting otherwise."""
        from llama_index.core.node_parser import CodeSplitter, SentenceSplitter
        from llama_index.core.schema import TransformComponent

        index_cfg = self.config.index
        sentence = SentenceSplitter(
            chunk_size=max(256, index_cfg.max_chars_per_chunk // 4),
            chunk_overlap=64,
        )
        code_splitters: dict[str, object] = {}

        def splitter_for(lang: str | None):
            if not lang:
                return sentence
            if lang not in code_splitters:
                try:
                    code_splitters[lang] = CodeSplitter(
                        language=lang,
                        chunk_lines=index_cfg.chunk_lines,
                        chunk_lines_overlap=index_cfg.chunk_overlap_lines,
                        max_chars=index_cfg.max_chars_per_chunk,
                    )
                except Exception:
                    code_splitters[lang] = sentence  # grammar missing from the pack
            return code_splitters[lang]

        class PolyglotSplitter(TransformComponent):
            def __call__(self, nodes, **kwargs):
                out = []
                for node in nodes:
                    lang = (node.metadata or {}).get("lang")
                    split = splitter_for(lang)
                    try:
                        out.extend(split([node]))
                    except Exception:
                        out.extend(sentence([node]))
                return out

        return PolyglotSplitter()

    def _documents(self):
        from llama_index.core import Document

        workroom = self.project.workroom
        docs = []
        for path in iter_source_files(workroom, self.config.index.ignore, self.config.index.max_kb):
            rel = str(path.relative_to(workroom))
            text = read_capped(path)
            if not text.strip():
                continue
            docs.append(
                Document(
                    text=text,
                    id_=rel,
                    metadata={"path": rel, "lang": language_for(path) or "text"},
                    excluded_embed_metadata_keys=["lang"],
                )
            )
        return docs

    # -- operations ----------------------------------------------------------
    def refresh(self, journal: Journal | None = None) -> dict:
        """Sync the index with the workroom. Returns stats for the journal."""
        self._require()
        self._probe_embeddings()

        from llama_index.core.ingestion import IngestionPipeline
        from llama_index.core.ingestion.pipeline import DocstoreStrategy
        from llama_index.core.storage.docstore import SimpleDocumentStore

        documents = self._documents()
        pipeline = IngestionPipeline(
            transformations=[self._splitter(), self._embed_model()],
            vector_store=self._vector_store(),
            docstore=SimpleDocumentStore(),
            docstore_strategy=DocstoreStrategy.UPSERTS_AND_DELETE,
        )
        if self.pipeline_dir.exists():
            try:
                pipeline.load(str(self.pipeline_dir))
            except Exception:
                pass  # cache corruption just means a full re-embed
        nodes = pipeline.run(documents=documents)
        pipeline.persist(str(self.pipeline_dir))
        stats = {"files": len(documents), "nodes_upserted": len(nodes)}
        if journal is not None:
            journal.append(Kind.INDEX_REFRESH, **stats)
        return stats

    def _probe_embeddings(self) -> None:
        try:
            self.client.embeddings(["ping"], model=self.config.models.embedder.model)
        except Exception as exc:
            raise IndexUnavailable(f"embedding endpoint unreachable: {exc}") from exc

    def search(self, query: str, top_k: int | None = None) -> list[Snippet]:
        self._require()
        if not self.has_data():
            return []
        from llama_index.core import VectorStoreIndex

        index = VectorStoreIndex.from_vector_store(self._vector_store(), embed_model=self._embed_model())
        retriever = index.as_retriever(similarity_top_k=top_k or self.config.index.top_k)
        results = []
        for hit in retriever.retrieve(query):
            metadata = hit.metadata or {}
            results.append(
                Snippet(
                    path=metadata.get("path", "?"),
                    text=hit.get_content(),
                    score=float(hit.score or 0.0),
                    source="vector",
                )
            )
        return results

    def rebuild(self, journal: Journal | None = None) -> dict:
        if self.chroma_dir.exists():
            shutil.rmtree(self.chroma_dir)
        if self.pipeline_dir.exists():
            shutil.rmtree(self.pipeline_dir)
        return self.refresh(journal=journal)
