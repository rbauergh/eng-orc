# eng-orc

A multi-project software-engineering orchestrator for a **single local GPU**
(designed around an RTX 4070 Ti, 12 GB, under WSL2). It runs a managed
engineering process — charter → design → plan → build/verify/review → wrap —
over a sequence of scope-limited agents served by hot-swapped local models,
with durable on-disk state, asynchronous user questions, a semantic codebase
index, and long-term memory that carries lessons between projects.

**Design pillars**

- **The filesystem is the database.** Every piece of state — mission, charter,
  design, plan DAG, decisions, questions, journal, transcripts — is a
  human-readable file. Kill the process at any time; `orc run` resumes with a
  fresh effort reconstructed from disk.
- **One GPU, one flight.** Models swap behind [llama-swap]; the scheduler
  serializes work, batches by resident model, and runs many projects
  concurrently by interleaving *steps*, not processes.
- **Structured decisions, freeform prose.** Anything that gates control flow
  is grammar-constrained JSON (llama.cpp enforces the schema server-side).
  Code and shell commands are never JSON-escaped — agents act through a
  plain-text `ACTION:` protocol built for small models.
- **Judgment over interrogation.** Agents assume-and-record instead of
  interrogating (bounded clarification budget). Genuinely blocking questions
  park the project in an **inbox** (`orc inbox` / `orc answer`) while other
  projects keep running — never a blocking prompt.

## Quickstart (Windows + WSL2 Ubuntu, RTX 4070 Ti)

```bash
# inside WSL2 Ubuntu, from the repo root:
scripts/setup_wsl.sh --profile balanced-12gb     # builds llama.cpp (CUDA), installs
                                                 # llama-swap, downloads models,
                                                 # creates the venv, installs orc,
                                                 # sets up systemd services (+ Letta)
source .venv/bin/activate
orc selftest        # full-pipeline check — no GPU or servers needed
orc doctor          # checks servers, models, memory, binaries
orc new "build me a CLI tool that ..."           # start a mission
orc run --watch     # let it work; it parks on questions
orc inbox           # see questions; orc answer <id> "..."
orc status          # where everything stands
```

Windows-side prerequisites (NVIDIA driver, `.wslconfig` memory sizing,
autostart) are covered in [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Try it anywhere (no GPU)

The orchestrator is fully exercisable without any model server:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/orc selftest
```

`orc selftest` runs a complete scripted mission through the real machinery —
charter with a blocking question, an inbox answer, design, planning,
test-first implementation with **real pytest verification**, review, git
commits, wrap-up, and memory writes — including a mid-mission cold restart to
prove resumability. Green selftest = the harness works on this machine.

## The stack

| Concern | Choice |
| --- | --- |
| Serving | [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` (CUDA build) behind [llama-swap] — one endpoint, hot-swapped models, resident CPU embedder |
| Models (default profile) | Qwen3.6-35B-A3B (coder+planner, MoE with CPU-offloaded experts), Qwen3.5-4B (utility), jina-code-embeddings-0.5b (embeddings, CPU), plus a **three-family review panel** (Qwen + gpt-oss-20b + GLM-4.7-Flash) signing off every diff — see [docs/MODELS.md](docs/MODELS.md) |
| Orchestration | [LangGraph](https://github.com/langchain-ai/langgraph) phase machine with SQLite checkpoints, one phase-unit per scheduler step |
| Code context | [LlamaIndex](https://github.com/run-llama/llama_index) + Chroma incremental index, ctags/AST repo map, hybrid retrieval |
| Long-term memory | [Letta](https://github.com/letta-ai/letta) archival passages + shared blocks, layered over an always-available local SQLite store |

[llama-swap]: https://github.com/mostlygeek/llama-swap

## Layout

```
src/engorc/            the package (state, llm, context, memory, agents, orchestrator, cli)
server/profiles/       model profiles: llama-swap config + download manifest + orc models config
server/letta/          docker-compose for the Letta memory server
scripts/               WSL2 one-shot setup, model downloads, systemd units
docs/                  architecture, models & VRAM budgets, operations, troubleshooting
tests/                 pytest suite (all GPU-less, FakeLLM-driven)
legacy/                the v0 harness this project replaces (kept for the postmortem)
~/.eng-orc/            runtime home: config.yaml, memory.db, projects/<slug>/...
```

Every project directory under `~/.eng-orc/projects/<slug>/` is self-contained:
`mission.md`, `charter.yaml`, `design.md`, `plan.yaml` (editable), `decisions.jsonl`,
`gates.jsonl`, `journal/`, `artifacts/` (handoffs, reviews, transcripts, report),
and `workroom/` (the actual code, its own git repo) — or an attached external
repository via `orc new --repo`.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — how it works and why, incl. the v0 postmortem
- [docs/MODELS.md](docs/MODELS.md) — model roster, VRAM budgets, profile tuning
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — day-to-day runbook, Windows/WSL2 setup
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
