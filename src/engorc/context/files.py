"""Source-tree walking shared by the indexer, repo map, and search tools."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from pathlib import Path

# tree-sitter-language-pack grammar names keyed by extension; also used to
# label snippets. Extensions absent here index via the plain-text splitter.
LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".c": "c",
    ".h": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cu": "cpp",
    ".cuh": "cpp",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "c_sharp",
    ".swift": "swift",
    ".sh": "bash",
    ".bash": "bash",
    ".lua": "lua",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".scala": "scala",
    ".zig": "zig",
}

TEXT_EXTS = {
    ".md", ".rst", ".txt", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg",
    ".conf", ".env.example", ".dockerfile", ".gitignore", ".editorconfig",
}

_SPECIAL_NAMES = {"Dockerfile", "Makefile", "CMakeLists.txt", "Justfile"}


def language_for(path: Path) -> str | None:
    return LANG_BY_EXT.get(path.suffix.lower())


def is_indexable(path: Path) -> bool:
    if path.name in _SPECIAL_NAMES:
        return True
    suffix = path.suffix.lower()
    return suffix in LANG_BY_EXT or suffix in TEXT_EXTS


def looks_binary(path: Path, sniff_bytes: int = 2048) -> bool:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(sniff_bytes)
    except OSError:
        return True
    return b"\x00" in chunk


def _ignored(rel: str, name: str, ignore: list[str]) -> bool:
    for pattern in ignore:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
            return True
        # bare directory names in the ignore list match at any depth
        if "/" not in pattern and "*" not in pattern and pattern in rel.split("/"):
            return True
    return False


def iter_source_files(
    root: Path,
    ignore: list[str],
    max_kb: int = 384,
    only_indexable: bool = True,
) -> Iterator[Path]:
    """Depth-first walk yielding files worth reading, pruning ignored dirs early.

    The root is walked as given (not resolved), so yielded paths stay
    relative_to()-compatible with whatever the caller holds."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            rel = str(entry.relative_to(root))
            if _ignored(rel, entry.name, ignore):
                continue
            if entry.is_dir():
                if not entry.is_symlink():
                    stack.append(entry)
                continue
            if not entry.is_file() or entry.is_symlink():
                continue
            if only_indexable and not is_indexable(entry):
                continue
            try:
                if entry.stat().st_size > max_kb * 1024:
                    continue
            except OSError:
                continue
            if looks_binary(entry):
                continue
            yield entry


def read_capped(path: Path, max_chars: int = 200_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:max_chars]


def tree_digest(root: Path, ignore: list[str], max_kb: int = 384) -> str:
    """Cheap change-detection key: hash of every source file's (path, mtime, size)."""
    import hashlib

    digest = hashlib.sha256()
    for path in iter_source_files(root, ignore, max_kb=max_kb):
        stat = path.stat()
        digest.update(f"{path.relative_to(root)}|{stat.st_mtime_ns}|{stat.st_size}\n".encode())
    return digest.hexdigest()
