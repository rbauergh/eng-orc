# orc diagnostics report
_generated 2026-07-06T05:40:17+00:00 · eng-orc 0.1.0 · python 3.12.3 · Linux-5.15.133.1-microsoft-standard-WSL2-x86_64-with-glibc2.39_

## Environment checks
- [ok] home writable — /home/rbauer/.eng-orc
- [ok] config file — /home/rbauer/.eng-orc/config.yaml
- [ok] binary: git
- [ok] binary: rg
- [ok] binary: ctags
- [ok] binary: bash
- [ok] python: langgraph — orchestration
- [ok] python: llama_index.core — code index
- [ok] python: chromadb — vector store
- [ok] python: letta_client — letta memory
- [ok] llm server — http://127.0.0.1:9292/v1
- [ok] model role: coder — coder
- [ok] model role: planner — planner
- [ok] model role: utility — utility
- [ok] model role: second-opinion — coder-fast
- [ok] model role: third-opinion — third-opinion
- [ok] model role: deep-reasoner — deep-reasoner
- [ok] review seat: correctness — coder
- [ok] review seat: adversarial — coder-fast
- [ok] review seat: tests — third-opinion
- [ok] review seat: architecture — deep-reasoner
- [ok] coder fallback: second-opinion — coder-fast
- [ok] embeddings — embed (dim 896)
- [ok] swap control — http://127.0.0.1:9292
- [ok] memory — sqlite at /home/rbauer/.eng-orc/memory.db; letta: not configured
- [ok] nvidia-smi — GPU visibility (informational)

## Versions
- llama.cpp tag: b9878
- llama-swap: (not installed)
- gpu: GPU 0: NVIDIA GeForce RTX 4070 Ti (UUID: GPU-b87ac69a-5a84-c323-24de-8fb2774d3bc3)

## Config (secrets redacted)
```yaml
home: /home/rbauer/.eng-orc
server:
  base_url: http://127.0.0.1:9292/v1
  control_url: http://127.0.0.1:9292
  api_key: '***'
  embeddings_url: null
  connect_timeout: 10.0
  request_timeout: 900.0
  max_retries: 3
models:
  profile: balanced-12gb
  coder:
    model: coder
    context_window: 16384
    max_output_tokens: 3072
    temperature: 0.6
    top_p: 0.95
    thinking: false
    supports_schema: true
    extra_body:
      chat_template_kwargs:
        enable_thinking: false
  planner:
    model: planner
    context_window: 12288
    max_output_tokens: 4096
    temperature: 0.8
    top_p: 0.95
    thinking: true
    supports_schema: true
    extra_body:
      chat_template_kwargs:
        enable_thinking: true
  utility:
    model: utility
    context_window: 8192
    max_output_tokens: 1024
    temperature: 0.2
    top_p: 0.95
    thinking: false
    supports_schema: true
    extra_body: {}
  embedder:
    model: embed
    batch_size: 16
    max_chars: 6000
  extra:
    second-opinion:
      model: coder-fast
      context_window: 16384
      max_output_tokens: 3072
      temperature: 1.0
      top_p: 1.0
      thinking: true
      supports_schema: false
      extra_body:
        chat_template_kwargs:
          reasoning_effort: high
    third-opinion:
      model: third-opinion
      context_window: 16384
      max_output_tokens: 3072
      temperature: 0.7
      top_p: 1.0
      thinking: true
      supports_schema: false
      extra_body: {}
    deep-reasoner:
      model: deep-reasoner
      context_window: 12288
      max_output_tokens: 4096
      temperature: 0.6
      top_p: 0.95
      thinking: true
      supports_schema: false
      extra_body: {}
run:
  clarification_budget: 2
  max_attempts_per_item: 3
  max_turns_coder: 40
  max_turns_oneshot_tools: 12
  review_required: true
  compact_after_turns: 14
  coder_fallbacks:
  - second-opinion
  max_tool_output_chars: 6000
  shell_timeout: 300.0
  verify_timeout: 600.0
  max_steps_per_project_visit: 1
review:
  panel:
  - model_role: coder
    lens: correctness
  - model_role: second-opinion
    lens: adversarial
  - model_role: third-opinion
    lens: tests
  - model_role: deep-reasoner
    lens: architecture
memory:
  backend: auto
  letta_base_url: http://127.0.0.1:8283
  letta_token: null
  letta_agent: orc-librarian
  letta_model_handle: null
  letta_embedding_handle: null
  timeout: 30.0
  recall_top_k: 5
  curate_with_agent: true
index:
  enabled: true
  ignore:
  - .git
  - .hg
  - .svn
  - node_modules
  - .venv
  - venv
  - __pycache__
  - dist
  - build
  - target
  - .idea
  - .vscode
  - .pytest_cache
  - .ruff_cache
  - .eng-orc
  - '*.lock'
  - '*.min.*'
  max_file_kb: 384
  chunk_lines: 60
  chunk_overlap_lines: 10
  max_chars_per_chunk: 3000
  top_k: 8
  repomap_tokens: 1600
scheduler:
  poll_seconds: 5.0
  gpu_lock_timeout: 3600.0
log_level: info
```

## Projects
### hello-world-script
phase build · state blocked_on_user (work items exhausted their attempts) · plan 0/1 · open gates 1
recent errors:
- [07-06T05:37] panel seat unavailable: LLM server returned 500: {"error":"unspecific error: upstream command exited prematurely","src":"llama-swap"}
recent activity:
- [07-06T05:37] error: panel seat unavailable: LLM server returned 500: {"error":"unspecific error: upstream command exited prematurely","src":"llama-swap"}
- [07-06T05:37] attempt started by None on wi_1kwty2343wv6sm11d
- [07-06T05:38] attempt on wi_1kwty2343wv6sm11d: stuck — three malformed replies in a row
- [07-06T05:38] state → blocked_on_user work items exhausted their attempts

## llama-swap service log (tail)
```
Jul 05 22:39:27 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 57.349µs Jul 05 22:39:27 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 39.285µs Jul 05 22:39:29 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 46.549µs Jul 05 22:39:29 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 31.851µs Jul 05 22:39:31 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 46.168µs Jul 05 22:39:31 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 38.744µs Jul 05 22:39:33 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 63.39µs Jul 05 22:39:33 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 30.378µs Jul 05 22:39:35 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 46.117µs Jul 05 22:39:35 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 34.686µs Jul 05 22:39:37 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 39.655µs Jul 05 22:39:37 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 39.815µs Jul 05 22:39:39 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 44.204µs Jul 05 22:39:39 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 36.309µs Jul 05 22:39:41 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 42.611µs Jul 05 22:39:41 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 33.434µs Jul 05 22:39:43 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 51.738µs Jul 05 22:39:43 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 37.912µs Jul 05 22:39:45 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 51.568µs Jul 05 22:39:45 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 34.345µs Jul 05 22:39:47 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 40.427µs Jul 05 22:39:47 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 34.686µs Jul 05 22:39:49 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 57.239µs Jul 05 22:39:49 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 33.473µs Jul 05 22:39:51 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 39.144µs Jul 05 22:39:51 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 37.151µs Jul 05 22:39:53 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 53.071µs Jul 05 22:39:53 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 33.945µs Jul 05 22:39:55 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 39.966µs Jul 05 22:39:55 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 35.657µs Jul 05 22:39:57 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 44.465µs Jul 05 22:39:57 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 43.152µs Jul 05 22:39:59 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 41.028µs Jul 05 22:39:59 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 33.594µs Jul 05 22:40:01 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 40.657µs Jul 05 22:40:01 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 32.301µs Jul 05 22:40:03 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 40.357µs Jul 05 22:40:03 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 34.205µs Jul 05 22:40:05 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 40.517µs Jul 05 22:40:05 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 34.665µs Jul 05 22:40:07 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /v1/models HTTP/1.1" 200 1562 "python-httpx/0.28.1" 47.02µs Jul 05 22:40:08 BATTLESTATION-REN llama-swap[32242]: [INFO] Request 127.0.0.1 "GET /running HTTP/1.1" 200 717 "python-httpx/0.28.1" 36.259µs Jul 05 22:40:10 …
```

_This report is generated by `orc bugreport` and is safe to share: config
values whose keys look secret-bearing are redacted before writing._
