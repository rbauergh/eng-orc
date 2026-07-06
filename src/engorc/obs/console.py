"""Console output: one shared rich logger for the CLI and orchestrator.

Verbosity follows config.log_level (debug|info|warn|error). All dynamic
content is markup-escaped before printing — model ids, test output, and
item titles routinely contain square brackets, which rich would otherwise
parse as style tags and crash on. Styling lives only in the fixed wrappers
here; callers never embed markup in messages.
"""

from __future__ import annotations

import os

from rich.console import Console
from rich.markup import escape

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


class Log:
    def __init__(self) -> None:
        self.console = Console(highlight=False)
        self.level = _LEVELS.get(os.environ.get("ENGORC__LOG_LEVEL", "info").lower(), 20)

    def set_level(self, name: str) -> None:
        self.level = _LEVELS.get(name.lower(), 20)

    def debug(self, message: str) -> None:
        if self.level <= 10:
            self.console.print(f"[dim]· {escape(message)}[/dim]")

    def info(self, message: str) -> None:
        if self.level <= 20:
            self.console.print(escape(message))

    def success(self, message: str) -> None:
        if self.level <= 20:
            self.console.print(f"[green]✓[/green] {escape(message)}")

    def warn(self, message: str) -> None:
        if self.level <= 30:
            self.console.print(f"[yellow]⚠ {escape(message)}[/yellow]")

    def error(self, message: str) -> None:
        self.console.print(f"[red]✗ {escape(message)}[/red]")

    def agent(self, role: str, message: str) -> None:
        if self.level <= 20:
            self.console.print(f"[bold cyan]{escape(role)}[/bold cyan] {escape(message)}")

    def step(self, subject: str, note: str) -> None:
        """The scheduler's per-step progress line."""
        if self.level <= 20:
            self.console.print(f"[bold]{escape(subject)}[/bold] · {escape(note)}")

    def rule(self, title: str = "") -> None:
        if self.level <= 20:
            self.console.rule(escape(title) if title else "")


log = Log()
