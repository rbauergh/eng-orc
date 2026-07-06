# Architecture

eng-orc runs a managed engineering process over sequential, scope-limited
agents on one GPU. This document explains the moving parts and — because the
design is best understood as a response to concrete failures — starts with
why v0 died.

## The v0 postmortem

The original `legacy/langgraph-harness.py` run (see `legacy/current-status.txt`)
loop-trapped asking *"What specific function name would you like to use?"*
a dozen times for a hello-world script. Every failure had a structural cause,
and each maps to a specific mechanism in this redesign:

| v0 failure | Root cause | Fix here |
| --- | --- | --- |
| PM interrogation loop | Prompt literally forbade accepting broad goals; binary `is_clear` gate | Charter with assumptions+confidence, a hard clarification budget, "proceed under recorded assumptions" as the default (`charterer.md`, `phase_charter`) |
| Answers ignored on re-ask | No memory: each PM call re-read a growing goal blob | Decisions log + gate answers injected once into a *revision* brief; nothing is ever re-litigated (`decisions.py`, `briefs.py`) |
| Blocking `input()` | Questions stopped the world | Gates: async inbox, project parks, scheduler moves on (`gates.py`, `orc inbox/answer`) |
| Routing on `"FAIL" in reviews` | Control flow parsed from prose | Grammar-constrained JSON verdicts with categorized findings (`schemas.py`, `structured.py`) |
| LLM deciding retry-vs-advance blind | Judgment-free decisions burned tokens on an 8B | Supervisor policy is pure code over disk state (`supervisor.py`) |
| Code fished out of markdown fences | `split("```python")` string surgery | Tools take code via fenced payloads; edits are exact SEARCH/REPLACE, syntax-gated before landing (`toolbox/fs.py`) |
| Nothing survived a crash | All state in process memory | The filesystem is the database; everything below is a file |

## The state model: filesystem as database

```
~/.eng-orc/
├── config.yaml            servers, model roles, budgets, memory backend
├── memory.db              long-term memory (SQLite FTS5; superset of Letta)
├── locks/gpu.lock         the single-flight GPU lease
└── projects/<slug>/
    ├── project.json       phase, state, priority, counters
    ├── mission.md         the goal + dated user amendments (orc ask)
    ├── charter.yaml       objective, assumptions(+confidence), success criteria
    ├── design.md          living design document
    ├── plan.yaml          work-item DAG — human-editable between runs
    ├── decisions.jsonl    ADR-lite log (assumption promotions, design choices)
    ├── gates.jsonl        question/answer fold (open gates derived, never mutated)
    ├── journal/           append-only event shards (every turn, tool call, verdict)
    ├── artifacts/         handoffs, reviews, attempt transcripts, report, doc versions
    ├── state/             langgraph checkpoints.sqlite, consumed-gate marks
    └── workroom/          the code being built (own git repo) — or an external
                           repo attached via `orc new --repo`
```

Invariants:
- every mutation is an atomic write or a locked JSONL append;
- any process can die at any instruction and `orc run` continues correctly,
  because *routing reads disk, not memory* (`supervisor.next_phase`
  reconstructs the correct phase from what actually exists);
- interrupted attempts are closed as `error` on the next visit and count
  against the item's attempt budget (`cleanup_dangling_attempts`).

## The engine: one graph step at a time

`orchestrator/graph.py` builds a LangGraph state machine whose single
invocation does exactly one unit of work:

```
START → route ──(reads disk)──→ scout | charter | design | plan | build | wrap → END
```

The scheduler (`scheduler.py`) round-robins runnable projects by (priority,
longest-idle), takes the GPU file lease, executes one step, and rotates.
Multi-project concurrency is interleaving, not parallelism — a 12 GB card is
single-tenant by nature, so fairness lives in the scheduler, and a parked
project costs nothing. LangGraph checkpoints (SQLite per project) record the
step timeline; losing them costs history, never correctness.

### Phases

- **scout** (brownfield only): read-only tool loop over an attached repo →
  `codebase-report.md`, plus index/repo-map warmup.
- **charter**: planner model, structured `Charter`. Blocking questions (each
  must justify *why* it changes the architecture, with options) become gates
  — at most `clarification_budget` ever; past that the charterer proceeds
  under recorded assumptions. User answers trigger a charter *revision*.
- **design**: architect writes `design.md` (prose, sectioned); the utility
  model then extracts its consequential decisions into the decision log so
  later briefs carry decisions, not essays.
- **plan**: planner emits a `PlanDraft` (items with acceptance criteria,
  verify commands, dependencies by index, sizes, test-first flags) →
  validated as a DAG, one retry with the validator's complaints quoted back.
- **build** (one item per step): supervisor picks the highest-priority ready
  item with attempt budget left; a tester loop writes failing tests first
  when flagged; the implementer loop works the item; **deterministic
  verification** runs the item's `verify_commands` (an agent saying "done"
  is a claim, not a fact); the reviewer returns a structured verdict whose
  blocker findings are recorded onto the item for the next attempt;
  approved items are committed (`<kind>: <title>`) and the index refreshes.
  Exhausted items fail; when nothing is runnable the supervisor opens a
  gate asking the user for direction — honestly stuck beats silently spinning.
- **wrap**: historian digests the journal → lessons/conventions/project card
  into long-term memory, writes `report.md`, marks the project done.

## The agent runtime (built for small models)

Tool-loop agents speak a deliberately primitive protocol (mini-SWE-agent
lineage): a short thought, then exactly one

```
ACTION: tool_name {"small": "scalar args"}
```payload
raw file content / patch / command
```
```

Design choices with receipts:
- **Code never travels inside JSON strings** (Aider's code-in-json benchmark:
  every model gets worse). Grammar-constrained JSON is reserved for
  *documents* (charter/plan/review), each schema with a free-text `reasoning`
  field first (dottxt: constrained decoding without a think-first field
  amputates chain-of-thought).
- **One action per turn** (BFCL multi-turn: small models collapse when
  orchestrating parallel calls); malformed turns get the error quoted back
  and cost a turn — self-repair, never a crash.
- **Syntax-gated edits** (SWE-agent's most load-bearing ablation): a write or
  SEARCH/REPLACE that would break Python syntax is rejected before landing.
- **Shaped observations**: capped middle-out (the end of a build log is where
  the truth lives), ANSI-stripped, with the current task + acceptance criteria
  re-recited at the end of every message (Manus recitation — fights
  lost-in-the-middle).
- **Repetition detection** (identical action twice → nudge; three times →
  attempt ends as `stuck`) and **history compaction** past a turn threshold
  (utility model summarizes older turns; mechanical truncation as fallback).
- **Context budgets are effective, not advertised**: quantized 7–35B models
  reason reliably in the 8–16k range (NoLiMa/RULER), so briefs are packed by
  the `ContextPacker` under per-role windows with prioritized, individually
  truncatable sections — and anything dropped is recorded.

Agents communicate through **artifacts, not chat history**: every attempt
leaves a transcript and a handoff document; every brief is rebuilt fresh from
disk (mission, charter/design excerpts, plan state, prior-attempt evidence,
retrieved code, recalled lessons). This is why resuming a project weeks later
is indistinguishable from the next scheduler tick.

## Context: how agents see the codebase

Three complementary channels, each degrading independently (`context/`):
1. **Repo map** — ctags (AST/regex fallback) symbols ranked by definition
   weight × git recency × reference count × focus proximity, rendered under a
   token budget and cached by tree digest. Always available.
2. **Semantic index** — LlamaIndex `IngestionPipeline` with a persisted
   docstore (UPSERTS_AND_DELETE: only changed files re-embed) over a
   per-project Chroma collection; tree-sitter `CodeSplitter` per language
   with sentence-split fallback; embeddings from the CPU-resident model.
3. **Exact search** — ripgrep (python fallback) for identifier probes.

`HybridRetriever.gather` fuses focus-file excerpts, symbol definitions,
vector hits, and grep matches, deduplicates by span overlap, and cuts to
budget.

## Memory: lessons that outlive projects

Two layers behind one `MemoryStore` protocol (`memory/`):
- **Local SQLite (FTS5)** — always on, the durable superset. Zero setup.
- **Letta** — the librarian agent's archival passages (semantic search, only
  the embedding model runs on the write/search path) plus shared memory
  blocks (`user_profile`, `engineering_conventions`) injected into every
  brief. Wrap-up optionally sends the librarian a digest to curate its blocks
  (explicit, scheduled — sleeptime agents would thrash model swaps on one GPU).

The composite store writes locally first, mirrors to Letta, queues an outbox
when Letta is down, and reconciles with `orc memory sync`. Losing the Letta
container never loses memory.

## Serving: extracting the most from 12 GB

llama.cpp `llama-server` processes behind **llama-swap**: chat models live in
an exclusive swap group (one resident at a time, TTL auto-unload), the code
embedder runs CPU-only in a persistent group so indexing never evicts the
chat model. The default profile serves coder and planner from the *same*
resident Qwen3.6-35B-A3B process — orc pins thinking off/on per request via
`chat_template_kwargs`, so the two most-alternated roles never swap at all.
KV cache is q8_0 (q4 measurably damages tool-calling), flash attention on,
`--cache-reuse` for prefix reuse across turns, MoE expert layers CPU-offloaded
with `--n-cpu-moe` (decode is RAM-bandwidth-bound — enable XMP). Details and
budgets: [docs/MODELS.md](docs/MODELS.md).

## Verification story

`orc selftest` runs the complete lifecycle against a deterministic FakeLLM —
including an inbox round-trip, real pytest verification inside the workroom,
git commits, review artifacts, memory writes, and a cold restart mid-mission —
in under a minute, no GPU. The pytest suite covers the state layer, parsing,
packing, toolbox safety (path jail, denylist, syntax gates), scheduler policy,
and memory, and ends with that same e2e. If selftest is green on a machine,
the orchestrator works there; `orc doctor` then checks the *servers*.
