"""Letta-backed semantic memory.

Uses one persistent "librarian" agent whose archival passages hold all
memory items (tagged kind:/project: for filtered recall) and whose memory
blocks hold the always-relevant context (user profile, engineering
conventions). Writes and searches go through the pure passages API — only
the embedding model runs, so recall is cheap and never contends for the GPU
with foreground work. Optional curation sends the librarian a digest to
fold into its blocks; that DOES run the small utility model.

The letta-client import is deferred and every operation is fail-soft: the
composite store falls back to the local store and queues unsynced items.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..fsio import atomic_write_json, read_json
from .schema import MemoryHit, MemoryItem

PERSONA = (
    "You are the librarian for a one-person software organization. You keep "
    "concise, high-signal memory: engineering conventions the user cares "
    "about, lessons from past projects, and cards summarizing finished work. "
    "When given a digest, fold durable insights into your memory blocks and "
    "keep them tight — drop stale or low-value detail."
)

BLOCK_LABELS = ("user_profile", "engineering_conventions")


class LettaUnavailable(RuntimeError):
    pass


class LettaMemoryStore:
    name = "letta"

    def __init__(self, config: Config):
        self.config = config
        self.memory_cfg = config.memory
        self._client = None
        self._agent_id: str | None = None
        self._cache_path: Path = config.home / "letta-agent.json"

    # -- plumbing -------------------------------------------------------------
    def _sdk(self):
        try:
            from letta_client import Letta
        except ImportError as exc:
            raise LettaUnavailable(f"letta-client not installed: {exc}") from exc
        return Letta

    def client(self):
        if self._client is None:
            import httpx

            Letta = self._sdk()
            self._client = Letta(
                api_key=self.memory_cfg.letta_token or "local",
                base_url=self.memory_cfg.letta_base_url,
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=max(300.0, self.memory_cfg.timeout),
                    write=30.0,
                    pool=10.0,
                ),
                max_retries=1,
            )
        return self._client

    def health(self) -> tuple[bool, str]:
        try:
            self.client().health.check()
            return True, f"letta at {self.memory_cfg.letta_base_url}"
        except LettaUnavailable as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"letta unreachable: {exc}"

    # -- agent lifecycle -----------------------------------------------------------
    def _model_handles(self) -> tuple[str, str]:
        llm = self.memory_cfg.letta_model_handle or f"openai/{self.config.models.utility.model}"
        embed = (
            self.memory_cfg.letta_embedding_handle
            or f"openai/{self.config.models.embedder.model}"
        )
        return llm, embed

    def agent_id(self) -> str:
        if self._agent_id:
            return self._agent_id
        cached = read_json(self._cache_path, default={})
        agent_id = cached.get("agent_id")
        client = self.client()
        if agent_id:
            try:
                client.agents.retrieve(agent_id)
                self._agent_id = agent_id
                return agent_id
            except Exception:
                pass  # stale cache: the server was reset — recreate below
        for agent in self._list_agents(client):
            if getattr(agent, "name", "") == self.memory_cfg.letta_agent:
                self._agent_id = agent.id
                atomic_write_json(self._cache_path, {"agent_id": agent.id})
                return agent.id
        llm_handle, embed_handle = self._model_handles()
        agent = client.agents.create(
            name=self.memory_cfg.letta_agent,
            model=llm_handle,
            embedding=embed_handle,
            context_window_limit=8192,  # local utility models choke on Letta's 128k default
            memory_blocks=[
                {"label": "persona", "value": PERSONA, "limit": 2500},
                {"label": "user_profile", "value": "", "limit": 3000},
                {"label": "engineering_conventions", "value": "", "limit": 5000},
            ],
        )
        self._agent_id = agent.id
        atomic_write_json(self._cache_path, {"agent_id": agent.id})
        return agent.id

    @staticmethod
    def _list_agents(client) -> list:
        try:
            page = client.agents.list(limit=100)
        except TypeError:
            page = client.agents.list()
        return list(page or [])

    # -- items ------------------------------------------------------------------
    def save(self, item: MemoryItem) -> str:
        client = self.client()
        client.agents.passages.create(
            agent_id=self.agent_id(),
            text=item.render_passage(),
            tags=item.passage_tags() + [f"id:{item.id}"],
        )
        return item.id

    def search(
        self,
        query: str,
        k: int = 5,
        kinds: list[str] | None = None,
        project: str | None = None,
    ) -> list[MemoryHit]:
        client = self.client()
        kwargs: dict = {"agent_id": self.agent_id(), "query": query, "top_k": k}
        if kinds and len(kinds) == 1:
            kwargs["tags"] = [f"kind:{kinds[0]}"]
            kwargs["tag_match_mode"] = "all"
        response = client.agents.passages.search(**kwargs)
        results = getattr(response, "results", None) or getattr(response, "passages", None) or response
        hits: list[MemoryHit] = []
        for entry in list(results or [])[: k * 3]:
            text = getattr(entry, "text", None) or getattr(entry, "content", "") or ""
            score = float(getattr(entry, "score", 0.0) or 0.0)
            tags = list(getattr(entry, "tags", []) or [])
            item = self._passage_to_item(text, tags)
            if kinds and item.kind not in kinds:
                continue
            if project is not None and item.project not in (project, "", "global"):
                continue
            hits.append(MemoryHit(item=item, score=score, backend=self.name))
            if len(hits) >= k:
                break
        return hits

    @staticmethod
    def _passage_to_item(text: str, tags: list[str]) -> MemoryItem:
        kind = "note"
        project = ""
        item_id = ""
        keep_tags = []
        for tag in tags:
            if tag.startswith("kind:"):
                kind = tag.split(":", 1)[1]
            elif tag.startswith("project:"):
                project = tag.split(":", 1)[1]
            elif tag.startswith("id:"):
                item_id = tag.split(":", 1)[1]
            else:
                keep_tags.append(tag)
        title, _, body = text.partition("\n\n")
        title = title.strip().lstrip("[").split("]", 1)[-1].strip() or title.strip()
        valid_kinds = {"lesson", "convention", "postmortem", "project_card", "decision", "note"}
        item = MemoryItem(
            kind=kind if kind in valid_kinds else "note",  # type: ignore[arg-type]
            project=project,
            title=title[:200],
            body=body.strip() or text,
            tags=keep_tags,
        )
        if item_id:
            item.id = item_id
        return item

    # -- blocks ----------------------------------------------------------------
    def get_block(self, label: str) -> str:
        client = self.client()
        block = client.agents.blocks.retrieve(label, agent_id=self.agent_id())
        return getattr(block, "value", "") or ""

    def set_block(self, label: str, value: str) -> None:
        client = self.client()
        client.agents.blocks.update(label, agent_id=self.agent_id(), value=value)

    # -- curation -----------------------------------------------------------------
    def curate(self, digest: str, max_steps: int = 6) -> str:
        """Ask the librarian agent to fold a digest into its memory blocks.
        Runs the utility LLM — call it at wrap-up, when the GPU is quiet."""
        client = self.client()
        response = client.agents.messages.create(
            agent_id=self.agent_id(),
            input=(
                "Digest from a just-finished work session follows. Update your "
                "memory blocks with anything durably useful (conventions, "
                "user preferences, hard-won lessons). Reply with one line "
                "summarizing what you kept.\n\n" + digest
            ),
            max_steps=max_steps,
        )
        for message in getattr(response, "messages", []) or []:
            if getattr(message, "message_type", "") == "assistant_message":
                content = getattr(message, "content", "")
                if isinstance(content, str):
                    return content
        return ""
