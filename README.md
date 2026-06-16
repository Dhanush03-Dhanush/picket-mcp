# Picket

**Event-driven API watcher MCP server for Claude Code.** Arm a long-lived watcher
over an API endpoint from an interactive session, then walk away. Each watcher is a
detached Python daemon that polls on a fixed cadence and evaluates a deterministic
predicate. When the predicate fires, the daemon launches a fresh headless
`claude -p` that runs **one** pre-registered runbook with the trigger payload, then
exits.

> **Core property — waiting is free.** No model runs while a watcher polls;
> predicate evaluation is plain Python. A Claude instance spins up only when a
> condition fires, does one job, and dies. Picket is for *judgment* tasks
> (analyze, decide, summarize, notify), never sub-second deterministic reaction.

Motivating example: *"Watch the SPX endpoint every 30s; when it drops 2% from
prior close, run the `spx-options-analysis` runbook and notify me."* Arm it once,
close the session; hours later, on the crossing, a scoped headless handler runs
the runbook unattended and records the fire.

## Architecture

Three processes coordinate only through a flat-file root — the folder is the
source of truth, no DB:

- **Control plane** — the FastMCP stdio server (`picket.server`). Fast
  request/response only: arm / list / inspect / pause / stop and the audit log.
  Never polls, never hosts the wait. May die with the session; nothing is lost.
- **Runtime** — one detached daemon per active watcher
  (`python -m picket.daemon <id>`, double-fork + setsid). Polls, extracts a
  field, evaluates the predicate vs a persisted baseline, fires. Pure Python.
- **Handler** — an ephemeral headless `claude -p`, scoped to an explicit tool
  allowlist, runs one runbook and exits (or runs a script directly for `exec`
  runbooks). The daemon supervises it (timeout, capture, retry, dead-letter).

On-disk root: `~/.claude/picket/` (override with `PICKET_HOME`) with
`watches/ runbooks/ fires/ logs/ locks/`.

## Requirements

POSIX (macOS/Linux — relies on `fork`/`setsid`), Python ≥ 3.11, the `claude` CLI
on `PATH`, and [uv](https://docs.astral.sh/uv/).

## Install

```sh
uv sync
```

Register it with Claude Code as an MCP server (stdio):

```sh
claude mcp add picket -- uv run --directory /path/to/picket-mcp picket
```

## Quick start

```text
# 1. Place a runbook under ~/.claude/picket/runbooks/<id>/ and register it.
install_default_runbooks()                       # ships picket-notify (macOS)
register_runbook(runbook_id="spx-analysis",
                 runbook_type="prompt", entry="prompt.md",
                 allowed_tools=["mcp__options-mcp__*", "Read"])

# 2. Dry-run the condition before arming.
test_predicate(endpoint={"url": "https://api.example.com/spx",
                         "auth_ref": "SPX_TOKEN"},
               predicate={"path": "$.last", "op": "pct_change", "value": -2,
                          "baseline_mode": "prior_close",
                          "baseline_path": "$.prev_close"})

# 3. Arm it and walk away.
arm_watch(runbook_id="spx-analysis",
          endpoint={"url": "https://api.example.com/spx", "auth_ref": "SPX_TOKEN"},
          predicate={"path": "$.last", "op": "pct_change", "value": -2,
                     "baseline_mode": "prior_close", "baseline_path": "$.prev_close"},
          cadence={"interval_seconds": 30,
                   "active_window": {"tz": "America/New_York",
                                     "start": "09:30", "end": "16:00",
                                     "days": [0, 1, 2, 3, 4]}},
          cooldown_seconds=3600, notify_runbook="picket-notify")
```

## Tools

| Tool | Purpose |
| --- | --- |
| `ping` | Health check. |
| `test_predicate` | Dry-run a spec (one fetch+extract+evaluate, no daemon, no state). |
| `register_runbook` / `list_runbooks` | Register (by id, never code) and list runbooks. |
| `install_default_runbooks` | Install the shipped `picket-notify` exec runbook. |
| `arm_watch` | Validate, trial-observe, persist baseline, spawn the daemon. |
| `list_watches` / `get_watch` | Inspect watchers (with liveness; `errored` for dead daemons). |
| `pause_watch` / `resume_watch` | Halt/restart polling without recomputing the baseline. |
| `stop_watch` / `stop_all_watches` | Verify-before-kill stop (`stop_all` needs `confirm=true`). |
| `get_fire_log` / `tail_watch_log` | "Did it fire and what happened?" / "Is it even observing?" |

## Predicates & cadence

Predicates: `on_change`, `lt/gt/lte/gte/eq/ne`, `crosses_above/crosses_below`
(edge-fired), and `pct_change` (signed % threshold) with `baseline_mode` of
`last_value | arm_time | prior_close | absolute`. Non-`last_value` baselines are
captured **and persisted at arm time**, so a restart restores rather than
recomputes. Fires are edge-triggered (once per satisfied episode) with optional
`debounce_seconds`, `cooldown_seconds`, `max_fires` and `ttl_seconds`. Cadence
supports an `active_window` (tz / hours / weekdays) and `jitter_seconds`.

## Runbooks

A runbook lives under `runbooks/<id>/` and is referenced **by id, never supplied
as code**. Two types: `prompt` (agentic `claude -p`, default) and `exec` (a
script run directly — no LLM, no tokens). `register_runbook` validates the entry
path is inside the runbook dir and records a `content_hash` over the entry (+ a
`scripts/` dir). At fire time the trigger payload is delivered three ways:
`PICKET_PAYLOAD_FILE`, the `PICKET_PAYLOAD` env var, and (for prompt runbooks)
inline in the prompt.

## Security

Picket scopes tools and is **deny-by-default**; it is **not** an
execution-safety layer — the irreversible boundary is defended *inside the
runbook*.

- **Scoped by default.** Prompt handlers run `claude -p ... --permission-mode
  dontAsk --allowedTools <runbook.allowed_tools>`: non-interactive **and**
  deny-by-default, so a tool that isn't allowlisted is refused, not awaited.
- **Secret refs.** `auth_ref` names an **environment variable**, resolved at
  fetch time. The literal credential never touches any state file, payload, or
  parameter — only the variable name is persisted.
- **Fire-time drift check.** The entry is re-hashed before each run; the default
  policy blocks and logs a `RUNBOOK_DRIFT` failed fire (set `drift_policy="run"`
  to run-and-record instead).
- **Bounded handlers.** `--max-turns` bounds runaways (there is intentionally no
  `--max-budget-usd`). Timeouts kill the whole process group and record
  `timed_out`.

### High-stakes: skip-permissions

For consciously-trusted runbooks, `arm_watch(skip_permissions=true,
confirm_skip=true)` launches with `--dangerously-skip-permissions`. `confirm_skip`
is required (else `PERMISSION_REQUIRED`) and the opt-in is recorded on the state
file. Guardrails are applied with **`--disallowedTools`** (e.g. `Bash(rm:*)`,
`Bash(curl:*)`, `Bash(sudo:*)`) — **not** `--allowedTools`, which is ignored under
`bypassPermissions`.

> **First-run precondition.** The *first ever* `--dangerously-skip-permissions`
> invocation on a machine shows a one-time interactive acceptance that a detached
> daemon cannot click. The owner must run it once interactively to clear that
> prompt **before** any unattended use.

**Headless safety-termination.** Claude's classifier may terminate a `-p` process
after repeated dangerous-action denials. Picket treats that as a handler failure:
it flows through retry → dead-letter like any other failure.

### Trade-execution runbook contract

Picket reduces but does **not** eliminate duplicate fires (the in-flight lock,
`cooldown_seconds`, and fire-once edge semantics narrow the window; a crash
between launch and record, or two daemons during recovery, can still double-fire).
Therefore any runbook that places a trade **must**, inside the runbook:

1. Carry an **idempotency key** derived from the fire (e.g. `watch_id` +
   `fired_at`) and refuse to act twice on the same key.
2. Gate the irreversible action behind a **hard confirmation** check (a
   broker-side idempotent order, a balance/limit precondition, or an explicit
   approval), never on Picket's delivery alone.

## Smoke tests

The default `pytest` run is hermetic (mocks the network and the `claude` launch).
Two **opt-in** suites exercise the real seams; both self-limit (`max_fires=1`, so
each watcher fires once and self-stops) and self-clean (temp `PICKET_HOME`,
daemons reaped on teardown):

```sh
uv run pytest -m smoke          # real detached daemons + exec handlers; no tokens
uv run pytest -m claude_smoke   # also fires a real, minimal `claude -p` (spends tokens)
```

`-m smoke` covers the conditional predicates (`pct_change`, `crosses_above`,
`on_change`) and a live **public-API monitor** (Coinbase BTC spot — the SPX
example shape against a real, no-auth endpoint; skipped if unreachable).
`-m claude_smoke` skips cleanly when the `claude` CLI isn't on `PATH`.

To prove it end-to-end by hand against a live, side-effecting daemon, run this in
a throwaway root:

```sh
export PICKET_HOME="$PWD/.picket-home"

# A trivial exec runbook that just records that it fired.
mkdir -p "$PICKET_HOME/runbooks/echo"
printf '#!/bin/sh\necho "$PICKET_PAYLOAD" >> "$PICKET_HOME/fired.txt"\n' \
  > "$PICKET_HOME/runbooks/echo/run.sh"
chmod +x "$PICKET_HOME/runbooks/echo/run.sh"

# Arm a watcher against a public endpoint with a condition that is already true,
# so the daemon fires on its first poll. Drive the tools from a Claude Code
# session with the picket MCP server registered, then:
#   tail -f "$PICKET_HOME/logs/"*.log     # watch it observe
#   cat "$PICKET_HOME/fired.txt"          # confirm the handler ran
#   get_fire_log()                        # confirm the recorded fire
```

## Development

```sh
uv run ruff check . && uv run ruff format --check .
uv run pytest -q              # hermetic unit suite
uv run pytest -m smoke        # opt-in real-process smoke suite (no tokens)
uv run pytest -m claude_smoke # opt-in; fires a real claude -p (spends tokens)
```
