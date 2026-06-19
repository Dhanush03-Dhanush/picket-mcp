# Picket

**An event-driven API watcher, exposed as an MCP server for Claude Code.**

Picket lets an interactive Claude Code session *arm* a long-lived watcher over an
HTTP endpoint and then walk away. Each watcher is a detached Python daemon that
polls the endpoint on a fixed cadence and evaluates a deterministic predicate.
When the predicate fires, the daemon launches a fresh headless `claude -p` that
runs **one** pre-registered runbook with the trigger payload, and then exits.

> **Core property — waiting is free.** No model runs while a watcher polls;
> predicate evaluation is plain Python. A Claude instance spins up *only* when a
> condition fires, does one job, and dies.

**Contents:** [What & why](#what-it-is-and-why) · [How it works](#how-it-works) ·
[Install](#install) · [Register](#register-with-claude-code) ·
[Quick start](#quick-start) · [Concepts](#concepts)
([watches](#watches--lifecycle) · [predicates](#predicates) ·
[cadence](#cadence) · [probes](#probes--scriptable-conditions) ·
[runbooks](#runbooks) · [fires](#fires--the-audit-trail) ·
[limits](#limits--gating) · [resilience](#resilience)) ·
[Tool reference](#tool-reference) · [Configuration](#configuration) ·
[Error codes](#error-codes) · [Security](#security) · [Testing](#testing) ·
[Project layout](#project-layout) · [Constraints & non-goals](#constraints--non-goals)

## What it is, and why

Some useful work is *gated on a condition that may not arrive for hours*: a price
crossing, a queue draining, a status flipping, a filing dropping. Keeping a chat
session open to wait is wasteful, and a cron job that wakes a full agent every
minute burns tokens doing nothing.

Picket separates the two halves of "wait, then act":

- **The wait** is cheap, deterministic Python (`httpx` fetch + JSONPath extract +
  a comparison). It runs in a detached daemon and costs nothing but a poll.
- **The act** is a single, scoped, headless Claude invocation that exists only for
  the duration of one runbook.

Picket is for **judgment** tasks — analyze, decide, summarize, notify — that are
worth spinning up a model for *once a condition holds*. It is **not** for
sub-second deterministic reactions.

**Motivating example:** *"Watch the SPX endpoint every 30s; when it drops 2% from
the prior close, run the `spx-options-analysis` runbook and notify me."* You arm
this once and close the session. Hours later, on the crossing, a scoped headless
handler runs the runbook unattended and records the fire — with full lifecycle
control (list / inspect / pause / resume / stop / stop-all) and a flat-file audit
trail.

## How it works

Three processes coordinate **only through a flat-file root** — the folder is the
source of truth, there is no database:

| Process | What it is | Role |
| --- | --- | --- |
| **Control plane** | the FastMCP stdio server (`picket.server`) | Fast request/response: arm / list / inspect / pause / stop / audit. Never polls, never hosts the wait. May die with the session — nothing is lost because state is on disk. |
| **Runtime** | one detached daemon per active watcher (`python -m picket.daemon <id>`) | double-fork + `setsid` so closing the session doesn't `SIGHUP` it. Polls, extracts, evaluates vs a persisted baseline (or runs a registered probe script), fires. Pure Python — no model in the loop. |
| **Handler** | an ephemeral headless `claude -p` (or a script, for `exec` runbooks) | Runs one runbook and exits. The daemon supervises it: timeout, output capture, retry, dead-letter. |

### On-disk layout

Root is `~/.claude/picket/` (override with `PICKET_HOME`), created on first use:

```
$PICKET_HOME/
  watches/<id>.json      watch state (daemon-owned; the server writes it once at arm)
  watches/<id>.control   server→daemon channel: stop | pause | resume
  runbooks/<id>/         human-placed runbook files + runbook.toml (registered by id)
  probes/<id>/           human-placed probe script + probe.toml (a custom condition)
  fires/<id>.jsonl       append-only fire records (the audit trail)
  logs/<id>.log          size-capped, rotating poll/debug log
  locks/<id>.lock        in-flight handler lock (at most one handler per watcher)
```

**Single-writer ownership rule:** after the daemon spawns it is the sole writer of
`watches/<id>.json`, `fires/<id>.jsonl`, and `logs/<id>.log`; the control plane
writes only `watches/<id>.control` (and the initial state file at arm time). All
state writes are atomic (temp file + `os.replace`), so a reader never sees a torn
file. (`stop_watch` is the one deliberate exception — it records the terminal
`stopped` status, since a killed daemon can't write its own.)

## Install

Requirements: POSIX (macOS/Linux — relies on `fork`/`setsid`), Python ≥ 3.11, the
`claude` CLI on `PATH`, and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
```

## Register with Claude Code

Picket is a stdio MCP server. Register the console script (user scope makes it
available in all your projects):

```sh
claude mcp add picket -s user /ABSOLUTE/PATH/picket-mcp/.venv/bin/picket
claude mcp list           # picket: ... - ✓ Connected
```

A portable alternative that doesn't hard-code the venv path:

```sh
claude mcp add picket -s user -- uv run --directory /ABSOLUTE/PATH/picket-mcp picket
```

MCP servers load at session start, so the tools appear in a **new** Claude Code
session. Remove with `claude mcp remove picket -s user`.

## Quick start

Drive these as MCP tool calls from a Claude Code session (shown here as
pseudocode). The flow is **register a runbook → dry-run the condition → arm**:

```text
# 1. Ship the built-in macOS notifier (an exec runbook), or register your own.
install_default_runbooks()                       # registers "picket-notify"

# Your own runbook: place files under ~/.claude/picket/runbooks/spx-analysis/
# (e.g. prompt.md), then register it by id — code is never passed as a parameter.
register_runbook(runbook_id="spx-analysis", runbook_type="prompt",
                 entry="prompt.md",
                 allowed_tools=["mcp__options-mcp__*", "Read"])

# 2. Dry-run the spec first: one fetch+extract+evaluate, no daemon, no state.
test_predicate(
    endpoint={"url": "https://api.example.com/spx", "auth_ref": "SPX_TOKEN"},
    predicate={"path": "$.last", "op": "pct_change", "value": -2,
               "baseline_mode": "prior_close", "baseline_path": "$.prev_close"})
# -> {"would_fire": false, "extracted_value": 5402.3, "response_excerpt": "...", ...}

# 3. Arm it and walk away.
arm_watch(
    runbook_id="spx-analysis",
    endpoint={"url": "https://api.example.com/spx", "auth_ref": "SPX_TOKEN"},
    predicate={"path": "$.last", "op": "pct_change", "value": -2,
               "baseline_mode": "prior_close", "baseline_path": "$.prev_close"},
    cadence={"interval_seconds": 30,
             "active_window": {"tz": "America/New_York",
                               "start": "09:30", "end": "16:00",
                               "days": [0, 1, 2, 3, 4]}},
    cooldown_seconds=3600, notify_runbook="picket-notify")
# -> {"ok": true, "watch_id": "wch_ab12cd34ef56", "status": "active",
#     "pid": 12345, "baseline": 5512.0, "trial_value": 5402.3}

# Later: inspect / control.
list_watches()                       # all watchers + liveness
get_watch(watch_id="wch_...")        # full state + most recent fire + log tail
get_fire_log(watch_id="wch_...")     # "did it fire, and what happened?"
stop_watch(watch_id="wch_...")       # graceful stop (idempotent)
```

**Probe variant** — when the condition needs real logic, swap the
endpoint+predicate for a registered script. Place
`probes/price-drop/probe.py` (it prints `{"fire": …, "value": …, "payload": …}`),
then arm with a `probe_id` instead:

```text
register_probe(probe_id="price-drop", language="python", entry="probe.py")
test_probe(probe_id="price-drop", probe_params={"symbol": "SPX", "floor": 5400})
arm_watch(runbook_id="spx-analysis", probe_id="price-drop",
          probe_params={"symbol": "SPX", "floor": 5400},
          cadence={"interval_seconds": 240}, max_fires=1)
```

## Concepts

### Watches & lifecycle

A **watch** is one endpoint + predicate + cadence + runbook, persisted as
`watches/<id>.json` (id prefix `wch_`). Status moves through:

- **active** — the daemon is polling.
- **paused** — the daemon is alive but not polling (baseline & history preserved).
- **stopped** — terminal; the daemon has exited.
- **errored** — *reported* (not stored) by `list_watches`/`get_watch` when a watch
  claims `active` but its recorded pid is gone or its heartbeat is stale. This is
  the v0 crash-recovery signal.

Liveness uses **verify-before-kill**: a watch is "alive" only if the recorded pid
is running *and* its `psutil` create-time still matches what was captured at spawn
(guards against PID reuse). `stop_watch`/`stop_all_watches` apply the same check
before signaling.

### Predicates

A predicate is `{path, op, value?, baseline_mode?, baseline_value?, baseline_path?}`.
`path` is a JSONPath (via `jsonpath-ng`, with a dotted-path fallback like
`a.b.0.c`). Values are coerced to the threshold's type; a non-numeric value where a
number is required is an *observe error* (never a fire).

| `op` | Fires when | Notes |
| --- | --- | --- |
| `on_change` | the extracted value differs from the baseline | baseline starts at the arm-time value, re-arms after each fire |
| `lt` `lte` `gt` `gte` `eq` `ne` | the comparison vs `value` holds | classic threshold |
| `crosses_above` / `crosses_below` | the value crosses `value` upward / downward | threshold check made a *crossing* by the edge model |
| `pct_change` | the signed % move from the baseline reaches `value` | `value=-2` → dropped ≥2%; `value=+2` → rose ≥2% |

**Edge / episode semantics.** A predicate fires on the **unsatisfied→satisfied
transition**, then **once per satisfied episode** — it will not re-fire while the
condition keeps holding. The episode resets when the condition goes false again.

**`pct_change` baselines** (`baseline_mode`):

| mode | baseline is… |
| --- | --- |
| `last_value` (default) | the previous poll's value (per-interval % change) |
| `arm_time` | the value observed when the watch was armed |
| `prior_close` | `baseline_path` extracted from the response at arm time |
| `absolute` | the fixed `baseline_value` you supply |

Any non-`last_value` baseline is **captured and persisted at arm time**, so a
daemon restart restores it rather than recomputing.

### Cadence

`{interval_seconds, jitter_seconds?, active_window?}`:

- **`interval_seconds`** — base poll period (> 0).
- **`jitter_seconds`** — random `[0, jitter)` added to each sleep, to avoid
  synchronized polling.
- **`active_window`** — `{tz, start "HH:MM", end "HH:MM", days [0..6]}` (Mon=0).
  Outside the window the daemon stays alive but skips polling (no stale
  after-hours evaluation). Windows may wrap past midnight (`start` > `end`).

> Picket polls; it cannot see a sub-interval crossing, and a daemon that is down
> cannot observe one. Choose `interval_seconds` accordingly.

### Probes — scriptable conditions

A predicate covers "fetch JSON, extract a field, compare it." When the condition
is more than that — multiple endpoints, a computation, custom auth, a non-JSON
source — arm the watch with a **probe** instead. A probe is a registered script
the daemon runs **in place of** the endpoint+predicate, on the same cadence. A
watch has **exactly one** condition source: an `endpoint`+`predicate` **or** a
`probe_id` — never both, never neither.

Like a runbook, a probe lives under `probes/<id>/`, is referenced **by id** (its
code is never a tool parameter), and is content-hashed + drift-checked. It is
**`python`** or **`sh`** (declared in the manifest) and prints **one JSON object
on its last stdout line**:

```json
{"fire": true, "value": 5402.3, "payload": {"symbol": "SPX"}}
```

- **`fire`** (bool, the only required field) — `true` is the satisfied signal,
  fed into the *same* edge / debounce / cooldown / once-per-episode gating a
  predicate uses (it fires on the unsatisfied→satisfied transition, once per
  episode).
- **`value`** — recorded as `last_value` and handed to the next run as
  `PICKET_LAST_VALUE` (one-tick memory for "changed since last time" logic).
- **`payload`** — merged into the trigger payload delivered to the runbook.

**Exit code is the error channel.** Exit `0` means *evaluated*. A non-zero exit,
a timeout (30s), or unparseable stdout is a **probe-error**: logged as
`last_error` and **never a fire** — exactly like an endpoint observe-error.

**Inputs.** At arm time, `probe_params` (a JSON object) reaches the script as the
`PICKET_PARAMS` env var **and** a `PICKET_PARAMS_FILE` path; the script also gets
`PICKET_LAST_VALUE` and `PICKET_WATCH_ID`. Secrets follow the endpoint model —
reference an **env-var name**, never a literal; the daemon inherits the arming
session's environment. One registered probe is reusable across watches with
different `probe_params`.

`probe.toml` (written by Picket):

```toml
id = "price-drop"
language = "python"
entry = "probe.py"
description = "..."
content_hash = "sha256:…"
version = 1
```

> A probe is arbitrary code Picket runs unattended on a timer, so it carries the
> same trust weight as a runbook: a human places the files, and the fire-time
> drift check (`drift_policy`, default `block`) refuses a probe whose entry
> changed since registration.

### Runbooks

A runbook is the **unit of approved work**. It lives under `runbooks/<id>/` and is
referenced **by id — its code is never passed as a tool parameter**. Two types:

- **`prompt`** — an agentic `claude -p` job (the default). `entry` is a prompt
  file; `allowed_tools` is its tool allowlist.
- **`exec`** — a script run directly: **no LLM, no tokens**. (The shipped
  `picket-notify` macOS notifier is an exec runbook.)

`register_runbook` references files a human already placed, validates the `entry`
path resolves **inside** the runbook dir, and records a `content_hash` over the
entry (+ an optional `scripts/` dir). `runbook.toml` (written by Picket) holds:

```toml
id = "spx-analysis"
type = "prompt"
entry = "prompt.md"
description = "..."
allowed_tools = ["mcp__options-mcp__*", "Read"]
content_hash = "sha256:…"
version = 1
```

**Payload.** At fire time the trigger payload is delivered three ways: the
`PICKET_PAYLOAD_FILE` env var (path to a JSON temp file), the `PICKET_PAYLOAD` env
var (the same JSON), and — for `prompt` runbooks — rendered inline into the prompt.
Its shape:

```json
{"watch_id": "wch_…", "label": "...", "runbook_id": "spx-analysis",
 "fired_at": "2026-…Z", "value": 5402.3, "baseline": 5512.0,
 "predicate": {…}, "endpoint_url": "https://…"}
```

### Fires & the audit trail

Every fire attempt appends a record to `fires/<id>.jsonl`:

```json
{"fire_id": "fire_…", "watch_id": "wch_…", "runbook_id": "…",
 "status": "completed", "started_at": "…", "ended_at": "…",
 "exit_code": 0, "error": null, "handler_pid": 4242,
 "duration_ms": 1837, "transcript_tail": "…"}
```

Fire **statuses**: `completed`, `failed`, `timed_out` (exceeded the handler
timeout — process group killed), `skipped_overlap` (a crossing arrived while a
handler held the in-flight lock; dropped), and `dead_lettered` (failed every retry).
`get_fire_log` reads these; `tail_watch_log` shows the daemon's poll/debug log
(observed values, observe-errors, FIRE markers) to answer *"is it even
observing?"*.

### Limits & gating

Set on `arm_watch` (all optional):

| field | effect |
| --- | --- |
| `debounce_seconds` | the condition must hold this long before firing |
| `cooldown_seconds` | minimum gap between fires (damps oscillation around a threshold) |
| `max_fires` | self-stop after the Nth fire |
| `ttl_seconds` | self-stop after this wall-clock lifetime |
| `overlap_policy` | `drop` only — a fire while a handler is in flight is recorded `skipped_overlap` |

Handlers are additionally bounded by a **600s timeout** (the process group is
killed and the fire recorded `timed_out`) and by `--max-turns` on the prompt
handler. There is intentionally **no** `--max-budget-usd`.

### Resilience

- **Retry → dead-letter.** With `max_retries=N`, a failing/timing-out handler is
  retried with exponential backoff; after `N+1` failures the fire is recorded
  `dead_lettered` (and the `notify_runbook`, if set, is fired). With the default
  `max_retries=0`, a single failure is just `failed`.
- **Fire-time drift.** Before each run the entry is re-hashed and compared to the
  value stored at registration. Default `drift_policy="block"` refuses and logs a
  `RUNBOOK_DRIFT` failed fire; `"run"` runs anyway and records it.
- **Crash recovery.** A dead/stale daemon surfaces as `errored` (see
  [lifecycle](#watches--lifecycle)).
- **Bulk stop.** `stop_all_watches(confirm=true)` stops everything matching a
  `status_filter`.

## Tool reference

All tools return either `{"ok": true, …}` or the failure envelope
`{"ok": false, "error_code": "…", "message": "…"}` (see [error codes](#error-codes)).

**Health**
- `ping()` → `{ok, service, version}`.

**Runbooks**
- `register_runbook(runbook_id, runbook_type, entry, description="", allowed_tools=None, version=1)`
  — register files placed under `runbooks/<id>/`; computes `content_hash`. Never
  accepts code. → the runbook record.
- `list_runbooks()` → `{ok, runbooks:[{runbook_id, type, entry, description, declared_tools, content_hash, version}]}`.
- `install_default_runbooks()` — ship + register the macOS notifier `picket-notify`.

**Probes**
- `register_probe(probe_id, language, entry, description="", version=1)` — register a
  condition script placed under `probes/<id>/`; computes `content_hash`. Never accepts
  code. → the probe record.
- `list_probes()` → `{ok, probes:[{probe_id, language, entry, description, content_hash, version}]}`.

**Dry run**
- `test_predicate(endpoint, predicate)` — one fetch+extract+evaluate, **no daemon,
  no state**. → `{ok, would_fire, extracted_value, response_excerpt, extract_error}`.
- `test_probe(probe_id, probe_params=None)` — one probe execution, **no daemon, no
  state**. → `{ok, would_fire, value, payload, error}`.

**Lifecycle**
- `arm_watch(runbook_id, cadence, endpoint=None, predicate=None, probe_id=None,
  probe_params=None, label=None, max_fires=None, ttl_seconds=None,
  debounce_seconds=0, cooldown_seconds=0, max_retries=0, drift_policy="block",
  notify_runbook=None, skip_permissions=False, confirm_skip=False)` — provide
  **exactly one** condition source (`endpoint`+`predicate` **or** `probe_id`).
  Validate, do a trial observation / probe-run (captures the baseline), persist
  state, spawn the detached daemon, read back its identity.
  → `{ok, watch_id, status, pid, pgid, baseline, trial_value}`.
- `list_watches(status_filter="all")` — `all|active|paused|stopped|errored`; each
  row has `{watch_id, label, status, runbook_id, cadence_summary, fire_count,
  last_observed_at, last_error, alive}`.
- `get_watch(watch_id, log_lines=20)` → `{ok, watch, alive, effective_status,
  most_recent_fire, log_tail}`.
- `pause_watch(watch_id)` / `resume_watch(watch_id)` — halt / restart polling
  (daemon stays alive; baseline preserved).
- `stop_watch(watch_id, mode="graceful")` — `graceful` (control file) or
  `immediate` (`SIGTERM` the group after verify-before-kill). Idempotent: a second
  call → `ALREADY_STOPPED`. → `{ok, final_status, handler_was_in_flight}`.
- `stop_all_watches(confirm=false, status_filter="active", mode="graceful")` —
  requires `confirm=true` (else `PERMISSION_REQUIRED`). → `{ok, stopped_count,
  watch_ids, failures}`.

**Audit**
- `get_fire_log(watch_id=None, limit=20)` — recent fires, across all watchers if
  `watch_id` is omitted, newest first.
- `tail_watch_log(watch_id, lines=50)` — recent poll/debug lines for one watch.

## Configuration

- **`PICKET_HOME`** — the on-disk root (default `~/.claude/picket`). Set it to an
  isolated directory for experiments or tests; everything Picket creates lives
  under it.
- **Secrets via `auth_ref`** — `endpoint.auth_ref` names an **environment
  variable** holding a bearer token; it is read at fetch time and sent as
  `Authorization: Bearer <value>`. The literal credential is never written to any
  state file, payload, or parameter — only the variable *name* is persisted. The
  daemon inherits the environment of the session that armed it.

## Error codes

| code | meaning |
| --- | --- |
| `INVALID_SPEC` | a spec (endpoint/predicate/cadence/runbook args) failed validation |
| `RUNBOOK_NOT_FOUND` | `arm_watch` referenced an unregistered runbook |
| `RUNBOOK_DRIFT` | the entry changed since registration (drift `block` policy) |
| `ENDPOINT_UNREACHABLE` | the arm-time trial fetch/extract failed |
| `PROBE_NOT_FOUND` | `arm_watch` referenced an unregistered probe |
| `PROBE_FAILED` | the arm-time trial probe run errored (probe analog of `ENDPOINT_UNREACHABLE`) |
| `PROBE_DRIFT` | the probe entry changed since registration (drift `block` policy) |
| `DAEMON_SPAWN_FAILED` | the daemon didn't start or never reported its identity |
| `NOT_FOUND` | no such watch |
| `ALREADY_STOPPED` | `stop_watch` on an already-stopped watch (idempotent) |
| `PERMISSION_REQUIRED` | a guarded action without its confirmation (`stop_all`, skip-permissions) |

## Security

Picket scopes tools and is **deny-by-default**; it is **not** an execution-safety
layer — the irreversible boundary is defended *inside the runbook*.

- **Scoped by default.** Prompt handlers run `claude -p … --permission-mode dontAsk
  --allowedTools <runbook.allowed_tools>`: non-interactive **and** deny-by-default,
  so a tool that isn't allowlisted is refused, never awaited.
- **Secret refs.** See [Configuration](#configuration) — only the env-var name is
  ever persisted.
- **Drift check & bounded handlers.** See [Resilience](#resilience) and
  [Limits](#limits--gating).

### High-stakes: skip-permissions

For consciously-trusted runbooks, `arm_watch(skip_permissions=true,
confirm_skip=true)` launches with `--dangerously-skip-permissions`. `confirm_skip`
is required (else `PERMISSION_REQUIRED`) and the opt-in is recorded on the state
file. Guardrails are applied with **`--disallowedTools`** (`Bash(rm:*)`,
`Bash(curl:*)`, `Bash(sudo:*)`) — **not** `--allowedTools`, which is ignored under
`bypassPermissions`.

> **First-run precondition.** The *first ever* `--dangerously-skip-permissions`
> invocation on a machine shows a one-time interactive acceptance that a detached
> daemon cannot click. Run it once interactively to clear that prompt **before**
> any unattended use.

**Headless safety-termination.** Claude's classifier may terminate a `-p` process
after repeated dangerous-action denials. Picket treats that as a handler failure:
it flows through retry → dead-letter like any other failure.

### Trade-execution runbook contract

Picket reduces but does **not** eliminate duplicate fires (the in-flight lock,
`cooldown_seconds`, and fire-once edge semantics narrow the window; a crash between
launch and record, or two daemons during recovery, can still double-fire).
Therefore any runbook that places a trade **must**, inside the runbook:

1. Carry an **idempotency key** derived from the fire (e.g. `watch_id` +
   `fired_at`) and refuse to act twice on the same key.
2. Gate the irreversible action behind a **hard confirmation** (a broker-side
   idempotent order, a balance/limit precondition, or an explicit approval),
   never on Picket's delivery alone.

## Testing

```sh
uv run pytest -q              # hermetic unit suite (network + claude launch mocked)
uv run pytest -m smoke        # opt-in real-process suite; no tokens
uv run pytest -m claude_smoke # opt-in; fires a real, minimal claude -p (spends tokens)
```

The two **opt-in** suites exercise the real seams the unit tests mock — a real
detached daemon polling a real HTTP server and firing a real handler. Both
self-limit (`max_fires=1`) and self-clean (a temp `PICKET_HOME` and a teardown that
stops and reaps every daemon). `-m smoke` covers the conditional predicates
(`pct_change`, `crosses_above`, `on_change`), a **probe-driven watch** (a real
daemon running a real probe script that fires once), and a live **public-API monitor**
(Coinbase BTC spot — the SPX example shape against a real, no-auth endpoint;
skipped if unreachable). `-m claude_smoke` skips cleanly when `claude` isn't on
`PATH`. Both are deselected from the default run.

Lint/format: `uv run ruff check . && uv run ruff format --check .`

## Project layout

```
src/picket/
  server.py     FastMCP server — thin tool adapters over the modules below
  models.py     Pydantic specs: EndpointSpec / PredicateSpec / CadenceSpec / WatchState
  store.py      PICKET_HOME, paths, atomic JSON, JSONL, rotating logs, control channel
  condition.py  fetch (httpx + auth_ref) · extract (jsonpath) · is_satisfied · baselines
  runbooks.py   runbook.toml, register/list, content_hash, payload + invocation dispatch
  probes.py     probe.toml, register/list, run (parse {fire,value,payload}), drift, dry-run
  handler.py    launch (scoped claude -p / exec), supervise, drift, retry, fire records
  daemon.py     python -m picket.daemon: detach, poll loop, gating, control, limits
  watches.py    arm / list / get / stop / pause / resume / stop_all, verify-before-kill
  audit.py      get_fire_log / tail_watch_log
  errors.py     ErrorCode enum + failure envelope
```

Server tools are intentionally thin: each delegates to a plain function in a
`picket.*` module that is unit-tested directly.

## Constraints & non-goals

Personal, single-user, local use. POSIX-only (relies on `fork`/`setsid`); flat
JSON files, no database; stdio transport. **Non-goals:** not a low-latency path;
not an execution-safety layer (idempotency/confirmation live in the runbook); not
a workflow/DAG engine (one predicate, one runbook); does not author runbooks; not
a zero-missed-event guarantee.
