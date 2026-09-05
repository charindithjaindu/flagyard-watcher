#!/usr/bin/env python3
"""FlagYard Watcher — poll an event/lab for new challenges and auto-spawn
T3 Code solver threads for each one.

Depends on two sibling skills being installed:
  - flagyard-submit (https://github.com/RusiruSadathana/flagyard-submit) —
    provides flagyard_lib.py (auth, HTTP, Telegram reporting) and the
    get_attachment.py / instance.py / submit_flag.py commands baked into
    each solver thread's prompt.
  - t3-manage — used to actually spawn/dispatch the T3 Code threads.

Auth is already handled by flagyard-submit (saved Bearer token + refresh
token, see its auth.py) — no login step needed here.

Usage:
    # T3 Code must be running; T3_TOKEN is only needed if the app can't
    # auto-mint one for local dispatch (see the t3-manage skill).
    python3 flagyard_watcher.py --event-id <uuid>
    python3 flagyard_watcher.py --url "https://flagyard.com/dashboard/events/<uuid>/challenges"

    # One-shot: spawn a thread for every currently unsolved challenge, then exit
    python3 flagyard_watcher.py --event-id <uuid> --once

    # See what would be spawned without touching T3 at all
    python3 flagyard_watcher.py --event-id <uuid> --once --dry-run
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

FLAGYARD_SKILL_CANDIDATES = [
    Path.home() / ".claude" / "skills" / "flagyard-submit" / "scripts",
    Path(__file__).resolve().parent.parent / "vendor" / "flagyard-submit" / "scripts",
]


def find_flagyard_scripts():
    for p in FLAGYARD_SKILL_CANDIDATES:
        if (p / "flagyard_lib.py").exists():
            return p
    return None


FLAGYARD_SCRIPTS = find_flagyard_scripts()
if FLAGYARD_SCRIPTS is None:
    print(
        "ERROR: flagyard-submit skill not found (looked for flagyard_lib.py under "
        + ", ".join(str(p) for p in FLAGYARD_SKILL_CANDIDATES) + ").\n"
        "Install it: https://github.com/RusiruSadathana/flagyard-submit",
        file=sys.stderr,
    )
    sys.exit(1)
sys.path.insert(0, str(FLAGYARD_SCRIPTS))
import flagyard_lib as lib  # noqa: E402  (path must be set up first)

ACCOUNTS_PY = Path(__file__).resolve().parent / "accounts.py"

DEFAULT_POLL_INTERVAL = 15
# Cursor's proxy driver, Opus 4.6 at high reasoning effort.
DEFAULT_INSTANCE = "cursor"
DEFAULT_MODEL = "claude-opus-4-6[effort=high]"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] {msg}", flush=True)


def find_t3_manage():
    candidates = [
        Path.home() / ".claude" / "skills" / "t3-manage" / "scripts" / "t3_manage.py",
        Path.home() / ".claude" / "skills" / "t3-code" / "t3-manage" / "scripts" / "t3_manage.py",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    import shutil
    return shutil.which("t3_manage.py")


# ---------------------------------------------------------------------------
# Multi-account pool (delegates all state to the sibling accounts.py)
# ---------------------------------------------------------------------------
def _accounts_cmd(*args):
    return subprocess.run(["python3", str(ACCOUNTS_PY), *args],
                          capture_output=True, text=True)


def accounts_list() -> list:
    """Return the configured account entries (empty list => single-account)."""
    r = _accounts_cmd("list", "--json")
    if r.returncode == 0:
        try:
            return json.loads(r.stdout).get("accounts", [])
        except Exception:
            return []
    return []


def lease_account():
    """Atomically lease a free account. Returns {'label','dir',...} or None."""
    r = _accounts_cmd("lease", "--json", "--thread", "pending")
    if r.returncode == 0:
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            return None
    return None  # rc 3 => no free account


def bind_account(label: str, thread_id: str) -> None:
    _accounts_cmd("bind", "--label", label, "--thread", thread_id)


def release_account(label: str = None, thread: str = None) -> None:
    args = ["release"]
    if label:
        args += ["--label", label]
    if thread:
        args += ["--thread", thread]
    _accounts_cmd(*args)


def t3_status(t3_manage: str, thread_id: str):
    """Return a thread's status string (or None if it can't be read)."""
    r = subprocess.run(["python3", t3_manage, "status", "--thread", thread_id, "--json"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        try:
            return json.loads(r.stdout.strip().splitlines()[-1]).get("status")
        except Exception:
            return None
    return None


# Statuses that mean the solver thread is still working (keep its account leased).
_BUSY_STATUSES = {"running", "unknown", "queued", "pending", "starting", None}


def reap_finished(t3_manage: str, active: dict) -> None:
    """Release the account of any leased thread that has finished/errored."""
    for tid, label in list(active.items()):
        st = t3_status(t3_manage, tid)
        if st not in _BUSY_STATUSES:
            log(f"  Thread {tid} ({label}) finished [{st}] — releasing account")
            release_account(label=label)
            active.pop(tid, None)


def challenge_field(c: dict, *names, default="?"):
    for n in names:
        if c.get(n) not in (None, ""):
            return c[n]
    return default


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def build_solver_prompt(target_flag: str, detail: dict, account: str = None) -> str:
    cid = challenge_field(detail, "id")
    title = challenge_field(detail, "title", "name")
    category = lib.category_name(detail)
    points = lib.challenge_points(detail)
    desc = lib.strip_html(detail.get("description") or detail.get("content") or "")
    slug = slugify(str(title)) or str(cid)

    # Every FlagYard command is pinned to this thread's leased account via the
    # flagyard-submit skill's `--account <name>` flag (each named account has its
    # own token/refresh pair + its own FlagYard instance-start quota). Telegram
    # config is global, so the writeup command intentionally has NO --account.
    acct = f" --account {account}" if account else ""
    workdir = f"/tmp/flagyard-{cid}-{slug}"
    writeup = f"{workdir}/writeup.md"

    dl_cmd = f"python3 {FLAGYARD_SCRIPTS}/get_attachment.py {target_flag} --challenge-id {cid}{acct} -o {workdir}/"
    senddoc_cmd = (f"python3 {FLAGYARD_SCRIPTS}/telegram.py send-doc "
                   f"--file {writeup} --caption 'SOLVED: {title} [{category}] — FLAG: <paste flag>'")

    prompt = f"""You are solving a CTF challenge autonomously on FlagYard.

==================================================================
!!! CRITICAL RULE — DO NOT SUBMIT THE FLAG !!!
- NEVER submit the flag to FlagYard. Do NOT run submit_flag.py. Do NOT POST to
  any /flag endpoint, and do NOT call the flag API via api.py or curl.
- Your ONLY deliverable is a writeup sent to Telegram (instructions below).
- A human submits the flag from the real competition team. If you submit it
  yourself you break the whole workflow. When in doubt: DO NOT SUBMIT.
==================================================================

CHALLENGE: {title} ({category}, {points} pts, ID: {cid})
DESCRIPTION:
{desc or "(no description)"}

"""
    files = detail.get("files") or detail.get("challengeFiles") or []
    if files:
        prompt += f"ATTACHMENT(S): this challenge has downloadable file(s).\nDownload with: {dl_cmd}\n\n"

    if detail.get("internalPort") is not None:
        start_cmd = f"python3 {FLAGYARD_SCRIPTS}/instance.py {target_flag} --challenge-id {cid}{acct} --action start"
        status_cmd = f"python3 {FLAGYARD_SCRIPTS}/instance.py {target_flag} --challenge-id {cid}{acct} --action status"
        stop_cmd = f"python3 {FLAGYARD_SCRIPTS}/instance.py {target_flag} --challenge-id {cid}{acct} --action stop"
        prompt += (
            f"TARGET INSTANCE: this challenge needs a dynamic instance/container.\n"
            f"Start it with: {start_cmd}\n"
            f"Check status/connection info with: {status_cmd}\n"
            f"When you are DONE, stop it to free your account's instance slot: {stop_cmd}\n\n"
        )

    acct_note = (
        f"- You have a DEDICATED FlagYard account '{account}'. Every FlagYard command\n"
        f"  above is already pinned to it via `--account {account}` — run them exactly\n"
        f"  as given; do NOT drop --account or touch any other account.\n"
        if account else
        "- Auth is already configured — the scripts use the saved FlagYard token/refresh\n"
        "  pair automatically, you don't need to pass --token.\n"
    )

    prompt += f"""INSTRUCTIONS:
- Work autonomously: analyze and SOLVE the challenge to recover the flag.
- Working directory: use {workdir}/ (create it).
{acct_note}- ⛔ DO NOT SUBMIT THE FLAG. Never run submit_flag.py; never POST to a /flag
  endpoint (directly, via api.py, or curl). Reporting is writeup-only — a human
  submits from the real competition team.
- When you have the flag, write a clear Markdown writeup to {writeup} containing:
  challenge name/category, your approach, the key steps, any exploit script or
  payload, and the final flag on its own line as `FLAG: <flag>`.
- Then send that writeup to Telegram as a document (this is your ONLY report —
  it does NOT submit the flag, it just delivers the writeup):
    {senddoc_cmd}
  (put the actual flag in the --caption in place of <paste flag>).
- Never unlock/purchase hints (do not spend points).
- Try multiple approaches if the first doesn't work.
- Flag format varies per event — check the description for a prefix hint.
- Reminder: your job ends at "writeup sent to Telegram". DO NOT SUBMIT THE FLAG."""

    return prompt


def spawn_thread(kind, container, detail, project, instance, model, t3_manage,
                 dry_run=False, account=None):
    cid = challenge_field(detail, "id")
    title = challenge_field(detail, "title", "name")
    category = lib.category_name(detail)
    label = slugify(f"{category}-{title}")[:40]
    thread_title = f"{category} - {title}"
    target_flag = f"--event-id {container}" if kind == "events" else f"--lab-id {container}"

    prompt = build_solver_prompt(target_flag, detail, account)

    cmd = ["python3", t3_manage, "spawn", "--project", project, "--title", thread_title, "--label", label]
    if instance and model:
        cmd += ["--instance", instance, "--model", model]
    if dry_run:
        cmd += ["--dry-run"]
    cmd += ["--json", prompt]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        thread_id = None
        try:
            thread_id = json.loads(result.stdout.strip().splitlines()[-1]).get("threadId")
        except Exception:
            pass
        log(f"  [SPAWNED] {thread_title}" + (f" (thread {thread_id})" if thread_id else ""))
        return True, thread_id
    log(f"  [ERROR] {thread_title}: {result.stderr.strip()}")
    return False, None


def load_seen(seen_file: str) -> set:
    if os.path.exists(seen_file):
        with open(seen_file) as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_seen(seen_file: str, seen: set) -> None:
    with open(seen_file, "w") as f:
        f.write("\n".join(sorted(seen)) + "\n")


def poll_once(kind, container, token, project, instance, model, t3_manage, seen, dry_run,
              report_telegram=True, multi=False, active=None):
    """Fetch the challenge list, spawn threads for anything new+unsolved.
    In multi-account mode, lease a distinct account per thread (deferring
    challenges when the pool is exhausted) and release accounts of finished
    threads. Returns (updated_seen, spawned_count)."""
    active = active if active is not None else {}

    # Free up accounts whose solver threads have finished before spawning more.
    if multi and active:
        reap_finished(t3_manage, active)

    list_path = f"{lib.container_path(kind, container)}/challenges"
    status, body = lib.api_json("GET", list_path, token)
    if status != 200:
        log(f"Poll error: HTTP {status}: {lib.envelope_message(body)}")
        return seen, 0

    rows = list(lib.flatten_challenges(lib.unwrap(body)))
    new_challenges = [
        c for c in rows
        if str(challenge_field(c, "id")) not in seen and not lib.is_solved(c)
    ]

    spawned = 0
    # Only solved or successfully-spawned challenges get marked seen (below), so
    # anything we skip here — a detail-fetch/spawn failure, or a deferred
    # challenge with no free account — stays unseen and is retried next poll.
    newly_handled = set()
    if new_challenges:
        log(f"{len(new_challenges)} NEW challenge(s) detected!")
        for c in new_challenges:
            cid = str(challenge_field(c, "id"))
            title = challenge_field(c, "title", "name")
            cat = lib.category_name(c)
            pts = lib.challenge_points(c)
            log(f"  NEW: #{cid} [{cat}] {title} ({pts}pts)")

            acct_label = None
            if multi:
                acct = lease_account()
                if not acct:
                    log(f"  [DEFER] #{cid}: no free account in the pool — will retry next poll")
                    continue
                acct_label = acct["label"]
                log(f"  Leased account '{acct_label}' for #{cid}")

            try:
                detail_path = f"{lib.container_path(kind, container)}/challenges/{cid}"
                dstatus, dbody = lib.api_json("GET", detail_path, token)
                if dstatus != 200:
                    log(f"  [ERROR] #{cid}: HTTP {dstatus}: {lib.envelope_message(dbody)} — will retry next poll")
                    if acct_label:
                        release_account(label=acct_label)
                    continue
                detail = lib.unwrap(dbody)
                ok, thread_id = spawn_thread(kind, container, detail, project, instance,
                                             model, t3_manage, dry_run, acct_label)
                if ok:
                    spawned += 1
                    newly_handled.add(cid)
                    if acct_label:
                        # Hold the lease for the running thread; release on dry-run
                        # or if we couldn't capture a thread id to track it.
                        if not dry_run and thread_id:
                            bind_account(acct_label, thread_id)
                            active[thread_id] = acct_label
                        else:
                            release_account(label=acct_label)
                    if report_telegram and not dry_run:
                        lib.notify_telegram_safe(
                            f"🆕 <b>New challenge</b>: {title} ({cat}, {pts}pts)\n"
                            f"Solver thread spawned ({kind[:-1]} {container} / challenge {cid})"
                            + (f" on account <code>{acct_label}</code>." if acct_label else ".")
                        )
                else:
                    log(f"  [ERROR] #{cid}: spawn failed — will retry next poll")
                    if acct_label:
                        release_account(label=acct_label)
            except Exception as exc:
                log(f"  [ERROR] #{cid}: {exc} — will retry next poll")
                if acct_label:
                    release_account(label=acct_label)

    # Only challenges that are solved or successfully spawned get marked
    # "seen" — anything that failed (detail fetch, spawn) or was deferred (no
    # free account) stays unseen so the next poll retries it.
    solved_ids = {str(challenge_field(c, "id")) for c in rows if lib.is_solved(c)}
    seen = seen | solved_ids | newly_handled
    return seen, spawned


def poll_loop(kind, container, token_arg, project, instance, model, t3_manage,
              poll_interval, once, seen_file, dry_run, report_telegram=True, multi=False):
    seen = load_seen(seen_file)
    active = {}  # thread_id -> account label (leased, still running)
    while True:
        try:
            token = lib.load_token(token_arg)  # refresh-aware; keeps polling alive
            seen, _ = poll_once(kind, container, token, project, instance, model, t3_manage, seen,
                                 dry_run, report_telegram, multi=multi, active=active)
            save_seen(seen_file, seen)
        except Exception as exc:
            log(f"Poll error: {exc}")

        if once:
            log("One-shot complete." + (f" {len(active)} account(s) still leased to running threads."
                                         if multi and active else ""))
            return
        time.sleep(poll_interval)


def main() -> int:
    lib.ensure_utf8_stdout()
    ap = argparse.ArgumentParser(description="FlagYard Watcher — auto-spawn T3 solver threads")
    lib.add_common_args(ap)
    ap.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL,
                     help=f"Seconds between polls (default {DEFAULT_POLL_INTERVAL})")
    ap.add_argument("--once", action="store_true", help="Spawn for all unsolved, then exit")
    ap.add_argument("--project", help="T3 project name (default: FlagYard - <event/lab id>)")
    ap.add_argument("--instance", default=DEFAULT_INSTANCE,
                     help=f"Provider instance id (default: {DEFAULT_INSTANCE})")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                     help=f"Model id (default: {DEFAULT_MODEL})")
    ap.add_argument("--seen-file", default="./seen_challenges_flagyard.txt")
    ap.add_argument("--dry-run", action="store_true",
                     help="Show what would be spawned without dispatching to T3")
    ap.add_argument("--no-telegram", action="store_true",
                     help="Don't report new-challenge/spawn events to Telegram "
                          "(uses the flagyard-submit skill's telegram.py config)")
    ap.add_argument("--single-account", action="store_true",
                     help="Force the single shared account even if an account pool "
                          "is configured (disables per-thread account leasing).")
    args = ap.parse_args()

    kind, container, _ = lib.resolve_target(args)

    # Multi-account mode auto-enables when the pool (accounts.py) has entries.
    pool = accounts_list()
    multi = bool(pool) and not args.single_account

    # The watcher's own read-only polling uses the DEFAULT account (or --token /
    # $FLAGYARD_TOKEN) — separate from the worker pool, so it never contends with
    # a solver thread's token refresh. Listing challenges doesn't consume any
    # per-account instance quota.
    token = lib.load_token(args.token)
    lib.warn_if_expired(token)

    t3_manage = find_t3_manage()
    if not t3_manage:
        print("ERROR: t3_manage.py not found (expected under ~/.claude/skills/t3-manage/scripts/).",
              file=sys.stderr)
        return 1

    project = args.project or f"FlagYard - {container}"

    log("FlagYard Watcher started")
    log(f"  Target:   {kind}/{container}")
    log(f"  Project:  {project}")
    log(f"  Poll:     {args.poll_interval}s" + (" (one-shot)" if args.once else ""))
    if multi:
        log(f"  Accounts: multi-account pool ({len(pool)} configured) — one leased per thread")
    else:
        log("  Accounts: single shared account")
    if args.dry_run:
        log("  Mode:     DRY RUN (no threads will actually be spawned)")

    poll_loop(kind, container, args.token, project, args.instance, args.model, t3_manage,
               args.poll_interval, args.once, args.seen_file, args.dry_run,
               report_telegram=not args.no_telegram, multi=multi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
