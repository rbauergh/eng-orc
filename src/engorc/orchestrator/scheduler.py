"""Multi-project scheduler: one GPU, many missions.

Round-robin with priority: each pass picks the runnable project that has
waited longest (within priority class), runs exactly one phase unit under
the GPU lease, and rotates. Projects parked on user questions cost nothing;
an answered gate makes a project runnable again on the next pass. All
scheduling inputs are on disk, so any number of CLI invocations can come and
go — the lease serializes actual model work.
"""

from __future__ import annotations

import time

from ..events import Kind
from ..fsio import FileLock
from ..obs.console import log
from ..project import Project
from ..util import iso_now
from .graph import run_step
from .services import Services


class Scheduler:
    def __init__(self, services: Services):
        self.services = services
        self.config = services.config

    # -- selection -------------------------------------------------------------
    def runnable(self) -> list[Project]:
        candidates: list[tuple[int, str, Project]] = []
        for project in self.services.registry.all_projects():
            try:
                meta = project.meta
            except FileNotFoundError:
                continue
            if not project.is_runnable():
                continue
            if meta.state == "blocked_on_user":
                project.set_state("active", reason="user answered")
                meta = project.meta
            candidates.append((meta.priority, meta.last_active, project))
        candidates.sort(key=lambda entry: (entry[0], entry[1]))
        return [entry[2] for entry in candidates]

    # -- stepping ---------------------------------------------------------------
    def step(self, slug: str | None = None) -> tuple[str, str] | None:
        """Run one phase unit; returns (slug, note) or None when nothing is runnable."""
        if slug is not None:
            project = self.services.registry.get(slug)
            if not project.is_runnable():
                meta = project.meta
                log.info(f"[{meta.slug}] not runnable: state={meta.state} {meta.state_reason}")
                return None
            if project.meta.state == "blocked_on_user":
                project.set_state("active", reason="user answered")
            queue = [project]
        else:
            queue = self.runnable()
        if not queue:
            return None
        project = queue[0]
        project_slug = project.root.name

        from .supervisor import next_phase

        log.step(project_slug, f"→ {next_phase(project)} …")
        lock = FileLock(self.config.gpu_lock_path, timeout=self.config.scheduler.gpu_lock_timeout)
        lock.acquire(label=project_slug)
        try:
            note = run_step(self.services, project)
        except Exception as exc:
            note = f"step errored: {exc}"
            project.journal.append(Kind.ERROR, error=str(exc)[:800])
            log.error(f"[{project_slug}] {note}")
        finally:
            lock.release()
            meta = project.meta
            meta.last_active = iso_now()
            meta.bump(steps=1)
            project.save_meta(meta)
        return project_slug, note

    # -- loops ----------------------------------------------------------------------
    def run(
        self,
        slug: str | None = None,
        max_steps: int | None = None,
        watch: bool = False,
    ) -> int:
        """Drive projects until nothing is runnable (or budget/watch says stop).
        Returns the number of steps executed."""
        steps = 0
        try:
            while True:
                result = self.step(slug)
                if result is not None:
                    stepped_slug, note = result
                    log.step(stepped_slug, note)
                    steps += 1
                    if max_steps is not None and steps >= max_steps:
                        log.info(f"stopped after {steps} step(s) (--max-steps)")
                        break
                    if slug is not None and not self.services.registry.get(slug).is_runnable():
                        break
                    continue
                # nothing runnable right now
                if not watch:
                    break
                time.sleep(self.config.scheduler.poll_seconds)
        except KeyboardInterrupt:
            log.warn("interrupted — all state is on disk; `orc run` resumes where this left off")
        if steps == 0 and slug is None:
            self._explain_idle()
        return steps

    def _explain_idle(self) -> None:
        gates = 0
        blocked = []
        for project in self.services.registry.all_projects():
            try:
                meta = project.meta
            except FileNotFoundError:
                continue
            open_gates = project.gates.open_gates()
            gates += len(open_gates)
            if meta.state == "blocked_on_user":
                blocked.append(meta.slug)
        if gates:
            log.info(
                f"nothing runnable: {gates} open question(s) across {len(blocked)} project(s) — "
                "`orc inbox` to answer"
            )
        else:
            log.info("nothing runnable — `orc new \"<goal>\"` to start a project")
