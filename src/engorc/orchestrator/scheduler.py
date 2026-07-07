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
from ..sessions import foreign_sessions
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
        # An interactive conversation in another terminal owns the GPU for
        # now: stepping would swap its model out between turns.
        sessions = foreign_sessions(self.config.home)
        if sessions:
            active = sessions[0]
            log.debug(f"yielding the GPU to interactive {active.get('kind', 'session')} "
                      f"({active.get('detail', '')})")
            return None
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
        self.services.observe_gpu()
        lock = FileLock(self.config.gpu_lock_path, timeout=self.config.scheduler.gpu_lock_timeout)
        lock.acquire(label=project_slug)
        try:
            note = run_step(self.services, project)
        except Exception as exc:
            import traceback

            note = f"step errored: {exc}"
            # the crash SITE travels with the error — a bug report must be
            # able to say where, not just what
            frames = [line.strip() for line in traceback.format_exc().splitlines()
                      if line.strip().startswith("File ")]
            where = f" (at {frames[-1][5:]})" if frames else ""
            project.journal.append(
                Kind.ERROR,
                error=f"{type(exc).__name__}: {str(exc)[:600]}{where[:250]}",
            )
            log.error(f"[{project_slug}] {note}")
        finally:
            lock.release()
            self.services.observe_gpu()
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
        interactive: bool = False,
    ) -> int:
        """Drive projects until nothing is runnable (or budget/watch says stop).
        Returns the number of steps executed."""
        steps = 0
        last_result: tuple[str, str] | None = None
        repeats = 0
        try:
            while True:
                result = self.step(slug)
                if result is not None:
                    stepped_slug, note = result
                    log.step(stepped_slug, note)
                    steps += 1
                    # Safety net: a step that changes nothing produces the same
                    # note forever — park the project instead of spinning.
                    if result == last_result:
                        repeats += 1
                        if repeats >= 3:
                            project = self.services.registry.get(stepped_slug)
                            project.journal.append(
                                Kind.ERROR,
                                error=f"scheduler loop guard: step repeated identically x{repeats + 1}: {note}",
                            )
                            if not project.gates.open_gates():
                                project.gates.open(
                                    question=("I was looping without making progress "
                                              f"(step kept saying: {note}). This is a bug worth an "
                                              "`orc bugreport --push`; answer anything to retry once."),
                                    from_role="supervisor",
                                )
                            project.set_state("blocked_on_user", reason="loop guard tripped")
                            log.error(f"[{stepped_slug}] loop guard tripped — project parked")
                            last_result, repeats = None, 0
                            continue
                    else:
                        last_result, repeats = result, 0
                    if max_steps is not None and steps >= max_steps:
                        log.info(f"stopped after {steps} step(s) (--max-steps)")
                        break
                    if slug is not None and not self.services.registry.get(slug).is_runnable():
                        if not interactive:
                            break
                    continue
                # Nothing runnable. In interactive mode, questions surface as
                # inline prompts right here — answer and the loop resumes.
                if interactive and log.console.is_terminal:
                    from ..interactive import prompt_gates

                    if prompt_gates(self.services) > 0:
                        continue
                if not watch:
                    break
                time.sleep(self.config.scheduler.poll_seconds)
        except KeyboardInterrupt:
            log.warn("interrupted — all state is on disk; `orc run` resumes where this left off")
        if steps == 0 and slug is None:
            self._explain_idle()
        return steps

    def _explain_idle(self) -> None:
        sessions = foreign_sessions(self.config.home)
        if sessions:
            active = sessions[0]
            log.info(f"yielding to interactive {active.get('kind', 'session')} "
                     f"({active.get('detail', '')}) — runs resume when it finishes")
            return
        gates = 0
        blocked: list[str] = []
        parked: list[tuple[str, str]] = []
        wrapped: list[str] = []
        for project in self.services.registry.all_projects():
            try:
                meta = project.meta
            except FileNotFoundError:
                continue
            open_gates = project.gates.open_gates()
            gates += len(open_gates)
            if meta.state == "blocked_on_user":
                if open_gates:
                    blocked.append(meta.slug)
                else:
                    parked.append((meta.slug, meta.state_reason or "no open questions"))
            elif meta.state == "paused":
                parked.append((meta.slug, "paused"))
            elif meta.state == "done":
                wrapped.append(meta.slug)
        if gates:
            log.info(
                f"nothing runnable: {gates} open question(s) across {len(blocked)} project(s) — "
                "`orc inbox` to answer"
            )
        for slug, reason in parked:
            log.info(f"[{slug}] parked ({reason}) — `orc resume {slug}` to requeue it")
        if gates or parked:
            return
        if wrapped:
            log.info(
                f"nothing runnable: all {len(wrapped)} project(s) are wrapped "
                f"({', '.join(wrapped)}) — `orc request <project> \"<change>\"` reopens one, "
                "`orc new \"<goal>\"` starts fresh"
            )
        else:
            log.info("nothing runnable — `orc new \"<goal>\"` to start a project")
