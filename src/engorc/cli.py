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
from rich.markup import escape
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
def sync(
    profile: str | None = typer.Option(None, help="profile to sync (default: the configured one)"),
) -> None:
    """Bring the live server + orc configs in line with the repo's profile.

    Run this after every `git pull`: copies the profile's llama-swap config
    into place, merges its model/review/run settings into ~/.eng-orc/config.yaml
    (your other tuning survives), and restarts llama-swap.
    """
    import shutil as _shutil
    import subprocess as _subprocess

    from .config import apply_profile_to_config, repo_root

    config = get_config()
    root = repo_root()
    if root is None:
        log.error("cannot locate the eng-orc repo next to this install — re-run scripts/setup_wsl.sh")
        raise typer.Exit(1)
    name = profile or config.models.profile
    profile_dir = root / "server" / "profiles" / name
    if not profile_dir.is_dir():
        available = ", ".join(sorted(p.name for p in (root / "server" / "profiles").iterdir()))
        raise typer.BadParameter(f"unknown profile {name!r} (available: {available})")

    swap_config = Path.home() / ".config" / "llama-swap" / "config.yaml"
    ensure_dir(swap_config.parent)
    _shutil.copyfile(profile_dir / "llama-swap.yaml", swap_config)
    log.success(f"llama-swap config ← server/profiles/{name}")

    if not config.config_path.exists():
        init()
    applied = apply_profile_to_config(profile_dir / "orc-models.yaml", config.config_path)
    log.success(f"~/.eng-orc/config.yaml ← profile sections: {', '.join(applied)}")

    restarted = False
    try:
        check = _subprocess.run(["systemctl", "--user", "cat", "llama-swap.service"],
                                capture_output=True, timeout=15)
        if check.returncode == 0:
            restart = _subprocess.run(["systemctl", "--user", "restart", "llama-swap.service"],
                                      capture_output=True, text=True, timeout=60)
            restarted = restart.returncode == 0
    except Exception:
        pass
    if restarted:
        log.success("llama-swap restarted with the fresh config")
    else:
        log.warn("could not restart llama-swap via systemd — restart it yourself "
                 "(scripts/start_stack.sh) so the new config loads")
    log.info("verify with: orc doctor")


@app.command()
def new(
    goal: str | None = typer.Argument(None, help="the mission in plain language; omit for a conversation"),
    doc: Path | None = typer.Option(None, help="seed the mission from a spec/notes document"),
    interactive: bool = typer.Option(False, "--interactive", "-i",
                                     help="define the project in a back-and-forth that builds a spec"),
    title: str | None = typer.Option(None, help="short display title (default: derived)"),
    slug: str | None = typer.Option(None, help="directory slug (default: derived from title)"),
    repo: Path | None = typer.Option(None, help="attach an EXISTING code repository as the workroom"),
    priority: int = typer.Option(3, min=1, max=5, help="1 = urgent … 5 = someday"),
    tag: list[str] = typer.Option([], help="freeform tags"),
) -> None:
    """Start a project: from a one-liner, a seed document, or an intake conversation."""
    if repo is not None and not repo.is_dir():
        raise typer.BadParameter(f"--repo does not exist: {repo}")
    if doc is not None and not doc.is_file():
        raise typer.BadParameter(f"--doc does not exist: {doc}")
    svc = services()
    create_kwargs = dict(slug=slug, workroom=repo, priority=priority, tags=list(tag))

    seed_parts = [p for p in (doc.read_text(encoding="utf-8") if doc else None, goal) if p]
    seed = "\n\n".join(seed_parts) or None
    if interactive or (goal is None and doc is None):
        if not log.console.is_terminal:
            raise typer.BadParameter("interactive intake needs a terminal — pass a goal or --doc")
        from .intake import create_project_from_intake, run_intake

        result = run_intake(svc, seed=seed)
        if result is None:
            log.info("intake abandoned — nothing created")
            raise typer.Exit(0)
        project = create_project_from_intake(svc, result, **create_kwargs)
    else:
        mission = seed or ""
        project = svc.registry.create(mission, title=title, **create_kwargs)
    log.success(f"created project [bold]{project.root.name}[/bold] at {project.root}")
    log.info("run it with: orc run " + project.root.name)


@app.command()
def request(
    project: str,
    text: str | None = typer.Argument(None, help="the bug fix / feature request; omit for a conversation"),
    interactive: bool = typer.Option(False, "--interactive", "-i",
                                     help="shape the request in a back-and-forth first"),
) -> None:
    """Queue a bug fix or feature request onto an existing project — the natural
    way to keep working on something already built (reactivates wrapped projects)."""
    svc = services()
    proj = svc.registry.get(project)
    if interactive or text is None:
        if not log.console.is_terminal:
            raise typer.BadParameter("interactive request needs a terminal — pass the request text")
        from .intake import run_intake

        design = proj.design_path.read_text(encoding="utf-8") if proj.design_path.exists() else ""
        seed = "\n\n".join(p for p in (
            text,
            f"(This is a change request for the EXISTING project '{proj.meta.title}'. "
            f"Existing design follows — spec only the change.)\n\n{design}",
        ) if p)
        result = run_intake(svc, seed=seed)
        if result is None:
            log.info("request abandoned — nothing queued")
            raise typer.Exit(0)
        text = result.spec_markdown
    proj.append_mission_note(text)
    from .orchestrator.phases import plan_request

    note = plan_request(svc, proj, text)
    log.success(note)
    log.info(f"run it with: orc run {proj.root.name}")


@app.command()
def run(
    project: str | None = typer.Argument(None, help="focus one project (default: all runnable)"),
    max_steps: int | None = typer.Option(None, help="stop after N phase steps"),
    watch: bool = typer.Option(False, "--watch", help="keep polling for unparked work instead of exiting"),
    once: bool = typer.Option(False, "--once", help="run exactly one step"),
    verbose: bool = typer.Option(False, "--verbose", "-v",
                                 help="narrate every agent turn (tool-by-tool)"),
    interactive: bool = typer.Option(False, "--interactive", "-i",
                                     help="prompt for gate answers inline whenever work parks"),
) -> None:
    """Advance projects: one phase unit at a time, GPU-fair, park on questions."""
    from .orchestrator.scheduler import Scheduler

    if verbose:
        log.set_level("debug")
    scheduler = Scheduler(services())
    scheduler.run(slug=project, max_steps=1 if once else max_steps, watch=watch,
                  interactive=interactive)


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
def dashboard(
    interval: float = typer.Option(2.0, help="refresh seconds"),
    once: bool = typer.Option(False, "--once", help="print one snapshot and exit"),
    details: bool = typer.Option(False, "--details", "-d",
                                 help="expand all review findings and handoff summaries in the feed"),
) -> None:
    """Live top-style view: resident model, GPU, projects, current work, activity."""
    from .obs.dashboard import run_dashboard

    run_dashboard(services(), interval=interval, once=once, details=details)


@app.command()
def status(project: str | None = typer.Argument(None)) -> None:
    """Status of all projects, or a deep dive on one."""
    svc = services()
    config = svc.config
    if project is None:
        _console().print(
            f"profile [bold]{escape(config.models.profile)}[/bold] · home {escape(str(config.home))}"
        )
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
                escape(meta.slug), str(meta.priority), meta.phase, escape(shorten(state, 40)), plan_str,
                str(open_gates) if open_gates else "-", human_age(meta.last_active),
                str(meta.counters.get("steps", 0)),
            )
        _console().print(table)
        _console().print(f"[dim]projects live in {escape(str(config.projects_dir))}[/dim]")
        return

    proj = svc.registry.get(project)
    meta = proj.meta
    _console().print(f"[bold]{escape(meta.title)}[/bold]  ({escape(meta.slug)})")
    _console().print(f"phase [cyan]{meta.phase}[/cyan] · state [cyan]{meta.state}[/cyan] "
                     f"{escape('· ' + meta.state_reason) if meta.state_reason else ''}")
    _console().print(f"[dim]root: {escape(str(proj.root))}[/dim]")
    _console().print(f"[dim]workroom (the code): {escape(str(proj.workroom))}[/dim]")
    plan = proj.load_plan()
    if plan.items:
        table = Table(title="plan")
        for column in ("item", "title", "kind", "status", "attempts", "deps"):
            table.add_column(column)
        for item in plan.items:
            table.add_row(
                item.id[-6:], escape(shorten(item.title, 50)), item.kind, item.status,
                str(len(item.attempts)), str(len(item.depends_on)) if item.depends_on else "-",
            )
        _console().print(table)
    for gate in proj.gates.open_gates():
        _console().print(f"[yellow]open question[/yellow] [{escape(gate.id)}] {escape(gate.question)}")
    from .context.summarizer import recent_activity

    _console().print("\n[bold]recent activity[/bold]")
    _console().print(recent_activity(proj.journal, 15), markup=False)


def _all_gates(svc) -> list[tuple[Project, Gate]]:
    pairs: list[tuple[Project, Gate]] = []
    for proj in svc.registry.all_projects():
        for gate in proj.gates.open_gates():
            pairs.append((proj, gate))
    return sorted(pairs, key=lambda pair: pair[1].ts)


@app.command()
def inbox(
    list_only: bool = typer.Option(False, "--list", help="print the plain list instead of prompting"),
) -> None:
    """Answer agents' questions interactively (plain listing with --list)."""
    svc = services()
    if not list_only and log.console.is_terminal:
        from .interactive import prompt_gates

        answered = prompt_gates(svc)
        if answered:
            log.info("continue with: orc run  (or it resumes on the next --watch pass)")
        else:
            log.success("inbox zero — nothing is waiting on you")
        return
    pairs = _all_gates(svc)
    if not pairs:
        log.success("inbox zero — nothing is waiting on you")
        return
    for proj, gate in pairs:
        origin = f"[{proj.root.name} · {gate.from_role} · {human_age(gate.ts)} ago]"
        _console().print(f"[bold]{escape(gate.id)}[/bold]  {escape(origin)}")
        _console().print(f"  Q: {escape(gate.question)}")
        if gate.context:
            _console().print(f"  [dim]context: {escape(shorten(gate.context, 160))}[/dim]")
        if gate.options:
            _console().print(f"  options: {escape(' | '.join(gate.options))}")
    _console().print("\nanswer with: orc answer \"your answer\"  (oldest gate) or orc answer <gate-id> \"...\"")


@app.command()
def answer(
    gate_id: str | None = typer.Argument(None, help="gate id (or prefix); omit to answer interactively"),
    text: str | None = typer.Argument(None, help="the answer; omit to be prompted"),
) -> None:
    """Answer an agent's question; the project unparks on the next run."""
    svc = services()
    # `orc answer "just do X"` — one argument means the text, aimed at the oldest gate
    if gate_id is not None and text is None:
        pairs = _all_gates(svc)
        if len(pairs) == 1 or (pairs and not any(g.id.startswith(gate_id) for _, g in pairs)):
            text = gate_id
            gate_id = None
    if gate_id is None and text is None:
        from .interactive import prompt_gates

        if prompt_gates(svc) == 0:
            log.success("inbox zero — nothing is waiting on you")
        return
    if gate_id is None:
        pairs = _all_gates(svc)
        if not pairs:
            raise typer.BadParameter("no open gates to answer")
        proj, gate = pairs[0]
        answered = proj.gates.answer(gate.id, text)
        proj.journal.append(Kind.GATE_ANSWERED, actor="user", answer=text, question=answered.question)
        log.success(f"answered the oldest gate on {proj.root.name} — `orc run` to continue")
        return
    for proj in svc.registry.all_projects():
        try:
            gate = proj.gates.get(gate_id)
        except KeyError:
            continue
        answered = proj.gates.answer(gate.id, text)
        proj.journal.append(Kind.GATE_ANSWERED, actor="user", answer=text, question=answered.question)
        log.success(f"answered {answered.id} on {proj.root.name} — `orc run` to continue")
        return
    raise typer.BadParameter(f"no open gate matches {gate_id!r} (see `orc inbox --list`)")


@app.command()
def report(project: str) -> None:
    """Print the project's final report (or its current state if unfinished)."""
    proj = services().registry.get(project)
    text = proj.artifacts.read("report.md")
    if text:
        _console().print(text, markup=False)
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
        origin = f"[{item.kind} · {item.project or 'global'} · {hit.backend}]"
        _console().print(f"[bold]{escape(item.title)}[/bold]  {escape(origin)}")
        _console().print(f"  {escape(shorten(item.body, 300))}")


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
        table.add_row(escape(name), escape(role_model.model), str(role_model.context_window),
                      str(role_model.max_output_tokens), "yes" if role_model.thinking else "no")
    table.add_row("embedder", svc.config.models.embedder.model, "—", "—", "—")
    _console().print(table)
    if svc.client.health():
        served = ", ".join(svc.client.model_ids()) or "(none)"
        log.success(f"server up at {svc.config.server.base_url} — serving: {served}")
        svc.observe_gpu()
        from .util import human_duration

        for entry in svc.timeline.current():
            log.info(f"resident: {entry['model']} ({entry['state']} for "
                     f"{human_duration(entry['for_seconds'])})")
        events = svc.timeline.recent(6)
        if events:
            log.info("gpu timeline:")
            for event in events:
                log.info(f"  {svc.timeline.describe(event)}")
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
    from .bugreport import UNLISTED_NOTE, gather_rows

    rows, unlisted = gather_rows(services(), get_config())
    table = Table(title="orc doctor")
    for column in ("check", "status", "detail"):
        table.add_column(column)
    marks = {True: "[green]ok[/green]", False: "[red]FAIL[/red]", None: "[yellow]warn[/yellow]"}
    for name, ok, detail in rows:
        table.add_row(escape(name), marks[ok], escape(shorten(detail, 90)))
    _console().print(table)
    if unlisted:
        log.warn("configured names missing from the server's model listing:")
        for entry in unlisted:
            log.info(f"  · {entry}")
        log.info(UNLISTED_NOTE)


@app.command()
def bugreport(
    output: Path = typer.Option(Path("orc-report.md"), help="where to write the report"),
    push: bool = typer.Option(False, "--push",
                              help="git add+commit+push the report from its directory"),
) -> None:
    """Write a sanitized diagnostics report to share (secrets redacted).

    The intended loop for remote debugging: run `orc bugreport --push` in the
    eng-orc repo checkout; whoever is helping pulls and reads orc-report.md.
    """
    from .bugreport import build_report, commit_and_push

    text = build_report(services(), get_config())
    output = output.resolve()
    output.write_text(text, encoding="utf-8")
    log.success(f"wrote {output} ({len(text.splitlines())} lines, secrets redacted)")
    if push:
        ok, message = commit_and_push(output)
        (log.success if ok else log.error)(message)
        if not ok:
            raise typer.Exit(1)
    else:
        log.info("share it with: orc bugreport --push   (commits and pushes just this file)")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
