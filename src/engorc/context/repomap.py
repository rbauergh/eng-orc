"""Token-budgeted structural map of the workroom (aider-style, simplified).

Gives every agent a cheap, always-available skeleton of the codebase: the
most load-bearing files with their top symbols and line numbers. Symbol
extraction prefers universal-ctags (fast, 100+ languages, one subprocess);
falls back to python's ast for .py plus a regex pass for everything else, so
the map never disappears just because a tool is missing.

Ranking is deliberately simple instead of full PageRank: definition-weight
× git-recency × focus-proximity × reference-count correlates well with
"what a developer would want on one screen" at a fraction of the cost.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..fsio import atomic_write_json, read_json
from ..util import approx_tokens
from .files import iter_source_files, language_for, read_capped

KIND_WEIGHTS = {
    "class": 4.0, "struct": 4.0, "interface": 4.0, "trait": 4.0, "protocol": 4.0,
    "enum": 3.0, "type": 3.0, "typedef": 3.0, "typealias": 3.0,
    "function": 2.5, "method": 2.0, "func": 2.5, "def": 2.5, "constructor": 2.0,
    "macro": 2.0, "module": 2.0, "namespace": 2.0,
    "constant": 1.2, "member": 0.8, "field": 0.8, "variable": 0.6, "var": 0.6,
}

_FALLBACK_PATTERNS = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_]\w*)"), "class"),
    (re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"), "function"),
    (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_]\w*)"), "function"),
    (re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z_]\w*)"), "struct"),
    (re.compile(r"^\s*(?:pub\s+)?(?:abstract\s+)?interface\s+([A-Za-z_]\w*)"), "interface"),
    (re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?[A-Za-z_][\w<>\[\]]*\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{"), "method"),
]


@dataclass
class Symbol:
    name: str
    kind: str
    line: int

    def weight(self) -> float:
        return KIND_WEIGHTS.get(self.kind, 0.5)


@dataclass
class FileEntry:
    path: str
    symbols: list[Symbol] = field(default_factory=list)
    score: float = 0.0


class RepoMap:
    def __init__(self, workroom: Path, cache_dir: Path, ignore: list[str], max_kb: int = 384):
        self.workroom = workroom
        self.cache_path = cache_dir / "repomap-cache.json"
        self.ignore = ignore
        self.max_kb = max_kb

    # -- public -----------------------------------------------------------
    def render(
        self,
        focus_files: list[str] | None = None,
        budget_tokens: int = 1600,
        counter: Callable[[str], int] | None = None,
    ) -> str:
        count = counter or approx_tokens
        entries = self._entries()
        if not entries:
            return "(workroom is empty)"
        self._apply_recency_boost(entries)
        self._apply_reference_boost(entries)
        focus = {self._normalize(f) for f in (focus_files or [])}
        for entry in entries:
            if entry.path in focus:
                entry.score *= 4.0
            elif focus and any(entry.path.rsplit("/", 1)[0] == f.rsplit("/", 1)[0] for f in focus):
                entry.score *= 1.5

        selected: list[FileEntry] = []
        used = 0
        for entry in sorted(entries, key=lambda e: -e.score):
            block = self._render_file(entry, max_symbols=8 if entry.path in focus else 5)
            tokens = count(block)
            if used + tokens > budget_tokens and selected:
                continue
            selected.append(entry)
            used += tokens
            if used >= budget_tokens:
                break
        selected.sort(key=lambda e: e.path)
        lines = [self._render_file(e, max_symbols=8 if e.path in focus else 5) for e in selected]
        remaining = len(entries) - len(selected)
        if remaining > 0:
            lines.append(f"…and {remaining} more files (map truncated to budget)")
        return "\n".join(lines)

    def file_symbols(self, rel_path: str) -> list[Symbol]:
        for entry in self._entries():
            if entry.path == rel_path:
                return entry.symbols
        return []

    def find_symbol(self, name: str) -> list[tuple[str, Symbol]]:
        hits: list[tuple[str, Symbol]] = []
        for entry in self._entries():
            for symbol in entry.symbols:
                if symbol.name == name:
                    hits.append((entry.path, symbol))
        return hits

    # -- extraction ----------------------------------------------------------
    def _entries(self) -> list[FileEntry]:
        from .files import tree_digest

        digest = tree_digest(self.workroom, self.ignore, self.max_kb)
        cached = read_json(self.cache_path, default=None)
        if cached and cached.get("digest") == digest:
            return [
                FileEntry(
                    path=raw["path"],
                    symbols=[Symbol(**s) for s in raw["symbols"]],
                    score=raw["score"],
                )
                for raw in cached["entries"]
            ]
        files = list(iter_source_files(self.workroom, self.ignore, self.max_kb))
        tags = self._ctags(files) if shutil.which("ctags") else None
        if tags is None:
            tags = self._fallback_tags(files)
        entries = []
        for rel, symbols in sorted(tags.items()):
            symbols.sort(key=lambda s: (-s.weight(), s.line))
            score = sum(s.weight() for s in symbols[:20])
            entries.append(FileEntry(path=rel, symbols=symbols[:24], score=score))
        atomic_write_json(
            self.cache_path,
            {
                "digest": digest,
                "entries": [
                    {"path": e.path, "score": e.score, "symbols": [vars(s) for s in e.symbols]}
                    for e in entries
                ],
            },
        )
        return entries

    def _ctags(self, files: list[Path]) -> dict[str, list[Symbol]] | None:
        if not files:
            return {}
        try:
            proc = subprocess.run(
                ["ctags", "--output-format=json", "--fields=+n", "-f", "-", "-L", "-"],
                input="\n".join(str(f) for f in files),
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self.workroom,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 and not proc.stdout:
            return None
        tags: dict[str, list[Symbol]] = {}
        for line in proc.stdout.splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if raw.get("_type") != "tag":
                continue
            rel = self._normalize(raw.get("path", ""))
            tags.setdefault(rel, []).append(
                Symbol(name=raw.get("name", "?"), kind=raw.get("kind", "?"), line=raw.get("line", 0))
            )
        return tags

    def _fallback_tags(self, files: list[Path]) -> dict[str, list[Symbol]]:
        tags: dict[str, list[Symbol]] = {}
        for path in files:
            rel = self._normalize(str(path))
            text = read_capped(path)
            if not text:
                continue
            if language_for(path) == "python":
                tags[rel] = self._python_tags(text)
            else:
                symbols = []
                for lineno, line in enumerate(text.splitlines(), 1):
                    for pattern, kind in _FALLBACK_PATTERNS:
                        match = pattern.match(line)
                        if match:
                            symbols.append(Symbol(name=match.group(1), kind=kind, line=lineno))
                            break
                if symbols:
                    tags[rel] = symbols
        return tags

    @staticmethod
    def _python_tags(text: str) -> list[Symbol]:
        symbols: list[Symbol] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return symbols
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                symbols.append(Symbol(name=node.name, kind="class", line=node.lineno))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(Symbol(name=node.name, kind="function", line=node.lineno))
        return symbols

    # -- ranking boosts ------------------------------------------------------
    def _apply_recency_boost(self, entries: list[FileEntry]) -> None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.workroom), "log", "--pretty=format:", "--name-only", "-n", "100"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        if proc.returncode != 0:
            return
        touches: dict[str, int] = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line:
                touches[line] = touches.get(line, 0) + 1
        for entry in entries:
            count = touches.get(entry.path, 0)
            if count:
                entry.score *= 1.0 + min(count, 10) * 0.15

    def _apply_reference_boost(self, entries: list[FileEntry]) -> None:
        """Files whose top symbols are referenced widely matter more."""
        rg = shutil.which("rg")
        if rg is None:
            return
        top_symbols: list[tuple[FileEntry, Symbol]] = []
        for entry in entries:
            for symbol in entry.symbols[:2]:
                if len(symbol.name) >= 4 and not symbol.name.startswith("_"):
                    top_symbols.append((entry, symbol))
        top_symbols.sort(key=lambda pair: -pair[1].weight())
        for entry, symbol in top_symbols[:20]:
            try:
                proc = subprocess.run(
                    [rg, "--count-matches", "--word-regexp", "--fixed-strings",
                     "--no-messages", symbol.name, "."],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=self.workroom,
                )
            except (OSError, subprocess.TimeoutExpired):
                return
            refs = 0
            for line in proc.stdout.splitlines():
                _, _, count = line.rpartition(":")
                if count.isdigit():
                    refs += int(count)
            if refs > 1:
                entry.score *= 1.0 + min(refs, 40) * 0.02

    # -- rendering ----------------------------------------------------------
    @staticmethod
    def _render_file(entry: FileEntry, max_symbols: int = 5) -> str:
        lines = [f"{entry.path}:"]
        for symbol in entry.symbols[:max_symbols]:
            lines.append(f"  {symbol.kind} {symbol.name} (L{symbol.line})")
        return "\n".join(lines)

    def _normalize(self, path: str) -> str:
        p = Path(path)
        if p.is_absolute():
            for root in (self.workroom, self.workroom.resolve()):
                try:
                    return str(p.relative_to(root))
                except ValueError:
                    continue
            return str(p)
        return str(p)
