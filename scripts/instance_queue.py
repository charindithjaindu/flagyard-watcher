#!/usr/bin/env python3
"""Instance Queue — enforce a single concurrent dynamic-instance challenge
*per FlagYard account*.

FlagYard only allows ONE running instance per account. If multiple solver
threads on the SAME account work instance-based challenges in parallel,
whichever calls `instance.py --action start` most recently silently kills
everyone else's instance out from under them — confirmed live during
BlackHat MEA CTF 2026 (Teto's running PWN instance was killed the moment
Huddle's WEB thread started its own, both on the same account).

Run one instance of this script PER ACCOUNT (--account), each with its own
list of challenge threads. Within one account's queue, only the active
entry's thread is resumed; every other entry stays interrupted (paused) so
it can't race for that account's instance slot. Each turn runs for up to
--turn-minutes or until the challenge is marked solved (isCompletedByUser),
whichever comes first — then the instance is stopped and the next unsolved
entry becomes active.

Usage:
    python3 instance_queue.py --event-id <uuid> --account rusiru \
      --entry "Huddle:ee3ec715-b095-4abc-b023-3f1fa3b65fc3:01a02951-4854-72a3-ad24-0511a629f68f" \
      --entry "Dead-Drop:184c9271-6abd-44f8-bd85-28112411bb27:01a00b67-02ed-78c9-b492-372ea499e910" \
      --active Huddle --turn-minutes 20
"""
import argparse
import json
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
    print("ERROR: flagyard-submit skill not found.", file=sys.stderr)
    sys.exit(1)
sys.path.insert(0, str(FLAGYARD_SCRIPTS))
import flagyard_lib as lib  # noqa: E402

INSTANCE_PY = FLAGYARD_SCRIPTS / "instance.py"

DEFAULT_INSTANCE = "cursor"
DEFAULT_MODEL = "claude-opus-4-6[effort=high]"
DEFAULT_TURN_MINUTES = 20
DEFAULT_POLL_SECONDS = 30


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


def log(label: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] [{label}] {msg}", flush=True)


def t3(t3_manage, *args, timeout=30):
    result = subprocess.run(
        ["python3", t3_manage, *args, "--json"],
        capture_output=True, text=True, timeout=timeout,
    )
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"ok": False, "raw_stdout": result.stdout, "raw_stderr": result.stderr}


def thread_status(t3_manage, thread_id):
    r = t3(t3_manage, "status", "--thread", thread_id)
    return r.get("status", "unknown")


def interrupt(t3_manage, thread_id):
    return t3(t3_manage, "interrupt", "--thread", thread_id)


def send(t3_manage, thread_id, message, instance, model):
    return t3(t3_manage, "send", message, "--thread", thread_id,
              "--instance", instance, "--model", model)


def instance_action(target_flag_args, challenge_id, action, account):
    cmd = ["python3", str(INSTANCE_PY)] + target_flag_args + [
        "--challenge-id", challenge_id, "--action", action, "--json",
    ]
    if account:
        cmd += ["--account", account]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except Exception:
        return None


def fetch_solved_map(kind, container, account):
    """Return {challenge_id: bool solved} for the whole event/lab."""
    token = lib.load_token(None, account=account)
    path = f"{lib.container_path(kind, container)}/challenges"
    status, body = lib.api_json("GET", path, token, account=account)
    if status != 200:
        log("queue", f"WARNING: couldn't refresh solved-status (HTTP {status}): {lib.envelope_message(body)}")
        return {}
    rows = list(lib.flatten_challenges(lib.unwrap(body)))
    return {str(c.get("id")): lib.is_solved(c) for c in rows}


def load_state(state_file, queue, active_label):
    if Path(state_file).exists():
        try:
            st = json.loads(Path(state_file).read_text())
            if [e["label"] for e in st.get("queue", [])] == [e["label"] for e in queue]:
                return st
        except Exception:
            pass
    active_index = 0
    if active_label:
        for i, e in enumerate(queue):
            if e["label"] == active_label:
                active_index = i
                break
    return {"queue": queue, "active_index": active_index, "solved": []}


def save_state(state_file, state):
    Path(state_file).write_text(json.dumps(state, indent=2))


def run_turn(entry, target_flag_args, t3_manage, instance, model, account,
             turn_seconds, poll_seconds, kind, container, already_active):
    label, thread_id, cid = entry["label"], entry["thread_id"], entry["challenge_id"]
    log(label, f"ACTIVE (thread {thread_id[:8]}, challenge {cid[:8]}, account={account or 'default'})")

    if not already_active:
        msg = (
            f"It's your turn now — you have EXCLUSIVE use of the "
            f"{('account ' + account) if account else 'default account'}'s instance slot for "
            f"the next ~{turn_seconds // 60} minutes. Nobody else on this account will start a "
            f"competing instance during that window, so `instance.py"
            f"{f' --account {account}' if account else ''} --action start` will work and stay "
            f"up. Continue (or begin) working. If you finish or run out of ideas before the "
            f"window ends, that's fine — just keep going until you're paused."
        )
        r = send(t3_manage, thread_id, msg, instance, model)
        if not r.get("ok"):
            log(label, f"WARNING: resume failed: {r}")

    deadline = time.time() + turn_seconds
    while time.time() < deadline:
        time.sleep(poll_seconds)
        solved = fetch_solved_map(kind, container, account)
        if solved.get(cid):
            log(label, "SOLVED — ending turn early.")
            break
        st = thread_status(t3_manage, thread_id)
        if st not in ("running", "starting"):
            log(label, f"thread went '{st}' on its own — ending turn early.")
            break
    else:
        log(label, "turn budget elapsed.")

    st = thread_status(t3_manage, thread_id)
    if st in ("running", "starting"):
        log(label, "interrupting (still running)...")
        interrupt(t3_manage, thread_id)

    log(label, "stopping instance...")
    instance_action(target_flag_args, cid, "stop", account)


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-account round-robin single-instance CTF scheduler")
    ap.add_argument("--event-id")
    ap.add_argument("--lab-id")
    ap.add_argument("--account", help="FlagYard account to run this queue under (see auth.py)")
    ap.add_argument("--entry", action="append", required=True,
                     help="label:thread_id:challenge_id (repeatable)")
    ap.add_argument("--active", help="Label that already holds the instance slot right now")
    ap.add_argument("--turn-minutes", type=int, default=DEFAULT_TURN_MINUTES)
    ap.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    ap.add_argument("--instance", default=DEFAULT_INSTANCE)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--state-file", default="./instance_queue_state.json")
    args = ap.parse_args()

    if args.event_id:
        kind, container = "events", args.event_id
    elif args.lab_id:
        kind, container = "labs", args.lab_id
    else:
        print("Provide --event-id or --lab-id", file=sys.stderr)
        return 2
    target_flag_args = ["--event-id", container] if kind == "events" else ["--lab-id", container]

    t3_manage = find_t3_manage()
    if not t3_manage:
        print("ERROR: t3_manage.py not found.", file=sys.stderr)
        return 1

    queue = []
    for spec in args.entry:
        label, thread_id, cid = spec.split(":", 2)
        queue.append({"label": label, "thread_id": thread_id, "challenge_id": cid})

    state = load_state(args.state_file, queue, args.active)
    turn_seconds = args.turn_minutes * 60

    log("queue", f"{len(queue)} entries (account={args.account or 'default'}): "
                  f"{', '.join(e['label'] for e in queue)}")
    first_turn = True
    while True:
        solved_map = fetch_solved_map(kind, container, args.account)
        for e in queue:
            if solved_map.get(e["challenge_id"]) and e["label"] not in state["solved"]:
                state["solved"].append(e["label"])
                log(e["label"], "confirmed solved, removing from rotation.")

        pending = [e for e in queue if e["label"] not in state["solved"]]
        if not pending:
            log("queue", "All challenges in this queue are solved. Done.")
            return 0

        active_label = queue[state["active_index"] % len(queue)]["label"]
        active_entries = [e for e in pending if e["label"] == active_label]
        entry = active_entries[0] if active_entries else pending[0]

        was_already_active = first_turn and args.active == entry["label"]
        run_turn(entry, target_flag_args, t3_manage, args.instance, args.model, args.account,
                 turn_seconds, args.poll_seconds, kind, container, was_already_active)
        first_turn = False

        idx = queue.index(entry)
        state["active_index"] = (idx + 1) % len(queue)
        save_state(args.state_file, state)


if __name__ == "__main__":
    raise SystemExit(main())
