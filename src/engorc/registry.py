"""Multi-project registry.

The projects directory itself is the registry: every child directory holding
a project.json is a project. No side index to drift out of sync.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .events import Kind
from .fsio import atomic_write_json, ensure_dir
from .project import Project, ProjectMeta
from .util import iso_now, slugify


class Registry:
    def __init__(self, config: Config):
        self.config = config
        self.projects_dir = ensure_dir(config.projects_dir)

    def slugs(self) -> list[str]:
        return sorted(
            p.parent.name for p in self.projects_dir.glob("*/project.json")
        )

    def exists(self, slug: str) -> bool:
        return (self.projects_dir / slug / "project.json").exists()

    def get(self, slug: str) -> Project:
        slug = self.resolve_slug(slug)
        return Project(self.projects_dir / slug, self.config)

    def resolve_slug(self, slug: str) -> str:
        """Accepts exact slugs or unambiguous prefixes."""
        if self.exists(slug):
            return slug
        matches = [s for s in self.slugs() if s.startswith(slug)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"no such project: {slug}")
        raise KeyError(f"ambiguous project {slug!r}: {matches}")

    def all_projects(self) -> list[Project]:
        return [Project(self.projects_dir / slug, self.config) for slug in self.slugs()]

    def create(
        self,
        goal: str,
        title: str | None = None,
        slug: str | None = None,
        workroom: Path | None = None,
        priority: int = 3,
        tags: list[str] | None = None,
    ) -> Project:
        title = title or goal.strip().splitlines()[0][:80]
        slug = slug or self._unique_slug(slugify(title))
        if self.exists(slug):
            raise FileExistsError(f"project already exists: {slug}")
        root = ensure_dir(self.projects_dir / slug)
        meta = ProjectMeta(
            slug=slug,
            title=title,
            priority=priority,
            tags=tags or [],
            external_workroom=str(workroom.resolve()) if workroom else None,
        )
        atomic_write_json(root / "project.json", meta.model_dump(exclude_none=True))
        project = Project(root, self.config)
        project.write_mission(f"# Mission\n\n{goal.strip()}\n\n_Created {iso_now()}_\n")
        if not meta.external_workroom:
            ensure_dir(root / "workroom")
        project.journal.append(
            Kind.PROJECT_CREATED,
            actor="user",
            title=title,
            priority=priority,
            external_workroom=meta.external_workroom,
        )
        return project

    def _unique_slug(self, base: str) -> str:
        if not self.exists(base):
            return base
        n = 2
        while self.exists(f"{base}-{n}"):
            n += 1
        return f"{base}-{n}"
