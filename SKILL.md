---
name: flagyard-watcher
description: 'Watch a FlagYard event/lab for new challenges and auto-spawn a T3 Code solver thread (Cursor, Opus 4.6 high effort) per challenge, unattended. Depends on the flagyard-submit skill (FlagYard API + auth + Telegram reporting) and t3-manage (thread spawning). Use for "watch this CTF and auto-solve", "start the FlagYard watcher", or automating a live FlagYard event.'
argument-hint: 'Provide an event id (or a flagyard.com URL); --once to spawn for all unsolved and exit.'
user-invocable: true
---

# FlagYard Watcher

Polls a FlagYard event/lab's challenge list and spawns one autonomous T3 Code
thread per new (unsolved) challenge — download, solve, and report a writeup to
Telegram, all unattended. Threads do **not** submit flags (a human submits from
the real competition team).

## Dependencies

- **[flagyard-submit](https://github.com/RusiruSadathana/flagyard-submit)**
  skill installed at `~/.claude/skills/flagyard-submit/` — provides
  `flagyard_lib.py` (Cloudflare-safe HTTP + auth + Telegram reporting), the
  `get_attachment.py` / `instance.py` commands referenced in each solver
  thread's prompt, `telegram.py send-doc` (writeup upload), and the named
  `--account <name>` support the account pool leases against.
  This repo does not duplicate any of that — it imports `flagyard_lib` directly
  from the sibling skill's `scripts/` dir and shells out to its scripts.
  Its `curl_cffi` dependency must be installed on whatever Python interpreter
  runs `flagyard_watcher.py` (a venv, or `uv run --with curl_cffi`).
- **t3-manage** skill installed (spawns/monitors the T3 Code threads).
- T3 Code running locally (or `T3_TOKEN` set for remote dispatch — see
  t3-manage's own docs).

## Quick Start

```bash
# One-shot: spawn a solver thread for every currently-unsolved challenge, then exit
python3 scripts/flagyard_watcher.py --event-id <uuid> --once

# Continuous: poll every 15s for newly-released challenges
python3 scripts/flagyard_watcher.py --event-id <uuid>

# Preview only — see what would spawn without touching T3 at all
python3 scripts/flagyard_watcher.py --event-id <uuid> --once --dry-run
```

`--url "https://flagyard.com/dashboard/events/<uuid>/challenges"` works in
place of `--event-id`; `--lab-id <id>` targets a practice lab instead.

## Multi-account pool (parallel instances)

FlagYard allows **one running dynamic instance per account** — starting an
instance on an account kills that account's previous one. To solve several
instance-backed challenges at once, the watcher keeps a **pool of accounts**
(separate teams/users) and **leases a distinct account to each solver thread**.
Lease state is handled by `scripts/accounts.py`; each account is a flagyard-submit
*named account* (`~/.config/flagyard/accounts/<label>/`), and every FlagYard
command in a thread's prompt is pinned to it via the skill's `--account <label>`
flag — one clean seam between the skill and this watcher.

**Prerequisite:** each pool account/team must be **registered/joined to the
event** (so it can see challenges and start instances). Seed each one from its
browser's Keycloak token JSON (same source as `flagyard-submit`'s `auth.py`):

```bash
# Seed accounts (paste the token JSON — access_token + refresh_token — on stdin)
pbpaste | python3 scripts/accounts.py add --label web1 --team "Team A"
pbpaste | python3 scripts/accounts.py add --label web2 --team "Team B"
python3 scripts/accounts.py list           # pool + lease state + token expiries
python3 scripts/accounts.py refresh-all     # keep every access token warm (cron-friendly)
```

Once ≥1 account is seeded, the watcher **auto-enables** multi-account mode:
leases one per thread, **defers** a challenge (retries next poll) when the pool
is exhausted, and **releases** an account when its thread finishes (polled via
`t3-manage status`). Force the old single shared account with `--single-account`.

> **Pool vs. `instance_queue.py`:** the pool gives each thread its *own* account
> (best when you have ~1 account per concurrent instance challenge). The
> [instance queue](#instance-based-challenges-one-slot-per-flagyard-account) is
> the complementary fallback — time-share *one* account's single instance slot
> across several already-spawned threads when you have fewer accounts than
> instance-based challenges.

## Reporting: writeup-only (no auto-submit)

Solver threads **no longer submit flags**. They solve, write a Markdown writeup
(`writeup.md`: approach, steps, exploit/payload, and the flag), and **send it to
Telegram as a document** via `telegram.py send-doc`. A human submits the flag
from the real competition team. This keeps the worker accounts purely for
running instances / downloads and never touches the scoreboard.

## What each spawned thread gets

- Challenge title, category, points, id, full (HTML-stripped) description
- The `get_attachment.py` download command, if the challenge has files
- The `instance.py start`/`status`/`stop` commands, if it's a dynamic instance
- Its **dedicated account** pinned via `--account <label>` (multi-account mode)
- Instructions to solve, write `writeup.md`, and **send it to Telegram as a
  document** (`telegram.py send-doc`) — **not** to submit the flag
- Instructions to work autonomously, never spend points on hints, and try
  multiple approaches

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--url` / `--event-id` / `--lab-id` | — | Target (same flags as flagyard-submit's scripts) |
| `--poll-interval` | 15 | Seconds between polls |
| `--once` | false | Spawn for all currently unsolved, then exit |
| `--project` | `FlagYard - <event/lab id>` | T3 project name |
| `--instance` / `--model` | `cursor` / `claude-opus-4-6[effort=high]` | Provider/model for solver threads |
| `--seen-file` | `./seen_challenges_flagyard.txt` | Dedup tracking file |
| `--dry-run` | false | Show what would spawn, without dispatching |
| `--no-telegram` | false | Don't report new-challenge/spawn events to Telegram |
| `--single-account` | false | Force the single shared account (disable the multi-account pool) |

## Caution

Each spawned thread lands in one T3 project (`--project`), one thread per
challenge, running fully unattended — **spawning is a real, billed action
with no undo**. Confirm the project/model/expected challenge count with the
user before a long unattended run, especially before `--once` against a big
event (same caution the `t3-manage` skill itself calls out for batch spawns).

## Instance-based challenges: one slot per FlagYard account

FlagYard allows only **one running dynamic instance per account** — if
multiple solver threads on the same account work instance-based (networked)
challenges in parallel, whichever calls `instance.py --action start` most
recently silently kills every other thread's instance out from under it
(confirmed live during BlackHat MEA CTF 2026).

`scripts/instance_queue.py` handles this: point it at one FlagYard account
(see flagyard-submit's multi-account support, `auth.py --account`) and a list
of already-spawned solver threads, and it round-robins them through that
account's single instance slot — only the active entry's thread is resumed,
every other one stays interrupted so it can't race for the slot. Each turn
runs until the challenge is solved or a time budget elapses, then the
instance is stopped and the next entry becomes active.

Run **one instance of this script per account** to get one concurrent
instance-slot per account — e.g. three team members' accounts means three
instance-based challenges can genuinely run at once instead of fighting over
one slot:

```bash
python3 scripts/instance_queue.py --event-id <uuid> --account teammateA \
  --entry "ChallengeA:<threadId>:<challengeId>" \
  --entry "ChallengeB:<threadId>:<challengeId>" \
  --active ChallengeA --turn-minutes 20
```

| Flag | Default | Description |
|------|---------|-------------|
| `--event-id` / `--lab-id` | — | Target |
| `--account` | (default account) | Which FlagYard account's instance slot to manage |
| `--entry` | — | `label:thread_id:challenge_id`, repeatable |
| `--active` | — | Label that already holds the instance slot right now (skips the initial resume-message) |
| `--turn-minutes` | 20 | Max time before rotating to the next entry |
| `--poll-seconds` | 30 | How often to check solved-status / thread status during a turn |
| `--instance` / `--model` | `cursor` / `claude-opus-4-6[effort=high]` | Model used when resuming a thread |
| `--state-file` | `./instance_queue_state.json` | Persists queue position + solved set (resumable) |

## Install

```bash
git clone https://github.com/charindithjaindu/flagyard-watcher.git
cd flagyard-watcher
./install.sh
```
