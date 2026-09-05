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


def challenge_field(c: dict, *names, default="?"):
    for n in names:
        if c.get(n) not in (None, ""):
            return c[n]
    return default


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def build_solver_prompt(target_flag: str, detail: dict) -> str:
    cid = challenge_field(detail, "id")
    title = challenge_field(detail, "title", "name")
    category = lib.category_name(detail)
    points = lib.challenge_points(detail)
    desc = lib.strip_html(detail.get("description") or detail.get("content") or "")
    slug = slugify(str(title)) or str(cid)

    dl_cmd = f"python3 {FLAGYARD_SCRIPTS}/get_attachment.py {target_flag} --challenge-id {cid}"
    submit_cmd = f"python3 {FLAGYARD_SCRIPTS}/submit_flag.py {target_flag} --challenge-id {cid} --flag 'FLAG_HERE'"

    prompt = f"""You are solving a CTF challenge autonomously on FlagYard.

CHALLENGE: {title} ({category}, {points} pts, ID: {cid})
DESCRIPTION:
{desc or "(no description)"}

"""
    files = detail.get("files") or detail.get("challengeFiles") or []
    if files:
        prompt += f"ATTACHMENT(S): this challenge has downloadable file(s).\nDownload with: {dl_cmd}\n\n"

    if detail.get("internalPort") is not None:
        start_cmd = f"python3 {FLAGYARD_SCRIPTS}/instance.py {target_flag} --challenge-id {cid} --action start"
        status_cmd = f"python3 {FLAGYARD_SCRIPTS}/instance.py {target_flag} --challenge-id {cid} --action status"
        prompt += (
            f"TARGET INSTANCE: this challenge needs a dynamic instance/container.\n"
            f"Start it with: {start_cmd}\n"
            f"Check status/connection info with: {status_cmd}\n\n"
        )

    prompt += f"""INSTRUCTIONS:
- Work autonomously: analyze, solve, and submit the flag
- Submit with: {submit_cmd}
  (this also reports the result — correct or all-attempts-failed — to Telegram
  automatically, see the flagyard-submit skill's telegram.py)
- Working directory: use /tmp/flagyard-{cid}-{slug}/ (create it)
- Auth is already configured — the scripts use the saved FlagYard token/refresh
  pair automatically, you don't need to pass --token
- Never unlock/purchase hints (do not spend points)
- Try multiple approaches if the first doesn't work
- Flag format varies per event — check the description for a prefix hint"""

    return prompt


def spawn_thread(kind, container, detail, project, instance, model, t3_manage, dry_run=False):
    cid = challenge_field(detail, "id")
    title = challenge_field(detail, "title", "name")
    category = lib.category_name(detail)
    label = slugify(f"{category}-{title}")[:40]
    thread_title = f"{category} - {title}"
    target_flag = f"--event-id {container}" if kind == "events" else f"--lab-id {container}"

    prompt = build_solver_prompt(target_flag, detail)

    cmd = ["python3", t3_manage, "spawn", "--project", project, "--title", thread_title, "--label", label]
    if instance and model:
        cmd += ["--instance", instance, "--model", model]
    if dry_run:
        cmd += ["--dry-run"]
    cmd += ["--json", prompt]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        log(f"  [SPAWNED] {thread_title}")
        return True
    log(f"  [ERROR] {thread_title}: {result.stderr.strip()}")
    return False


def load_seen(seen_file: str) -> set:
    if os.path.exists(seen_file):
        with open(seen_file) as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_seen(seen_file: str, seen: set) -> None:
    with open(seen_file, "w") as f:
        f.write("\n".join(sorted(seen)) + "\n")


def poll_once(kind, container, token, project, instance, model, t3_manage, seen, dry_run,
              report_telegram=True):
    """Fetch the challenge list, spawn threads for anything new+unsolved.
    Returns (updated_seen, spawned_count)."""
    list_path = f"{lib.container_path(kind, container)}/challenges"
    status, body = lib.api_json("GET", list_path, token)
    if status != 200:
        log(f"Poll error: HTTP {status}: {lib.envelope_message(body)}")
        return seen, 0

    rows = list(lib.flatten_challenges(lib.unwrap(body)))
    all_ids = {str(challenge_field(c, "id")) for c in rows}
    new_challenges = [
        c for c in rows
        if str(challenge_field(c, "id")) not in seen and not lib.is_solved(c)
    ]

    spawned = 0
    newly_handled = set()
    if new_challenges:
        log(f"{len(new_challenges)} NEW challenge(s) detected!")
        for c in new_challenges:
            cid = str(challenge_field(c, "id"))
            title = challenge_field(c, "title", "name")
            cat = lib.category_name(c)
            pts = lib.challenge_points(c)
            log(f"  NEW: #{cid} [{cat}] {title} ({pts}pts)")
            try:
                detail_path = f"{lib.container_path(kind, container)}/challenges/{cid}"
                dstatus, dbody = lib.api_json("GET", detail_path, token)
                if dstatus != 200:
                    log(f"  [ERROR] #{cid}: HTTP {dstatus}: {lib.envelope_message(dbody)} — will retry next poll")
                    continue
                detail = lib.unwrap(dbody)
                if spawn_thread(kind, container, detail, project, instance, model, t3_manage, dry_run):
                    spawned += 1
                    newly_handled.add(cid)
                    if report_telegram and not dry_run:
                        lib.notify_telegram_safe(
                            f"🆕 <b>New challenge</b>: {title} ({cat}, {pts}pts)\n"
                            f"Solver thread spawned ({kind[:-1]} {container} / challenge {cid})."
                        )
                else:
                    log(f"  [ERROR] #{cid}: spawn failed — will retry next poll")
            except Exception as exc:
                log(f"  [ERROR] #{cid}: {exc} — will retry next poll")

    # Only challenges that are solved or successfully spawned get marked
    # "seen" — anything that failed (detail fetch, spawn) stays unseen so
    # the next poll retries it instead of silently dropping it forever.
    solved_ids = {str(challenge_field(c, "id")) for c in rows if lib.is_solved(c)}
    seen = seen | solved_ids | newly_handled
    return seen, spawned


def poll_loop(kind, container, token, project, instance, model, t3_manage,
              poll_interval, once, seen_file, dry_run, report_telegram=True):
    seen = load_seen(seen_file)
    while True:
        try:
            seen, _ = poll_once(kind, container, token, project, instance, model, t3_manage, seen,
                                 dry_run, report_telegram)
            save_seen(seen_file, seen)
        except Exception as exc:
            log(f"Poll error: {exc}")

        if once:
            log("One-shot complete.")
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
    args = ap.parse_args()

    kind, container, _ = lib.resolve_target(args)
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
    if args.dry_run:
        log("  Mode:     DRY RUN (no threads will actually be spawned)")

    poll_loop(kind, container, token, project, args.instance, args.model, t3_manage,
               args.poll_interval, args.once, args.seen_file, args.dry_run,
               report_telegram=not args.no_telegram)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
