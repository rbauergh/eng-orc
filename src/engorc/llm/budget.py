"""Token-budgeted context packing.

Small quantized models degrade sharply as context fills; the packer treats
the context window as a hard budget and every prompt section as a
prioritized, individually-truncatable block. When the budget is exceeded,
low-priority sections shrink (middle-out or tail-keep) and then drop, and
what was dropped is recorded so the caller can journal it — silent
truncation reads as "covered everything" when it didn't.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from ..util import approx_tokens, truncate_middle, truncate_tail

TruncateMode = Literal["middle", "tail", "drop"]


@dataclass
class Section:
    name: str
    text: str
    priority: int = 5  # 1 = never sacrificed first; larger = more expendable
    min_tokens: int = 120  # floor before the section drops entirely
    truncate: TruncateMode = "middle"

    def header(self) -> str:
        return f"## {self.name}\n"


@dataclass
class PackResult:
    text: str
    tokens: int
    dropped: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)


class ContextPacker:
    def __init__(
        self,
        context_window: int,
        reserve_output: int,
        counter: Callable[[str], int] | None = None,
        overhead: int = 300,  # chat template + role framing slack
    ):
        self.budget = max(512, context_window - reserve_output - overhead)
        self.count = counter or approx_tokens

    def pack(self, sections: list[Section], fixed_tokens: int = 0) -> PackResult:
        """fixed_tokens covers content outside the packer's control (system prompt)."""
        available = self.budget - fixed_tokens
        live = [s for s in sections if s.text.strip()]
        sizes = {s.name: self.count(s.header() + s.text) for s in live}
        dropped: list[str] = []
        truncated: list[str] = []

        def total() -> int:
            return sum(sizes[s.name] for s in live)

        # Shrink from the most expendable end until we fit.
        for section in sorted(live, key=lambda s: -s.priority):
            if total() <= available:
                break
            overshoot = total() - available
            current = sizes[section.name]
            target_tokens = max(section.min_tokens, current - overshoot)
            if target_tokens < current:
                max_chars = max(200, int(target_tokens * 3.6))
                if section.truncate == "tail":
                    section.text = truncate_tail(section.text, max_chars)
                else:
                    section.text = truncate_middle(section.text, max_chars)
                sizes[section.name] = self.count(section.header() + section.text)
                truncated.append(section.name)

        # Still over: drop whole sections, most expendable first.
        for section in sorted(list(live), key=lambda s: -s.priority):
            if total() <= available:
                break
            if section.truncate == "drop" or section.priority > 1:
                live.remove(section)
                sizes.pop(section.name, None)
                dropped.append(section.name)

        # Hard clamp: even a prompt of only priority-1 sections must fit,
        # otherwise the server rejects the request outright.
        while total() > available and live:
            biggest = max(live, key=lambda s: sizes[s.name])
            target_chars = max(200, int((sizes[biggest.name] - (total() - available)) * 3.6))
            new_text = truncate_middle(biggest.text, target_chars)
            if new_text == biggest.text:
                live.remove(biggest)
                sizes.pop(biggest.name, None)
                dropped.append(biggest.name)
            else:
                biggest.text = new_text
                sizes[biggest.name] = self.count(biggest.header() + biggest.text)
                truncated.append(biggest.name)

        parts = [s.header() + s.text.rstrip() for s in sections if s in live]
        text = "\n\n".join(parts)
        return PackResult(text=text, tokens=self.count(text), dropped=dropped, truncated=truncated)
