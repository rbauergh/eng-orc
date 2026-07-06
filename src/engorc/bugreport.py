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
    # "upstream command exited prematurely" at load time — catch it here.
    for model_id, path in _llama_swap_model_files():
        row(f"model file: {model_id}", path.exists(),
            str(path) if path.exists()
            else f"MISSING {path} — download it or fix the -m filename in the llama-swap config")
    return rows, unlisted


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
    "profiles do. Easiest fix: re-run scripts/setup_wsl.sh, which re-copies the\n"
    "profile config and restarts llama-swap."
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


def build_report(svc, config: Config) -> str:
    parts: list[str] = [
        "# orc diagnostics report",
        f"_generated {iso_now()} · eng-orc {__version__} · python {sys.version.split()[0]} · {platform.platform()}_",
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
        "## Projects",
    ]
    try:
        projects = svc.registry.all_projects()
    except Exception as exc:
        projects = []
        parts.append(f"(could not list projects: {exc})")
    for project in projects:
        try:
            meta = project.meta
        except FileNotFoundError:
            continue
        progress = project.load_plan().progress()
        parts += [
            f"### {meta.slug}",
            f"phase {meta.phase} · state {meta.state}"
            + (f" ({meta.state_reason})" if meta.state_reason else "")
            + f" · plan {progress.get('done', 0)}/{progress.get('total', 0)}"
            + f" · open gates {len(project.gates.open_gates())}",
        ]
        errors = project.journal.tail(5, kinds=[Kind.ERROR])
        if errors:
            parts.append("recent errors:")
            parts += [f"- [{e.ts[5:16]}] {shorten(str(e.payload.get('error', '')), 300)}" for e in errors]
        activity = render_events(project.journal.tail(10))
        if activity:
            parts += ["recent activity:", activity]
        parts.append("")

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
