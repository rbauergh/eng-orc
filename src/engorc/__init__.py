"""eng-orc: a multi-project software-engineering orchestrator for a single local GPU.

Design pillars:

- The filesystem is the database. Every piece of project state lives on disk in
  human-readable form (markdown, YAML, JSONL). A project can be inspected,
  edited, zipped, or resumed at any time; the process holds no state that
  cannot be reconstructed from disk.
- One GPU, one flight. Models are hot-swapped behind an OpenAI-compatible
  proxy; the scheduler serializes LLM work and batches by model affinity to
  minimize swaps.
- Structured decisions, freeform prose. Anything that gates control flow is
  grammar-constrained JSON validated against a schema. Prose artifacts
  (charters, designs, reviews) are markdown. Control flow is never parsed out
  of prose.
- Judgment over interrogation. Agents make assumptions, record them with
  confidence, and only ask the user when the answer would change the
  architecture. Questions are asynchronous gates in an inbox, never a blocking
  prompt.
"""

__version__ = "0.1.0"
