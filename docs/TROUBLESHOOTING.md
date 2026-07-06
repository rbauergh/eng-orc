# Troubleshooting

Work top-down: `orc selftest` proves the orchestrator, `orc doctor` proves
the environment, then per-symptom below.

## Serving

**`orc doctor` says the LLM server is unreachable.**
`systemctl --user status llama-swap`; if inactive, `scripts/start_stack.sh`.
If the unit is missing, systemd probably isn't enabled in WSL —
`/etc/wsl.conf` → `[boot] systemd=true`, `wsl --shutdown`, re-run setup.

**First request after idle takes ages / 503s briefly.**
That's the model swapping into VRAM (tens of seconds for the 35B from a warm
page cache; minutes cold). eng-orc retries 503s automatically. Raise
`healthCheckTimeout` in the llama-swap config if loads legitimately exceed it.

**Out of VRAM (CUDA OOM in `journalctl --user -u llama-swap`).**
Something else is holding VRAM (check Task Manager on Windows — browsers and
games count) or the profile is tuned too hot. Raise `--n-cpu-moe`, lower
`-c`, or switch to the classic profile. Keep ~1 GB headroom for the desktop.

**Generation is painfully slow on the MoE models.**
Almost always RAM: XMP/EXPO disabled (fix in BIOS — up to 3×), WSL memory
sized too small so experts hit swap (`.wslconfig`), or models on `/mnt/c`
(move to `~/models`). Verify with `free -g` during generation.

**Model answers are garbled / tool actions never parse.**
Chat template mismatch. Confirm the GGUF is the exact file from the manifest
(unsloth GGUFs carry template fixes), the llama.cpp build matches the pinned
tag, and thinking is off for the coder role (`enable_thinking: false` in the
orc models config). Fall back to `--profile classic-12gb` to isolate:
if classic behaves, it's the new-model stack, not eng-orc.

## Orchestrator

**A project sits at `blocked_on_user` and nothing happens.**
By design — `orc inbox`, answer, `orc run`. If the gate is stale,
`orc answer <id> "proceed with your assumption"`.

**An item keeps failing attempts.**
Read the evidence: `orc status <slug>` for the plan, then the attempt
transcripts and `review.md` under
`~/.eng-orc/projects/<slug>/artifacts/attempts/<item>/`. Usual causes: vague
acceptance criteria (edit plan.yaml, tighten them), a too-large item (split
it), or verify commands that can't pass in the environment (fix them). After
attempt exhaustion the supervisor asks you for direction via a gate.

**`step errored: ...` in the run log.**
The step was journaled and the project stays runnable; the next visit retries
freshly. Persistent errors are usually the server (see above) or a structured
call repeatedly failing validation — check `journal/` for `structured_call`
events with `ok: false`, and consider a stronger planner model.

**Charter asks something it should have assumed.**
Answer once — it lands in the decision log and is never re-asked. If it
happens systematically, lower the bar in `agents/prompts/charterer.md`
or reduce `run.clarification_budget` to force assumptions.

**Semantic index unavailable.**
`orc index <slug>` prints why (packages missing → `pip install -e .`;
embedding endpoint down → check llama-swap; disabled → config). Everything
still works via repo map + grep — the index is an accelerator.

## Memory / Letta

**"permission denied" talking to the Docker API (pulling letta/letta).**
Your user isn't in the docker group: `sudo usermod -aG docker $USER`, then a
NEW shell (`newgrp docker` for the current one; `wsl --shutdown` applies it
everywhere). If it instead says the daemon isn't running:
`sudo systemctl enable --now docker`. The stack scripts print this guidance
themselves now.

**Doctor shows `letta unreachable`.**
`cd server/letta && docker compose ps` / `logs`. First boot migrations take
~2 min. eng-orc keeps writing to the local store and syncs later
(`orc memory sync`).

**Letta broke after an image update.**
This is why `LETTA_TAG` exists — pin it back to the last working tag in
`server/letta/.env`, `docker compose up -d`. Memory written meanwhile is in
the local store; sync pushes it.

## WSL2

**Setup fails with "generator Ninja does not match previously used Unix
Makefiles".** A pre-existing `~/llama.cpp/build` from an earlier manual build.
The installer resets mismatched build trees automatically; on an older copy
of the script, `rm -rf ~/llama.cpp/build` and re-run.

**`nvidia-smi` missing inside WSL.** Windows driver not installed/too old, or
WSL needs `wsl --update` + `wsl --shutdown`.

**Everything slows to a crawl after hours of use.** WSL's
`autoMemoryReclaim` evicting mmap'd weights — set it `disabled`
(`server/wslconfig.sample`) or rely on the profiles' `--mlock` (the systemd
unit already sets `LimitMEMLOCK=infinity`).

**Ports unreachable from Windows.** Default NAT forwards localhost only;
that's all eng-orc needs (`127.0.0.1:9292`, `:8283`). If you changed
networkingMode, remember mirrored mode has VPN/Docker quirks.

## Escalation path

1. `orc selftest --keep` — if this fails, the bug is in eng-orc itself; the
   kept home directory plus the journal shards are everything needed to debug.
2. `orc doctor` — environment.
3. `orc chat utility "ping"` then `orc chat coder "ping"` — serving path.
4. The journal never lies: `~/.eng-orc/projects/<slug>/journal/*.jsonl`.
5. Getting details to whoever is helping: `orc bugreport --push` (sanitized
   report, committed and pushed as one file — see docs/OPERATIONS.md).
