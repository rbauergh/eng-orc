"""orc — the eng-orc command line.

Everyday flow:
    orc new "build me X"          start a mission (add --repo for existing code)
    orc run --watch               work all runnable projects, park on questions
    orc status                    where everything stands
    orc inbox / orc answer        see and answer agent questions
    orc selftest                  prove the machinery end-to-end, no GPU needed
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from . import __version__
from .config import CONFIG_TEMPLATE, get_config, load_config
from .events import Kind
from .fsio import atomic_write_text, ensure_dir
from .gates import Gate
from .obs.console import log
from .project import Project
from .util import human_age, shorten

app = typer.Typer(help=__doc__, no_args_is_help=True, add_completion=False)
memory_app = typer.Typer(help="Long-term memory operations")
app.add_typer(memory_app, name="memory")

_services = None


def services():
    global _services
    if _services is None:
        from .llm.client import LLMClient
        from .orchestrator.services import Services

        config = get_config()
        _services = Services.build(config, client=LLMClient(config.server))
    return _services


def _console():
    return log.console


# ---------------------------------------------------------------------- lifecycle


@app.command()
def init() -> None:
    """Create the eng-orc home directory and a commented config file."""
    config = load_config()
    ensure_dir(config.home)
    ensure_dir(config.projects_dir)
    if config.config_path.exists():
        log.info(f"config already exists: {config.config_path}")
    else:
        atomic_write_text(config.config_path, CONFIG_TEMPLATE)
        log.success(f"wrote {config.config_path}")
    log.info("next: run `orc selftest`, then `orc doctor` once your servers are up")


@app.command()
def version() -> None:
    """Print the eng-orc version."""
    log.info(f"eng-orc {__version__}")


@app.command()
def new(
    goal: str = typer.Argument(..., help="the mission, in plain language"),
    title: str | None = typer.Option(None, help="short display title (default: first line of goal)"),
    slug: str | None = typer.Option(None, help="directory slug (default: derived from title)"),
    repo: Path | None = typer.Option(None, help="attach an EXISTING code repository as the workroom"),
    priority: int = typer.Option(3, min=1, max=5, help="1 = urgent … 5 = someday"),
    tag: list[str] = typer.Option([], help="freeform tags"),
) -> None:
    """Start a new project from a goal."""
    if repo is not None and not repo.is_dir():
        raise typer.BadParameter(f"--repo does not exist: {repo}")
    project = services().registry.create(
        goal, title=title, slug=slug, workroom=repo, priority=priority, tags=list(tag)
    )
    log.success(f"created project [bold]{project.root.name}[/bold] at {project.root}")
    log.info("run it with: orc run " + project.root.name)


@app.command()
def run(
    project: str | None = typer.Argument(None, help="focus one project (default: all runnable)"),
    max_steps: int | None = typer.Option(None, help="stop after N phase steps"),
    watch: bool = typer.Option(False, "--watch", help="keep polling for unparked work instead of exiting"),
    once: bool = typer.Option(False, "--once", help="run exactly one step"),
) -> None:
    """Advance projects: one phase unit at a time, GPU-fair, park on questions."""
    from .orchestrator.scheduler import Scheduler

    scheduler = Scheduler(services())
    scheduler.run(slug=project, max_steps=1 if once else max_steps, watch=watch)


@app.command()
def pause(project: str, reason: str = typer.Option("", help="why (shown in status)")) -> None:
    """Pause a project; the scheduler will skip it until resumed."""
    proj = services().registry.get(project)
    proj.set_state("paused", reason=reason or "paused by user")
    log.success(f"paused {proj.root.name}")


@app.command()
def resume(project: str) -> None:
    """Reactivate a paused (or done) project for fresh work."""
    proj = services().registry.get(project)
    meta = proj.meta
    if meta.phase == "done":
        meta.phase = "build" if proj.plan_path.exists() else "charter"
        proj.save_meta(meta)
    proj.set_state("active", reason="resumed by user")
    proj.journal.append(Kind.RESUME, actor="user")
    log.success(f"resumed {proj.root.name} (phase {proj.meta.phase}) — `orc run {proj.root.name}`")


@app.command()
def abandon(project: str, reason: str = typer.Option("", help="for the record")) -> None:
    """Walk away from a project (kept on disk, never scheduled)."""
    proj = services().registry.get(project)
    proj.set_state("abandoned", reason=reason or "abandoned by user")
    log.success(f"abandoned {proj.root.name}")


@app.command()
def ask(project: str, note: str) -> None:
    """Add guidance/an amendment to a project's mission (picked up next step)."""
    proj = services().registry.get(project)
    proj.append_mission_note(note)
    log.success("noted — future briefs will include it")


# ---------------------------------------------------------------------- visibility


@app.command()
def status(project: str | None = typer.Argument(None)) -> None:
    """Status of all projects, or a deep dive on one."""
    svc = services()
    if project is None:
        table = Table(title="projects", show_lines=False)
        for column in ("project", "pri", "phase", "state", "plan", "gates", "active", "steps"):
            table.add_column(column)
        for proj in svc.registry.all_projects():
            meta = proj.meta
            progress = proj.load_plan().progress()
            plan_str = f"{progress.get('done', 0)}/{progress.get('total', 0)}" if progress.get("total") else "—"
            open_gates = len(proj.gates.open_gates())
            state = meta.state + (f" ({meta.state_reason})" if meta.state_reason and meta.state != "active" else "")
            table.add_row(
                meta.slug, str(meta.priority), meta.phase, shorten(state, 40), plan_str,
                str(open_gates) if open_gates else "-", human_age(meta.last_active),
                str(meta.counters.get("steps", 0)),
            )
        _console().print(table)
        return

    proj = svc.registry.get(project)
    meta = proj.meta
    _console().print(f"[bold]{meta.title}[/bold]  ({meta.slug})")
    _console().print(f"phase [cyan]{meta.phase}[/cyan] · state [cyan]{meta.state}[/cyan] "
                     f"{('· ' + meta.state_reason) if meta.state_reason else ''}")
    plan = proj.load_plan()
    if plan.items:
        table = Table(title="plan")
        for column in ("item", "title", "kind", "status", "attempts", "deps"):
            table.add_column(column)
        for item in plan.items:
            table.add_row(
                item.id[-6:], shorten(item.title, 50), item.kind, item.status,
                str(len(item.attempts)), str(len(item.depends_on)) if item.depends_on else "-",
            )
        _console().print(table)
    for gate in proj.gates.open_gates():
        _console().print(f"[yellow]open question[/yellow] [{gate.id}] {gate.question}")
    from .context.summarizer import recent_activity

    _console().print("\n[bold]recent activity[/bold]")
    _console().print(recent_activity(proj.journal, 15))


def _all_gates(svc) -> list[tuple[Project, Gate]]:
    pairs: list[tuple[Project, Gate]] = []
    for proj in svc.registry.all_projects():
        for gate in proj.gates.open_gates():
            pairs.append((proj, gate))
    return sorted(pairs, key=lambda pair: pair[1].ts)


@app.command()
def inbox() -> None:
    """All open questions from agents, across every project."""
    pairs = _all_gates(services())
    if not pairs:
        log.success("inbox zero — nothing is waiting on you")
        return
    for proj, gate in pairs:
        _console().print(
            f"[bold]{gate.id}[/bold]  [{proj.root.name} · {gate.from_role} · {human_age(gate.ts)} ago]"
        )
        _console().print(f"  Q: {gate.question}")
        if gate.context:
            _console().print(f"  [dim]context: {shorten(gate.context, 160)}[/dim]")
        if gate.options:
            _console().print(f"  options: {' | '.join(gate.options)}")
    _console().print("\nanswer with: orc answer <gate-id> \"your answer\"")


@app.command()
def answer(gate_id: str, text: str) -> None:
    """Answer an agent's question; the project unparks on the next run."""
    svc = services()
    for proj in svc.registry.all_projects():
        try:
            gate = proj.gates.get(gate_id)
        except KeyError:
            continue
        answered = proj.gates.answer(gate.id, text)
        proj.journal.append(Kind.GATE_ANSWERED, actor="user", answer=text, question=answered.question)
        log.success(f"answered {answered.id} on {proj.root.name} — `orc run` to continue")
        return
    raise typer.BadParameter(f"no open gate matches {gate_id!r} (see `orc inbox`)")


@app.command()
def report(project: str) -> None:
    """Print the project's final report (or its current state if unfinished)."""
    proj = services().registry.get(project)
    text = proj.artifacts.read("report.md")
    if text:
        _console().print(text)
    else:
        log.info("no final report yet — showing status instead")
        status(project)


# ---------------------------------------------------------------------- context/index


@app.command()
def index(
    project: str,
    rebuild: bool = typer.Option(False, help="drop and re-embed everything"),
) -> None:
    """Refresh (or rebuild) the semantic code index for a project."""
    svc = services()
    proj = svc.registry.get(project)
    ctx = svc.context_for(proj)
    if ctx.index is None:
        from .context.indexer import CodebaseIndex

        _, reason = CodebaseIndex(proj, svc.config, svc.client).status()
        log.error(f"index unavailable: {reason}")
        raise typer.Exit(1)
    stats = ctx.index.rebuild(proj.journal) if rebuild else ctx.index.refresh(proj.journal)
    log.success(f"index synced: {stats}")


# ---------------------------------------------------------------------- memory


@memory_app.command("search")
def memory_search(query: str, k: int = typer.Option(5), kind: str | None = typer.Option(None)) -> None:
    """Search long-term memory."""
    hits = services().memory.search(query, k=k, kinds=[kind] if kind else None)
    if not hits:
        log.info("no matches")
        return
    for hit in hits:
        item = hit.item
        _console().print(f"[bold]{item.title}[/bold]  [{item.kind} · {item.project or 'global'} · {hit.backend}]")
        _console().print(f"  {shorten(item.body, 300)}")


@memory_app.command("add")
def memory_add(
    body: str,
    title: str | None = typer.Option(None),
    kind: str = typer.Option("note"),
    project: str = typer.Option(""),
) -> None:
    """Record a memory item by hand (convention, lesson, note)."""
    from .memory.schema import MemoryItem

    item = MemoryItem(kind=kind, project=project, title=title or shorten(body, 80), body=body)  # type: ignore[arg-type]
    services().memory.save(item)
    log.success(f"saved {item.kind} {item.id}")


@memory_app.command("sync")
def memory_sync() -> None:
    """Push locally-queued memory items to Letta."""
    memory = services().memory
    if hasattr(memory, "sync"):
        count = memory.sync()
        log.success(f"synced {count} item(s) to letta")
    else:
        log.info("memory backend has no sync surface")


# ---------------------------------------------------------------------- models/servers


@app.command()
def models(unload: bool = typer.Option(False, help="unload whatever is resident on the GPU")) -> None:
    """Show configured model roles and what the server is actually serving."""
    svc = services()
    if unload:
        log.success("unloaded" if svc.swap.unload_all() else "nothing to unload (or no control endpoint)")
        return
    table = Table(title="model roles")
    for column in ("role", "served model", "ctx", "max out", "thinking"):
        table.add_column(column)
    from .llm.catalog import chat_model_roles

    for name, role_model in chat_model_roles(svc.config).items():
        table.add_row(name, role_model.model, str(role_model.context_window),
                      str(role_model.max_output_tokens), "yes" if role_model.thinking else "no")
    table.add_row("embedder", svc.config.models.embedder.model, "—", "—", "—")
    _console().print(table)
    if svc.client.health():
        served = ", ".join(svc.client.model_ids()) or "(none)"
        log.success(f"server up at {svc.config.server.base_url} — serving: {served}")
        loaded = svc.swap.loaded_model()
        log.info(f"resident on GPU now: {loaded or '(nothing loaded)'}")
    else:
        log.warn(f"server not reachable at {svc.config.server.base_url}")


@app.command()
def chat(
    role: str = typer.Argument("utility", help="coder | planner | utility"),
    message: str = typer.Argument(...),
) -> None:
    """One direct exchange with a model role (server smoke test)."""
    svc = services()
    role_model = svc.config.models.for_role(role)
    result = svc.client.chat(role_model, [{"role": "user", "content": message}],
                             stream_cb=lambda chunk: print(chunk, end="", flush=True))
    print()
    log.info(f"[{result.model}] {result.usage.prompt_tokens}+{result.usage.completion_tokens} tokens "
             f"in {result.elapsed:.1f}s")


# ---------------------------------------------------------------------- verification


@app.command()
def selftest(
    keep: bool = typer.Option(False, help="keep the temp home for inspection"),
    home: Path | None = typer.Option(None, help="run in this directory instead of a temp one"),
) -> None:
    """Full-pipeline verification with a fake model: no GPU, no servers."""
    from .selftest import print_report, run_selftest

    report = run_selftest(home=home, keep=keep)
    print_report(report)
    raise typer.Exit(0 if report.ok else 1)


@app.command()
def doctor() -> None:
    """Check every dependency of a working installation."""
    import shutil as _shutil

    config = get_config()
    table = Table(title="orc doctor")
    for column in ("check", "status", "detail"):
        table.add_column(column)

    def row(name: str, ok: bool | None, detail: str = "") -> None:
        mark = {True: "[green]ok[/green]", False: "[red]FAIL[/red]", None: "[yellow]warn[/yellow]"}[ok]
        table.add_row(name, mark, shorten(detail, 70))

    row("home writable", config.home.exists() or config.home.parent.exists(), str(config.home))
    row("config file", config.config_path.exists(), str(config.config_path)
        if config.config_path.exists() else "run `orc init`")

    for binary, required in (("git", True), ("rg", False), ("ctags", False), ("bash", True)):
        present = _shutil.which(binary) is not None
        row(f"binary: {binary}", present if required else (present or None),
            "" if present else ("required" if required else "optional — better context if installed"))

    for module, why in (("langgraph", "orchestration"), ("llama_index.core", "code index"),
                        ("chromadb", "vector store"), ("letta_client", "letta memory")):
        try:
            __import__(module)
            row(f"python: {module}", True, why)
        except ImportError as exc:
            row(f"python: {module}", None, f"{why} — {exc}")

    svc = services()
    server_up = svc.client.health()
    row("llm server", server_up, config.server.base_url)
    if server_up:
        from .llm.catalog import chat_model_roles

        served = set(svc.client.model_ids())
        alias_hint = (
            "not in /v1/models — if this is a llama-swap alias, set "
            "includeAliasesInList: true in the llama-swap config and restart it"
        )
        for name, role_model in chat_model_roles(config).items():
            ok_role = role_model.model in served
            row(f"model role: {name}", ok_role, role_model.model if ok_role else
                f"{role_model.model}: {alias_hint}")
        for seat in config.review.panel:
            try:
                seat_model = config.models.for_role(seat.model_role)
                row(f"review seat: {seat.lens}", seat_model.model in served,
                    f"{seat.model_role} → {seat_model.model}")
            except KeyError:
                row(f"review seat: {seat.lens}", False, f"unknown model role {seat.model_role!r}")
        for fallback in config.run.coder_fallbacks:
            try:
                fb_model = config.models.for_role(fallback)
                row(f"coder fallback: {fallback}", fb_model.model in served, fb_model.model)
            except KeyError:
                row(f"coder fallback: {fallback}", False, "unknown model role")
        try:
            dim = len(svc.client.embeddings(["ping"], model=config.models.embedder.model)[0])
            row("embeddings", True, f"{config.models.embedder.model} (dim {dim})")
        except Exception as exc:
            row("embeddings", None, str(exc))
        row("swap control", svc.swap.health(), config.server.control_url)

    ok_memory, detail = svc.memory.health()
    row("memory", ok_memory, detail)

    nvidia = _shutil.which("nvidia-smi")
    row("nvidia-smi", (nvidia is not None) or None, "GPU visibility (informational)")

    _console().print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
