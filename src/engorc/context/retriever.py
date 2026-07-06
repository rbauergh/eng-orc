"""Hybrid retrieval: fuse vector search, symbol definitions, exact grep, and
focus-file excerpts into one token-budgeted context section.

Each channel catches what the others miss — vectors find concepts, symbols
find definitions, grep finds exact identifiers — and every channel degrades
independently (no index → symbols+grep still work; no ctags → grep still
works), so agents always get the best context the machine can produce.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..util import approx_tokens
from .files import iter_source_files, read_capped
from .repomap import RepoMap

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "should", "must", "when",
    "where", "what", "have", "will", "make", "add", "use", "using", "into", "each",
    "file", "files", "code", "test", "tests", "function", "class", "implement",
    "create", "update", "support", "ensure", "write",
}


@dataclass
class ContextChunk:
    path: str
    text: str
    label: str
    start: int = 0
    end: int = 0


def extract_terms(query: str, limit: int = 6) -> list[str]:
    """Identifier-looking tokens, rarest-looking first (long, mixed-case, snake)."""
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", query)
    seen: dict[str, int] = {}
    for token in tokens:
        if token.lower() in _STOPWORDS:
            continue
        score = len(token)
        if "_" in token or (token != token.lower() and token != token.upper()):
            score += 6  # well-named identifiers are the highest-signal probes
        seen[token] = max(seen.get(token, 0), score)
    ranked = sorted(seen, key=lambda t: -seen[t])
    return ranked[:limit]


class HybridRetriever:
    def __init__(
        self,
        workroom: Path,
        config: Config,
        repomap: RepoMap,
        index=None,  # CodebaseIndex | None
        counter: Callable[[str], int] | None = None,
    ):
        self.workroom = workroom
        self.config = config
        self.repomap = repomap
        self.index = index
        self.count = counter or approx_tokens

    # -- channels ----------------------------------------------------------
    def _vector_chunks(self, query: str) -> list[ContextChunk]:
        if self.index is None:
            return []
        try:
            snippets = self.index.search(query)
        except Exception:
            return []
        return [
            ContextChunk(path=s.path, text=s.text.strip(), label=f"semantic match (score {s.score:.2f})")
            for s in snippets
        ]

    def _symbol_chunks(self, terms: list[str]) -> list[ContextChunk]:
        chunks: list[ContextChunk] = []
        for term in terms:
            for rel, symbol in self.repomap.find_symbol(term)[:2]:
                excerpt = self._excerpt(rel, symbol.line, symbol.line + 30)
                if excerpt:
                    chunks.append(
                        ContextChunk(
                            path=rel,
                            text=excerpt,
                            label=f"definition of {symbol.name}",
                            start=symbol.line,
                            end=symbol.line + 30,
                        )
                    )
        return chunks

    def _grep_chunks(self, terms: list[str]) -> list[ContextChunk]:
        chunks: list[ContextChunk] = []
        rg = shutil.which("rg")
        for term in terms[:4]:
            hits = self._rg_hits(rg, term) if rg else self._py_hits(term)
            for rel, line in hits[:2]:
                excerpt = self._excerpt(rel, max(1, line - 3), line + 8)
                if excerpt:
                    chunks.append(
                        ContextChunk(path=rel, text=excerpt, label=f"mentions {term}", start=line - 3, end=line + 8)
                    )
        return chunks

    def _rg_hits(self, rg: str, term: str) -> list[tuple[str, int]]:
        try:
            proc = subprocess.run(
                [rg, "--line-number", "--no-heading", "--max-count", "2", "--word-regexp",
                 "--fixed-strings", "--no-messages", term, "."],
                cwd=self.workroom,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        hits = []
        for raw in proc.stdout.splitlines()[:6]:
            parts = raw.split(":", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                hits.append((parts[0].lstrip("./"), int(parts[1])))
        return hits

    def _py_hits(self, term: str) -> list[tuple[str, int]]:
        hits: list[tuple[str, int]] = []
        for path in iter_source_files(self.workroom, self.config.index.ignore):
            rel = str(path.relative_to(self.workroom))
            for lineno, line in enumerate(read_capped(path, 100_000).splitlines(), 1):
                if term in line:
                    hits.append((rel, lineno))
                    break
            if len(hits) >= 6:
                break
        return hits

    def _focus_chunks(self, focus_files: list[str]) -> list[ContextChunk]:
        chunks = []
        for rel in focus_files[:4]:
            excerpt = self._excerpt(rel, 1, 60)
            if excerpt:
                chunks.append(ContextChunk(path=rel, text=excerpt, label="focus file (head)", start=1, end=60))
        return chunks

    def _excerpt(self, rel: str, start: int, end: int) -> str:
        path = self.workroom / rel
        if not path.is_file():
            return ""
        lines = read_capped(path).splitlines()
        window = lines[max(0, start - 1) : min(len(lines), end)]
        return "\n".join(window)

    # -- fusion ------------------------------------------------------------
    def gather(self, query: str, focus_files: list[str] | None = None, budget_tokens: int = 3000) -> str:
        terms = extract_terms(query)
        ordered: list[ContextChunk] = []
        ordered += self._focus_chunks(focus_files or [])
        ordered += self._symbol_chunks(terms)
        ordered += self._vector_chunks(query)
        ordered += self._grep_chunks(terms)

        seen_spans: list[tuple[str, int, int]] = []

        def overlaps(path: str, start: int, end: int) -> bool:
            for p, s, e in seen_spans:
                if p == path and not (end < s or start > e):
                    return True
            return False

        parts: list[str] = []
        used = 0
        for chunk in ordered:
            if overlaps(chunk.path, chunk.start, chunk.end):
                continue
            rendered = f"--- {chunk.path} ({chunk.label}) ---\n{chunk.text}"
            tokens = self.count(rendered)
            if used + tokens > budget_tokens:
                continue
            seen_spans.append((chunk.path, chunk.start, chunk.end))
            parts.append(rendered)
            used += tokens
        return "\n\n".join(parts)
