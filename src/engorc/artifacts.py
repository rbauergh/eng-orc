"""Versioned project artifacts and handoff documents.

Agents communicate through documents, not chat transcripts: the charter,
design, reviews, attempt transcripts, and handoff briefs are all files under
artifacts/. Re-writing an artifact keeps prior versions (name.md, name.v2.md,
...) so the living-document history is preserved and diffable.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from .fsio import atomic_write_text, ensure_dir
from .util import iso_now

_VERSION_RE = re.compile(r"^(?P<stem>.+?)\.v(?P<n>\d+)$")


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = ensure_dir(root)

    def _resolve_dir(self, subdir: str | None) -> Path:
        return ensure_dir(self.root / subdir) if subdir else self.root

    def write(self, name: str, content: str, subdir: str | None = None) -> Path:
        """Write a new version of an artifact; the previous content rotates to
        the next free .vN slot so document history is preserved and diffable."""
        directory = self._resolve_dir(subdir)
        target = directory / name
        if target.exists():
            base, suffix = target.stem, target.suffix
            existing_versions = []
            for candidate in directory.glob(f"{base}.v*{suffix}"):
                match = _VERSION_RE.match(candidate.stem)
                if match and match.group("stem") == base:
                    existing_versions.append(int(match.group("n")))
            next_version = max(existing_versions, default=0) + 1
            target.replace(directory / f"{base}.v{next_version}{suffix}")
        atomic_write_text(target, content)
        return target

    def read(self, name: str, subdir: str | None = None) -> str | None:
        path = self._resolve_dir(subdir) / name
        return path.read_text(encoding="utf-8") if path.exists() else None

    def exists(self, name: str, subdir: str | None = None) -> bool:
        return (self._resolve_dir(subdir) / name).exists()

    def path(self, name: str, subdir: str | None = None) -> Path:
        return self._resolve_dir(subdir) / name

    def list(self, subdir: str | None = None) -> list[Path]:
        directory = self._resolve_dir(subdir)
        return sorted(p for p in directory.rglob("*") if p.is_file())


class Handoff(BaseModel):
    """The structured baton passed between roles for one work item."""

    from_role: str
    to_role: str = "next"
    item: str | None = None
    ts: str = Field(default_factory=iso_now)
    summary: str = ""
    state_of_work: str = ""  # what exists now, where, and how to run it
    warnings: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    touched_files: list[str] = Field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# Handoff from {self.from_role}",
            f"_{self.ts}_",
            "",
            "## Summary",
            self.summary or "(none)",
            "",
            "## State of work",
            self.state_of_work or "(none)",
        ]
        if self.touched_files:
            lines += ["", "## Touched files"] + [f"- {f}" for f in self.touched_files]
        if self.warnings:
            lines += ["", "## Warnings"] + [f"- {w}" for w in self.warnings]
        if self.next_steps:
            lines += ["", "## Next steps"] + [f"- {s}" for s in self.next_steps]
        return "\n".join(lines) + "\n"
