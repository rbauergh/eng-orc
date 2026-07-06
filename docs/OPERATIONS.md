# Operations

The day-to-day runbook, plus the Windows/WSL2 machine setup.

## Windows-side setup (once)

1. **NVIDIA driver on Windows only** (Game Ready or Studio, R495+). Never
   install a Linux GPU driver inside WSL — Windows projects the driver into
   WSL at `/usr/lib/wsl/lib` automatically. (The recurring
   `libcuda.so.1 is not a symbolic link` ldconfig warning during apt upgrades
   is benign; keep WSL current with `wsl --update`.)
2. **`.wslconfig`**: copy `server/wslconfig.sample` to
   `C:\Users\<you>\.wslconfig` and size `memory=` per the comments
   (24 GB on a 32 GB host). Then `wsl --shutdown`, wait ~8 s, relaunch.
3. **systemd** in WSL: ensure `/etc/wsl.conf` contains
   `[boot]`/`systemd=true` (Ubuntu 24.04 ships it enabled), restart WSL.
4. **Autostart on login** (optional): Task Scheduler → new task, trigger
   *At log on*, action `wsl.exe -d Ubuntu --exec /bin/true`. Booting the
   distro is enough — systemd + `loginctl enable-linger` (set up by the
   installer) bring llama-swap up, and Docker Desktop restarts Letta.

## Install / update (inside WSL)

```bash
git clone https://github.com/rbauergh/eng-orc && cd eng-orc
scripts/setup_wsl.sh --profile balanced-12gb        # ~25 GB of downloads
source .venv/bin/activate
orc doctor
```

Re-run the installer any time — it is idempotent and skips finished work.

**Updating after a `git pull` — always the same three commands:**

```bash
.venv/bin/pip install -e ".[dev]"   # new code
orc sync                            # live server config + model settings ← repo profile,
                                    # llama-swap restarted; your other tuning survives
orc selftest                        # prove it
```

`orc doctor`'s "llama-swap config sync" row tells you whenever the live
config has drifted from the repo profile; `orc sync` is always the fix.

## Daily driving

```bash
orc new "Build a small FastAPI service that ..."     # greenfield
orc new "Add rate limiting to my API" --repo ~/code/myapi   # existing repo
orc run --watch          # work everything; Ctrl-C any time — state is on disk
orc run --watch -i       # …and answer questions inline the moment work parks
orc status               # all projects: phase, plan progress, waiting gates
orc status <slug>        # deep dive: plan table, open questions, activity
orc inbox                # interactive Q&A session (no ids; --list for plain output)
orc answer "use sqlite, keep it simple"          # answers the oldest open gate
orc ask <slug> "also make sure it handles unicode"  # amend a mission mid-flight
orc pause <slug> / orc resume <slug> / orc abandon <slug>
orc report <slug>        # the wrap-up report
```

Watching it work:
- `orc dashboard` (second terminal) — top-style live view: profile, GPU
  utilization, the **gpu timeline** (which model is resident and for how
  long, load/unload history with durations, live token flow while
  generating), per-project status, the in-flight attempt with its turn
  counter, and an activity feed with review findings and tester/implementer
  handoff summaries inlined. `--details` expands every finding; `--once`
  prints a single snapshot.
- `orc run --watch` narrates phases, attempt starts (role/model/attempt
  count), reviewer verdicts with their blocking findings, verify failures,
  and finished attempts' handoff summaries; add `--verbose` for
  tool-by-tool turn lines.
- `orc models` shows the configured roles plus current residency and the
  recent gpu timeline.

Habits that pay off:
- **Edit `plan.yaml` freely between runs** — reprioritize, drop items, tighten
  acceptance criteria; the graph is validated on load.
- **Feed the memory**: `orc memory add "prefer httpx over requests" --kind convention`
  gets injected into future briefs on every project.
- **Watch a build**: `orc run <slug> --once` runs a single phase step —
  perfect for supervising early projects; artifacts land under
  `~/.eng-orc/projects/<slug>/artifacts/attempts/<item>/` (full transcripts).
- The GPU is yours when you want it: `orc models --unload` frees VRAM
  instantly (in-flight step finishes first).

## Servers

```bash
scripts/start_stack.sh / scripts/stop_stack.sh
systemctl --user status llama-swap
journalctl --user -u llama-swap -f          # server logs, live
curl -s http://127.0.0.1:9292/running       # what's resident on the GPU
# llama-swap web UI (playground, live logs, manual load/unload):
#   http://127.0.0.1:9292/ui
cd server/letta && docker compose logs -f   # letta
```

Letta notes: first boot runs DB migrations (~2 min) — `orc doctor` shows
memory as local-only until it's healthy, and eng-orc keeps working
regardless (writes queue; `orc memory sync` reconciles). After the first
successful pull, pin `LETTA_TAG` in `server/letta/.env` — the self-hosted
image is maintenance-mode upstream and `latest` has broken before.

## Sending diagnostics back (remote debugging loop)

When something misbehaves on this machine and the person debugging works
elsewhere:

```bash
orc bugreport --push
```

writes `orc-report.md` — doctor results, versions, config with secrets
redacted, recent per-project journal errors, and the llama-swap log tail —
then commits and pushes **just that file** from the current repo checkout.
The other side pulls and reads. Without `--push` it only writes the file.

## Resuming after weeks away

Nothing special: `orc status` to see where things stand, `orc run`. Briefs
are rebuilt from disk (mission, charter, plan state, prior attempts,
decisions), so a fresh effort starts with everything that matters and
nothing that doesn't. `orc resume <slug>` reactivates paused or wrapped
projects (a wrapped project re-enters at build with its existing plan —
add new work items via `orc ask` + a planner pass, or edit plan.yaml).

## Multiple machines

The orchestrator state lives in `~/.eng-orc`; the code in this repo. To move
a project between machines, copy its directory under `~/.eng-orc/projects/`
— it is fully self-contained (attached external workrooms move separately).
