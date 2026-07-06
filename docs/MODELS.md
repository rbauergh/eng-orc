# Models

Model choices for a 12 GB RTX 4070 Ti as of mid-2026, with the reasoning and
the VRAM math. Profiles live in `server/profiles/<name>/` (llama-swap config +
download manifest + orc models config); switch by re-running
`scripts/setup_wsl.sh --profile <name>`.

## Profiles at a glance

| Profile | Coder | Planner | Utility | Embeddings | Needs |
| --- | --- | --- | --- | --- | --- |
| **balanced-12gb** (default) | Qwen3.6-35B-A3B IQ4_XS (17.7 GB) | same model, thinking on | Qwen3.5-4B (2.8 GB) | jina-code-0.5b (1.0 GB, CPU) | 12 GB VRAM + 24 GB WSL RAM |
| **max-64gb** | Qwen3-Coder-Next 80B-A3B IQ4_XS (42.9 GB) | Qwen3.6-35B-A3B-MTP Q4_K_XL (23.3 GB) | same | same | 48 GB WSL RAM, DDR5 strongly advised |
| **classic-12gb** | Qwen2.5-Coder-14B Q4_K_M (9.0 GB, dense) | gpt-oss-20b MXFP4 (12.1 GB) | Qwen3-4B-2507 | Qwen3-Embedding-0.6B | works with 16 GB RAM |

## Why these (mid-2026 snapshot)

- **Qwen3.6-35B-A3B** — the best agentic-coding open model that runs on this
  card: SWE-bench Verified 73.4, Terminal-Bench 2.0 51.5. MoE with only ~3B
  active parameters and a hybrid-attention design whose KV cache costs
  ~20 KB/token — long agent contexts are nearly free. Expert layers live in
  system RAM (`--n-cpu-moe`); expect ~20–40 tok/s depending on RAM speed.
  One resident process serves both coder (thinking off) and planner
  (thinking on) via per-request `chat_template_kwargs` — zero swaps between
  the two most-alternated roles. Thinking-off on the coder path also
  side-steps the known llama.cpp Qwen3.x tool-tag parser bug (#21158) —
  which eng-orc is doubly immune to, since its tool loop uses a plain-text
  protocol rather than native tool calls.
- **Qwen3-Coder-Next 80B-A3B** (max profile) — proven ~40 tok/s generation on
  an RTX 4070 12 GB + DDR5-6000 with `--fit on`; SWE-bench V 70.6 and
  Aider 66.2 while *non-thinking*, i.e. snappy in tool loops.
- **gpt-oss-20b** — the speed/reliability alternate: 56–67+ tok/s measured on
  12 GB-class cards with `--n-cpu-moe 3-4`, Apache-2.0, the most
  battle-tested (harmony) template stack in llama.cpp, `reasoning_effort`
  dial for planning. Serves as classic-profile planner and optional
  `coder-fast` alias in the balanced profile.
- **Qwen3.5-4B** — utility summarizer/extractor at 100+ tok/s; 262K native
  context; also the Letta librarian's model (`openai/utility`).
- **jina-code-embeddings-0.5b** — code-specialized embeddings that beat
  general 0.6B embedders by ~5 points on code retrieval; F16 GGUF runs
  comfortably on CPU (`-ngl 0`), so it stays resident forever without
  costing VRAM.
- **Qwen2.5-Coder-14B + gpt-oss-20b** (classic) — the proven-2025 dense
  stack: zero MoE-offload moving parts, mature templates, lowest RAM bar.
  Optional 1.5–2.5× code-gen speedup via the 0.5B draft
  (`--spec-type draft-simple`, flags in the profile comments).

## VRAM budget (12 GB card, ~10.8–11.3 GB usable beside the Windows desktop)

KV bytes/token = 2 × full-attention layers × KV heads × head dim × bytes/elem
(q8_0 ≈ 0.53× f16). Compute buffer ≈ 0.5–1.2 GB at `-ub 512` with FA; CUDA
context ≈ 0.5 GB.

| Config | GPU weights | KV | Buffers | Total GPU | Host RAM |
| --- | --- | --- | --- | --- | --- |
| Qwen3.6-35B IQ4_XS, 48k ctx q8, `--n-cpu-moe 32` | ~4–5 GB (attn+shared+some experts) | ~0.5 GB | ~1.7 GB | **~6–7 GB** (tune experts onto the GPU with spare VRAM) | ~15 GB |
| Qwen3-Coder-Next IQ4_XS, 64k q8, `--fit on` | ~6–7 GB | ~0.8 GB | ~2 GB | **~9–10 GB** | ~40 GB |
| gpt-oss-20b MXFP4, 32k, `--n-cpu-moe 4` | ~9.5 GB | ~0.8 GB | ~1 GB | **~10.5 GB** | ~3 GB |
| Qwen2.5-Coder-14B Q4_K_M, 12k q8 (dense) | 8.99 GB | ~1.2 GB | ~1.4 GB | **~11.5 GB** (ceiling — hence ctx 12k) | ~1 GB |
| Qwen3.5-4B Q4_K_XL, 32k q8 | 2.78 GB | ~1.0 GB | ~0.6 GB | **~4.3 GB** | ~1 GB |

Performance levers, in order of impact:
1. **Enable XMP/EXPO in the BIOS.** MoE decode is RAM-bandwidth-bound;
   DDR5-6000 vs DDR4-3200 is roughly a 2–3× generation-speed difference.
2. `--n-cpu-moe`: start at the profile value, lower it until `nvidia-smi`
   shows ~11 GB — every expert layer moved onto the GPU is speed.
3. Keep models on ext4 (`~/models`), never `/mnt/c` (9P I/O cripples loads),
   and size `.wslconfig` memory so the CPU-resident experts never swap
   (`server/wslconfig.sample`).
4. KV stays q8_0 — q4 KV measurably degrades tool-calling accuracy.
5. Optional speculative decoding: MTP variants for Qwen3.6
   (`--spec-type draft-mtp`, ~1.3–2× decode), classic 0.5B draft for the
   dense 14B (~1.5–2.5× on code). Note: Qwen3.5/3.6 use a new 248k-token
   vocabulary — old Qwen3-0.6B drafts do NOT pair with them; use MTP.

## Changing models

1. Edit (or add) a profile under `server/profiles/<name>/`: the llama-swap
   entry (exact GGUF filename + flags), the manifest line, and the orc
   models section. Keep the aliases `coder`/`planner`/`utility`/`embed` —
   orc and Letta address models only through them.
2. `scripts/download_models.sh --profile <name>`
3. `cp server/profiles/<name>/llama-swap.yaml ~/.config/llama-swap/config.yaml`
   and `systemctl --user restart llama-swap`
4. Update `~/.eng-orc/config.yaml`'s models section (setup does this for you).
5. `orc doctor`, then `orc chat coder "write a haiku about VRAM"`.

Model temperatures follow each family's published recommendations (Qwen3.6
coding 0.6 / thinking higher; gpt-oss 1.0 with no repeat penalty; dense
Qwen2.5 low) — set per role in the orc config, which overrides server flags.
