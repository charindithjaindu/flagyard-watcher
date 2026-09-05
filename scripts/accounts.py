#!/usr/bin/env python3
"""Multi-account state manager for the FlagYard Watcher.

FlagYard limits each account to ONE running dynamic instance at a time —
starting an instance on account A kills A's previous one. To solve several
instance-backed challenges in parallel, the watcher keeps a POOL of FlagYard
accounts (each a separate team/user) and leases a distinct one to every solver
thread.

Each account is a flagyard-submit *named account*: its tokens live under
`~/.config/flagyard/accounts/<label>/` (exactly where the skill's `--account
<label>` flag reads/refreshes them). So an account seeded here is immediately
usable as `instance.py ... --account <label>`; that flag is the entire isolation
mechanism. This file adds only the LEASE layer (free/leased state) on top — it
is pure orchestration and lives in the watcher repo, not the skill.

State lives at  $FLAGYARD_ACCOUNTS_DIR (default ~/.config/flagyard/accounts):
    accounts.json          # the pool + lease state (this file's own)
    <label>/token          # per-account access JWT      (shared with the skill)
    <label>/refresh_token  # per-account Keycloak refresh (shared with the skill)

Commands:
    accounts.py add --label web1 [--team "Team A"] [--file tokens.json]
                                   # seed an account (paste Keycloak token JSON on stdin)
    accounts.py list [--json]      # show pool + lease state + token expiries
    accounts.py lease --thread <id|pending> [--json]
                                   # atomically take a free account (exit 3 if none free)
    accounts.py bind --label web1 --thread <id>   # attach a real thread id post-spawn
    accounts.py release (--label web1 | --thread <id>)
    accounts.py refresh-all        # refresh every account's access token (keep warm)
    accounts.py remove --label web1
"""
import argparse
import fcntl
import json
import os
import pathlib
import sys
import time

# --- locate the flagyard-submit skill (same discovery as the watcher) --------
_SKILL_CANDIDATES = [
    pathlib.Path.home() / ".claude" / "skills" / "flagyard-submit" / "scripts",
    pathlib.Path.home() / ".cursor" / "skills" / "flagyard-submit" / "scripts",
    pathlib.Path(__file__).resolve().parent.parent / "vendor" / "flagyard-submit" / "scripts",
]
for _p in _SKILL_CANDIDATES:
    if (_p / "flagyard_lib.py").exists():
        sys.path.insert(0, str(_p))
        break
try:
    import flagyard_lib as lib  # noqa: E402
except Exception:
    sys.stderr.write(
        "ERROR: flagyard-submit skill not found (flagyard_lib.py). Looked in:\n  "
        + "\n  ".join(str(p) for p in _SKILL_CANDIDATES) + "\n"
    )
    raise

BASE = pathlib.Path(
    os.environ.get("FLAGYARD_ACCOUNTS_DIR")
    or (pathlib.Path.home() / ".config" / "flagyard" / "accounts")
).expanduser()
STATE = BASE / "accounts.json"


# --- state persistence (lock + atomic write) ---------------------------------
class _Lock:
    def __enter__(self):
        BASE.mkdir(parents=True, exist_ok=True)
        self._f = open(BASE / ".lock", "w")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()


def _load() -> dict:
    if not STATE.exists():
        return {"accounts": []}
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        data.setdefault("accounts", [])
        return data
    except Exception:
        return {"accounts": []}


def _save(state: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE)


def _exp_str(token: str) -> str:
    exp = lib.token_exp(token) if token else None
    if not exp:
        return "?"
    left = exp - int(time.time())
    when = time.strftime("%H:%M:%S", time.localtime(exp))
    return f"{when} ({left}s)" if left > 0 else f"{when} EXPIRED"


def _read_account_token(entry: dict) -> str:
    p = pathlib.Path(entry["dir"]) / "token"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


# --- commands ----------------------------------------------------------------
def cmd_add(args) -> int:
    raw = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        print("Input is not valid JSON. Paste the Keycloak token response "
              "(access_token + refresh_token).", file=sys.stderr)
        return 2
    access = obj.get("access_token")
    refresh = obj.get("refresh_token")
    if not access or not refresh:
        print("JSON must contain both access_token and refresh_token.", file=sys.stderr)
        return 2

    adir = BASE / args.label
    adir.mkdir(parents=True, exist_ok=True)
    tok = adir / "token"
    rt = adir / "refresh_token"
    tok.write_text(lib._clean_token(access), encoding="utf-8")
    rt.write_text(refresh.strip(), encoding="utf-8")
    for p in (tok, rt):
        try:
            p.chmod(0o600)
        except OSError:
            pass

    with _Lock():
        state = _load()
        existing = next((a for a in state["accounts"] if a["label"] == args.label), None)
        if existing:
            existing["dir"] = str(adir)
            if args.team is not None:
                existing["team"] = args.team
        else:
            state["accounts"].append({
                "label": args.label,
                "team": args.team or "",
                "dir": str(adir),
                "status": "free",
                "thread": None,
                "leased_at": None,
            })
        _save(state)
    print(f"Account '{args.label}' seeded. access exp {_exp_str(lib._clean_token(access))}")
    return 0


def cmd_list(args) -> int:
    state = _load()
    if args.as_json:
        print(json.dumps(state, indent=2))
        return 0
    accs = state["accounts"]
    if not accs:
        print("No accounts. Seed one:  accounts.py add --label <label>")
        return 0
    print(f"{'LABEL':<12} {'STATUS':<7} {'TEAM':<16} {'ACCESS EXP':<20} LEASE")
    for a in accs:
        exp = _exp_str(_read_account_token(a))
        lease = f"thread={a.get('thread')}" if a["status"] != "free" else ""
        print(f"{a['label']:<12} {a['status']:<7} {(a.get('team') or '-'):<16} {exp:<20} {lease}")
    return 0


def cmd_lease(args) -> int:
    with _Lock():
        state = _load()
        free = [a for a in state["accounts"] if a["status"] == "free"]
        if not free:
            print("No free account available.", file=sys.stderr)
            return 3
        a = free[0]
        a["status"] = "leased"
        a["thread"] = args.thread
        a["leased_at"] = int(time.time())
        _save(state)
    out = {"label": a["label"], "dir": a["dir"], "team": a.get("team", "")}
    print(json.dumps(out) if args.as_json else f"{a['label']} {a['dir']}")
    return 0


def cmd_bind(args) -> int:
    with _Lock():
        state = _load()
        ok = False
        for a in state["accounts"]:
            if a["label"] == args.label:
                a["thread"] = args.thread
                ok = True
        _save(state)
    if not ok:
        print(f"No account labelled '{args.label}'.", file=sys.stderr)
        return 1
    return 0


def cmd_release(args) -> int:
    if not args.label and not args.thread:
        print("Provide --label or --thread.", file=sys.stderr)
        return 2
    with _Lock():
        state = _load()
        changed = 0
        for a in state["accounts"]:
            if (args.label and a["label"] == args.label) or \
               (args.thread and a.get("thread") == args.thread):
                a["status"] = "free"
                a["thread"] = None
                a["leased_at"] = None
                changed += 1
        _save(state)
    print(f"Released {changed} account(s)." if changed else "No matching leased account.")
    return 0 if changed else 1


def cmd_refresh_all(_args) -> int:
    state = _load()
    ok = fail = 0
    for a in state["accounts"]:
        d = pathlib.Path(a["dir"])
        rt = d / "refresh_token"
        if not rt.exists():
            print(f"  {a['label']}: no refresh token", file=sys.stderr)
            fail += 1
            continue
        try:
            bundle = lib.refresh_access_token(rt.read_text(encoding="utf-8").strip())
        except Exception as exc:
            print(f"  {a['label']}: refresh FAILED — {exc}", file=sys.stderr)
            fail += 1
            continue
        (d / "token").write_text(lib._clean_token(bundle["access_token"]), encoding="utf-8")
        (d / "token").chmod(0o600)
        if bundle.get("refresh_token"):
            (d / "refresh_token").write_text(bundle["refresh_token"].strip(), encoding="utf-8")
            (d / "refresh_token").chmod(0o600)
        print(f"  {a['label']}: refreshed, exp {_exp_str(bundle['access_token'])}")
        ok += 1
    print(f"Refreshed {ok}, failed {fail}.")
    return 0 if fail == 0 else 1


def cmd_remove(args) -> int:
    with _Lock():
        state = _load()
        before = len(state["accounts"])
        state["accounts"] = [a for a in state["accounts"] if a["label"] != args.label]
        _save(state)
    print("Removed." if before != len(state["accounts"]) else "Not found.")
    return 0


def main() -> int:
    lib.ensure_utf8_stdout()
    ap = argparse.ArgumentParser(description="FlagYard multi-account pool manager.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add"); a.add_argument("--label", required=True)
    a.add_argument("--team"); a.add_argument("--file")

    ls = sub.add_parser("list"); ls.add_argument("--json", action="store_true", dest="as_json")

    le = sub.add_parser("lease"); le.add_argument("--thread", default="pending")
    le.add_argument("--json", action="store_true", dest="as_json")

    bd = sub.add_parser("bind"); bd.add_argument("--label", required=True)
    bd.add_argument("--thread", required=True)

    rl = sub.add_parser("release"); rl.add_argument("--label"); rl.add_argument("--thread")

    sub.add_parser("refresh-all")

    rm = sub.add_parser("remove"); rm.add_argument("--label", required=True)

    args = ap.parse_args()
    return {
        "add": cmd_add, "list": cmd_list, "lease": cmd_lease, "bind": cmd_bind,
        "release": cmd_release, "refresh-all": cmd_refresh_all, "remove": cmd_remove,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
