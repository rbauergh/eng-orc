"""Diagnostics: the environment checks behind `orc doctor` and the shareable
report behind `orc bugreport`.

The report is designed to travel: everything needed to debug a remote
installation (check results, versions, sanitized config, per-project journal
errors, server log tail) in one markdown file with secrets redacted, small
enough to commit and push.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from . import __version__
from .config import Config
from .context.summarizer import render_events
from .events import Kind
from .util import iso_now, shorten

Row = tuple[str, bool | None, str]  # (check name, ok/warn(None)/fail, detail)

_SECRET_MARKERS = ("key", "token", "password", "secret")


# ---------------------------------------------------------------------- checks


def gather_rows(svc, config: Config) -> tuple[list[Row], list[str]]:
    """Every installation check, shared by doctor (table) and bugreport (text)."""
    rows: list[Row] = []
    unlisted: list[str] = []

    def row(name: str, ok: bool | None, detail: str = "") -> None:
        rows.append((name, ok, detail))

    row("home writable", config.home.exists() or config.home.parent.exists(), str(config.home))
    row("config file", config.config_path.exists(),
        str(config.config_path) if config.config_path.exists() else "run `orc init`")

    for binary, required in (("git", True), ("rg", False), ("ctags", False), ("bash", True)):
        present = shutil.which(binary) is not None
        row(f"binary: {binary}", present if required else (present or None),
            "" if present else ("required" if required else "optional — better context if installed"))

    for module, why in (("langgraph", "orchestration"), ("llama_index.core", "code index"),
                        ("chromadb", "vector store"), ("letta_client", "letta memory")):
        try:
            __import__(module)
            row(f"python: {module}", True, why)
        except ImportError as exc:
            row(f"python: {module}", None, f"{why} — {exc}")

    server_up = svc.client.health()
    row("llm server", server_up, config.server.base_url)
    if server_up:
        from .llm.catalog import chat_model_roles

        served = set(svc.client.model_ids()) | svc.swap.known_model_names()

        def model_row(label: str, name: str) -> None:
            # An incomplete listing is a warning, not a failure: llama-swap
            # routes aliases correctly even when it doesn't list them.
            if name in served:
                row(label, True, name)
            else:
                unlisted.append(f"{label} → {name}")
                row(label, None, f"{name} — not in the server's listing (note below)")

        for name, role_model in chat_model_roles(config).items():
            model_row(f"model role: {name}", role_model.model)
        for seat in config.review.panel:
            try:
                model_row(f"review seat: {seat.lens}", config.models.for_role(seat.model_role).model)
            except KeyError:
                row(f"review seat: {seat.lens}", False, f"unknown model role {seat.model_role!r}")
        for fallback in config.run.coder_fallbacks:
            try:
                model_row(f"coder fallback: {fallback}", config.models.for_role(fallback).model)
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
    row("nvidia-smi", (shutil.which("nvidia-smi") is not None) or None,
        "GPU visibility (informational)")

    # A configured model whose GGUF is missing dies with an opaque
    # "upstream command exited prematurely" at load time — catch it here,
    # and when a near-identical file exists, say so (quant-name drift).
    for model_id, path in _llama_swap_model_files():
        if path.exists():
            row(f"model file: {model_id}", True, str(path))
            continue
        similar = _similar_files(path)
        if similar:
            row(f"model file: {model_id}", False,
                f"config wants {path.name} but you have {similar[0]} — run `orc sync`")
        else:
            row(f"model file: {model_id}", False,
                f"MISSING {path} — scripts/download_models.sh fetches it")

    drift_ok, drift_detail = _profile_config_drift(config)
    if drift_ok is not None:
        row("llama-swap config sync", True if drift_ok else None, drift_detail)
    return rows, unlisted


def _similar_files(path: Path) -> list[str]:
    """GGUFs in the same directory that look like the wanted file modulo
    quant-naming drift (UD- prefixes and case)."""
    if not path.parent.is_dir():
        return []

    def normalize(name: str) -> str:
        return re.sub(r"ud[-_]", "", name.lower())

    target = normalize(path.name)
    exact = [f.name for f in path.parent.glob("*.gguf")
             if normalize(f.name) == target and f.name != path.name]
    if exact:
        return exact
    prefix = path.name[:16].lower()
    return [f.name for f in path.parent.glob("*.gguf")
            if f.name.lower().startswith(prefix)][:2]


def _profile_config_drift(config: Config,
                          live_path: Path | None = None,
                          profile_path: Path | None = None) -> tuple[bool | None, str]:
    """Compare the LIVE llama-swap config against the repo profile it was
    copied from. Drift is the recurring failure mode of 'the repo is fixed
    but the box still runs the old config'. Warn, don't fail — the user may
    have tuned the live copy deliberately."""
    live_path = live_path or Path.home() / ".config" / "llama-swap" / "config.yaml"
    profile_path = profile_path or (
        Path(__file__).resolve().parents[2] / "server" / "profiles"
        / config.models.profile / "llama-swap.yaml"
    )
    if not live_path.exists() or not profile_path.exists():
        return None, ""
    if live_path.read_bytes() == profile_path.read_bytes():
        return True, f"matches server/profiles/{config.models.profile}"
    return False, (f"DRIFTED from server/profiles/{config.models.profile} — run `orc sync` "
                   f"(re-copies the profile and restarts llama-swap)")


def _llama_swap_model_files(config_path: Path | None = None) -> list[tuple[str, Path]]:
    """(model_id, gguf_path) pairs parsed from the llama-swap config, with
    ${env.HOME} and user macros expanded. Best-effort: empty on any surprise."""
    config_path = config_path or Path.home() / ".config" / "llama-swap" / "config.yaml"
    if not config_path.exists():
        return []
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    home = str(Path.home())
    macros = {}
    for name, value in (data.get("macros") or {}).items():
        macros[str(name)] = str(value).replace("${env.HOME}", home)
    out: list[tuple[str, Path]] = []
    for model_id, spec in (data.get("models") or {}).items():
        if not isinstance(spec, dict):
            continue
        cmd = str(spec.get("cmd", "")).replace("${env.HOME}", home)
        for name, value in macros.items():
            cmd = cmd.replace("${" + name + "}", value)
        match = re.search(r"-m\s+(\S+)", cmd)
        if match:
            out.append((str(model_id), Path(match.group(1))))
    return out


UNLISTED_NOTE = (
    "Aliases route correctly even when unlisted, so these usually work anyway.\n"
    "To make the listing complete (also needed for Letta model discovery), the\n"
    "llama-swap config must contain includeAliasesInList: true — the shipped\n"
    "profiles do. Fix: run `orc sync` (re-copies the profile config and\n"
    "restarts llama-swap)."
)


# ---------------------------------------------------------------------- report


def redact(value):
    """Recursively blank STRING values whose keys look secret-bearing.
    Only strings: numeric fields like max_output_tokens must survive."""
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            secretish = any(marker in key.lower() for marker in _SECRET_MARKERS)
            if secretish and isinstance(inner, str):
                out[key] = "***"
            else:
                out[key] = redact(inner)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or proc.stderr or "").strip()
        return out if out else f"(exit {proc.returncode}, no output)"
    except FileNotFoundError:
        return "(not installed)"
    except Exception as exc:
        return f"(unavailable: {exc})"


def _mark(ok: bool | None) -> str:
    return {True: "ok", False: "FAIL", None: "warn"}[ok]


def _repo_sha() -> str:
    """Which eng-orc commit this box is actually running (editable install)."""
    repo = Path(__file__).resolve().parents[2]
    try:
        proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return "unknown"
        sha = proc.stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10)
        return sha + ("+dirty" if dirty.stdout.strip() else "")
    except Exception:
        return "unknown"


def build_report(svc, config: Config) -> str:
    parts: list[str] = [
        "# orc diagnostics report",
        f"_generated {iso_now()} · eng-orc {__version__} (commit {_repo_sha()}) · "
        f"python {sys.version.split()[0]} · {platform.platform()}_",
        "",
        "## Environment checks",
    ]
    rows, unlisted = gather_rows(svc, config)
    for name, ok, detail in rows:
        parts.append(f"- [{_mark(ok)}] {name}" + (f" — {detail}" if detail else ""))
    if unlisted:
        parts += ["", "Unlisted (warn) names: " + "; ".join(unlisted)]

    gpu_lines = _run(["nvidia-smi", "-L"]).splitlines()
    swap_binary = shutil.which("llama-swap") or str(Path.home() / ".local" / "bin" / "llama-swap")
    parts += [
        "",
        "## Versions",
        f"- llama.cpp tag: {_read(Path.home() / 'llama.cpp' / 'build' / '.engorc-tag')}",
        f"- llama-swap: {shorten(_run([swap_binary, '--version']), 120)}",
        f"- gpu: {shorten(gpu_lines[0], 120) if gpu_lines else '(none reported)'}",
        "",
        "## Config (secrets redacted)",
        "```yaml",
        yaml.safe_dump(redact(config.model_dump(mode="json")), sort_keys=False).strip(),
        "```",
        "",
        "## GPU",
    ]
    try:
        svc.observe_gpu()
        for entry in svc.timeline.current():
            parts.append(f"- resident: {entry['model']} ({entry['state']})")
            if entry["state"] == "ready":
                parts.append(f"  slots: {svc.swap.raw_slots(entry['model'])}")
        for event in svc.timeline.recent(8):
            parts.append(f"- {svc.timeline.describe(event)}")
    except Exception as exc:
        parts.append(f"(gpu section unavailable: {exc})")

    parts += ["", "## Projects"]
    try:
        projects = svc.registry.all_projects()
    except Exception as exc:
        projects = []
        parts.append(f"(could not list projects: {exc})")
    for project in projects:
        try:
            parts += _project_section(project)
        except Exception as exc:  # one broken project must not kill the report
            parts += [f"### {project.root.name}", f"(section failed: {exc})", ""]

    parts += [
        "## llama-swap service log (tail)",
        "```",
        shorten(_run(["journalctl", "--user", "-u", "llama-swap", "-n", "60", "--no-pager"], timeout=15), 6000),
        "```",
        "",
        "_This report is generated by `orc bugreport` and is safe to share: config",
        "values whose keys look secret-bearing are redacted before writing._",
    ]
    return "\n".join(parts) + "\n"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() or "(empty)"
    except OSError:
        return "(not found)"


def _latest_transcript_tail(project, item_id: str, max_chars: int = 2200) -> str:
    from .util import truncate_tail

    attempt_dir = project.artifacts.path("", subdir=f"attempts/{item_id}")
    if not attempt_dir.is_dir():
        return ""
    candidates = [p for p in attempt_dir.glob("*.md")
                  if not p.name.startswith(("handoff-", "review"))]
    if not candidates:
        return ""
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return truncate_tail(_read(newest), max_chars)


def _project_section(project) -> list[str]:
    """Everything needed to debug a project remotely: plan state, the
    substance of failed attempts (summaries, verification output, review
    blockers via notes), transcript tails, and gate texts."""
    meta = project.meta
    plan = project.load_plan()
    progress = plan.progress()
    gates = project.gates.all()
    open_gates = [g for g in gates if g.status == "open"]
    parts = [
        f"### {meta.slug}",
        f"phase {meta.phase} · state {meta.state}"
        + (f" ({meta.state_reason})" if meta.state_reason else "")
        + f" · plan {progress.get('done', 0)}/{progress.get('total', 0)}"
        + f" · open gates {len(open_gates)}",
    ]

    if plan.items:
        parts.append("")
        parts.append("| item | title | status | attempts | triaged |")
        parts.append("| --- | --- | --- | --- | --- |")
        for item in plan.items:
            triaged = sum(1 for n in item.notes if n.startswith("triage#"))
            parts.append(
                f"| {item.id[-6:]} | {shorten(item.title, 50)} | {item.status} "
                f"| {len(item.attempts)} | {triaged} |"
            )

    problem_items = [i for i in plan.items
                     if i.status in ("failed", "blocked") or (i.attempts and i.status != "done")]
    for item in problem_items[:4]:
        parts += ["", f"#### problem item {item.id[-6:]}: {shorten(item.title, 70)} ({item.status})"]
        if item.acceptance:
            parts.append("acceptance: " + "; ".join(shorten(a, 100) for a in item.acceptance))
        for attempt in item.attempts[-3:]:
            parts.append(f"- attempt [{attempt.role}/{attempt.model}] {attempt.outcome}: "
                         f"{shorten(attempt.summary, 260)}")
            if attempt.test_summary:
                parts.append(f"  verification: {shorten(attempt.test_summary, 400)}")
        for note in item.notes[-10:]:
            parts.append(f"- note: {shorten(note, 260)}")
        transcript = _latest_transcript_tail(project, item.id)
        if transcript:
            parts += ["", "latest attempt transcript (tail):", "```", transcript, "```"]

    if gates:
        parts.append("")
        parts.append("gates:")
        for gate in [g for g in gates if g.status == "open"][:5]:
            parts.append(f"- OPEN [{gate.from_role}] {shorten(gate.question, 400)}")
        for gate in [g for g in gates if g.status == "answered"][-3:]:
            parts.append(f"- answered [{gate.from_role}] Q: {shorten(gate.question, 200)} "
                         f"→ A: {shorten(gate.answer, 200)}")

    errors = project.journal.tail(10, kinds=[Kind.ERROR])
    if errors:
        parts.append("")
        parts.append("recent errors:")
        parts += [f"- [{e.ts[5:16]}] [{e.actor}] {shorten(str(e.payload.get('error', '')), 300)}"
                  for e in errors]
    activity = render_events(project.journal.tail(15))
    if activity:
        parts += ["", "recent activity:", activity]
    parts.append("")
    return parts


# ---------------------------------------------------------------------- sharing


def commit_and_push(path: Path) -> tuple[bool, str]:
    """Commit exactly this file in its repository and push. Returns (ok, message)."""
    repo_dir = path.parent

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo_dir), *args],
                              capture_output=True, text=True, timeout=120)

    if git("rev-parse", "--git-dir").returncode != 0:
        return False, f"{repo_dir} is not a git repository — commit and push the file manually"
    if git("add", "--", str(path)).returncode != 0:
        return False, "git add failed"
    commit = git("commit", "-m", f"orc bugreport {iso_now()}", "--", str(path))
    combined = (commit.stdout or "") + (commit.stderr or "")
    if commit.returncode != 0 and "nothing to commit" not in combined:
        return False, f"git commit failed: {shorten(combined, 200)}"
    push = git("push")
    if push.returncode != 0:
        return False, f"git push failed: {shorten(push.stderr, 200)} — push manually"
    return True, "report committed and pushed — pull it on the other machine"
