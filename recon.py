#!/usr/bin/env python3
"""
recon.py — on-demand debug access into the LiteLLM traffic router.

Successor to the OmniRoute-era classify.py. That script could work off the
call-log directory OmniRoute persisted for every request; LiteLLM persists
nothing usable — with `store_prompts_in_spend_logs` off, `proxy_server_request`
and `messages` are literally `{}` in every spend row, and turning it on records
only rendered messages, never headers or body structure. So traffic_router.py
grew its own capture sink, armed on demand, and this drives it.

The flow for writing a router profile for a new coding agent:

    recon.py on            # arm capture (unknowns only, 30 min, 50 records)
    <run the agent once>
    recon.py list          # distinct callers, clustered by fingerprint
    recon.py show <fp>     # full inbound shape: headers, system, tools, body
    recon.py suggest <fp>  # candidate marker substrings to paste into the hook
    recon.py off

Capture self-disables after max_records or expire_minutes even if you forget
`off`, so an armed session cannot run away.

Paths default to the host-side litellm config volume; override with --config-dir
or $TRAFFIC_ROUTER_CONFIG_DIR. Run it on the host, not in the container.
"""

import argparse
import json
import os
import re
import sys
import time

DEFAULT_CONFIG_DIR = os.environ.get(
    "TRAFFIC_ROUTER_CONFIG_DIR",
    "/mnt/data/containers-backed/custom/default_litellm-config",
)
POLL_SECS = 1.0

DIM, BOLD, RED, CYAN, GREEN, YELLOW, RESET = (
    "\033[2m", "\033[1m", "\033[31m", "\033[36m", "\033[32m", "\033[33m", "\033[0m"
)
if not sys.stdout.isatty():
    DIM = BOLD = RED = CYAN = GREEN = YELLOW = RESET = ""

VERDICT_COLOR = {"unknown": RED, "security": GREEN, "cheap": GREEN,
                  "claude": CYAN, "codex": CYAN, "hermes": CYAN, "direct": DIM}

# Header names worth showing in the compact view — the ones that actually
# identify a caller. Everything else is in `show`.
INTERESTING_HEADERS = ("user-agent", "x-openai-subagent", "anthropic-beta", "x-app", "x-title")


def flag_path(cfg_dir):
    return os.path.join(cfg_dir, "recon.json")


def recon_dir(cfg_dir):
    return os.path.join(cfg_dir, "recon")


def load_records(cfg_dir):
    d = recon_dir(cfg_dir)
    out = []
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return out
    for name in names:
        try:
            with open(os.path.join(d, name), "r", errors="replace") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        rec["_file"] = name
        out.append(rec)
    return out


def header_of(rec, name):
    for k, v in (rec.get("headers") or {}).items():
        if k.lower() == name:
            return v if isinstance(v, str) else (v[0] if isinstance(v, list) and v else "")
    return ""


def first_line(text, width=90):
    if not text:
        return ""
    line = re.sub(r"\s+", " ", text).strip()
    return line[:width] + ("…" if len(line) > width else "")


def ts_str(rec):
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(rec.get("ts") or 0)))
    except (TypeError, ValueError):
        return "??:??:??"


# --------------------------------------------------------------------------


def cmd_on(args):
    cfg = {
        "capture": [c.strip() for c in args.capture.split(",") if c.strip()],
        "max_records": args.max,
        "expire_minutes": args.minutes,
        "max_text": args.max_text,
    }
    path = flag_path(args.config_dir)
    with open(path, "w") as fh:
        json.dump(cfg, fh, indent=2)
    # The hook measures the deadline from the flag file's mtime and resets its
    # record counter when the mtime changes, so rewriting the file re-arms.
    print(f"{GREEN}armed{RESET} {path}")
    print(f"  capture={cfg['capture']} max_records={cfg['max_records']} "
          f"expire_minutes={cfg['expire_minutes']}")
    print(f"{DIM}  takes effect within ~3s (no restart needed){RESET}")
    return 0


def cmd_off(args):
    path = flag_path(args.config_dir)
    try:
        os.unlink(path)
        print(f"{YELLOW}disarmed{RESET} {path}")
    except FileNotFoundError:
        print(f"{DIM}already off{RESET} ({path} absent)")
    return 0


def cmd_status(args):
    path = flag_path(args.config_dir)
    try:
        st = os.stat(path)
    except OSError:
        print(f"{DIM}capture: OFF{RESET} ({path} absent)")
    else:
        try:
            with open(path, errors="replace") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            cfg = {}
        minutes = float(cfg.get("expire_minutes") or 0)
        deadline = st.st_mtime + minutes * 60 if minutes > 0 else float("inf")
        left = deadline - time.time()
        state = f"{GREEN}ON{RESET}" if left > 0 else f"{YELLOW}EXPIRED{RESET}"
        remain = "no expiry" if left == float("inf") else f"{int(left)}s left"
        print(f"capture: {state}  ({remain})")
        print(f"  armed at {time.strftime('%H:%M:%S', time.localtime(st.st_mtime))}  "
              f"capture={cfg.get('capture')} max_records={cfg.get('max_records')}")

    recs = load_records(args.config_dir)
    verdicts = {}
    for r in recs:
        verdicts[r.get("verdict")] = verdicts.get(r.get("verdict"), 0) + 1
    print(f"records: {len(recs)} in {recon_dir(args.config_dir)}"
          + (f"  {verdicts}" if verdicts else ""))
    return 0


def cmd_clear(args):
    d = recon_dir(args.config_dir)
    n = 0
    try:
        names = [x for x in os.listdir(d) if x.endswith(".json")]
    except OSError:
        names = []
    for name in names:
        try:
            os.unlink(os.path.join(d, name))
            n += 1
        except OSError:
            pass
    print(f"removed {n} record(s) from {d}")
    return 0


def line_for(rec):
    verdict = rec.get("verdict") or "?"
    color = VERDICT_COLOR.get(verdict, "")
    ua = first_line(header_of(rec, "user-agent"), 28)
    routed = rec.get("routed_model") or ""
    req = rec.get("requested_model") or ""
    model = routed if routed == req else f"{req}→{routed}"
    return (f"{color}{verdict:8}{RESET} {DIM}{ts_str(rec)}{RESET} "
            f"{BOLD}{rec.get('fingerprint','')}{RESET} {model:26} "
            f"{DIM}{ua:28}{RESET} {CYAN}{first_line(rec.get('prompt_text'), 60)}{RESET}")


def cmd_watch(args):
    d = recon_dir(args.config_dir)
    seen = set()
    if not args.replay:
        try:
            seen = {n for n in os.listdir(d) if n.endswith(".json")}
        except OSError:
            pass
    print(f"{DIM}● watching {d} — Ctrl-C to stop{RESET}", file=sys.stderr)
    try:
        while True:
            try:
                names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
            except OSError:
                names = []
            for name in names:
                if name in seen:
                    continue
                seen.add(name)
                try:
                    with open(os.path.join(d, name), errors="replace") as fh:
                        rec = json.load(fh)
                except (OSError, ValueError):
                    continue
                rec["_file"] = name
                print(line_for(rec))
                if args.full:
                    print(json.dumps(rec, indent=2))
            time.sleep(POLL_SECS)
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped.{RESET}", file=sys.stderr)
    return 0


def cmd_list(args):
    recs = load_records(args.config_dir)
    if args.verdict:
        wanted = {v.strip() for v in args.verdict.split(",")}
        recs = [r for r in recs if r.get("verdict") in wanted]
    if not recs:
        print(f"{DIM}no records — arm with `recon.py on`, then run the agent{RESET}")
        return 0

    clusters = {}
    for r in recs:
        clusters.setdefault(r.get("fingerprint", "?"), []).append(r)

    print(f"{BOLD}{'count':>5}  {'fp':8} {'verdict':8} {'model':22} {'user-agent':30} first system line{RESET}")
    for fp, group in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        rec = group[-1]
        color = VERDICT_COLOR.get(rec.get("verdict"), "")
        print(f"{len(group):>5}  {BOLD}{fp:8}{RESET} {color}{(rec.get('verdict') or '?'):8}{RESET} "
              f"{(rec.get('requested_model') or ''):22} "
              f"{first_line(header_of(rec, 'user-agent'), 30):30} "
              f"{CYAN}{first_line(rec.get('prompt_text'), 70)}{RESET}")
    print(f"\n{DIM}`recon.py show <fp>` for the full request, "
          f"`recon.py suggest <fp>` for marker candidates{RESET}")
    return 0


def pick(recs, ident):
    """Resolve a fingerprint, a filename, or a filename prefix to records."""
    hits = [r for r in recs if r.get("fingerprint") == ident]
    if hits:
        return hits
    return [r for r in recs if r.get("_file", "").startswith(ident) or r.get("_file") == ident]


def cmd_show(args):
    recs = load_records(args.config_dir)
    hits = pick(recs, args.ident)
    if not hits:
        print(f"no record matches {args.ident!r}", file=sys.stderr)
        return 1
    for rec in hits[-args.count:]:
        print(f"{DIM}── {rec['_file']}{RESET}")
        print(json.dumps({k: v for k, v in rec.items() if k != "_file"}, indent=2))
    return 0


# Lines that look like per-session context rather than agent identity. A marker
# built from one of these would match one session and never again.
VOLATILE = re.compile(
    r"^(cwd|pwd|current (working )?dir|working dir|today|current date|date|"
    r"platform|os version|repo|branch|git|session|user|model)\b[:=]?|"
    r"\d{4}-\d{2}-\d{2}|/(home|root|tmp|var|usr)/"
)


def marker_candidates(text):
    """Lower-cased substrings in the exact form the hook matches.

    A good marker is an identity sentence the operator wrote and the user cannot
    forge: it sits at the start of the system/developer prompt and names the
    agent. Sentence-length prefixes are the sweet spot — long enough to be
    unique to this agent, short enough to survive prompt edits."""
    out = []
    for ln in text.splitlines():
        ln = re.sub(r"\s+", " ", ln).strip().lower()
        if not ln:
            continue
        candidate = ln.split(". ")[0][:80].rstrip(" .,:")
        if len(candidate) < 12:
            continue
        out.append((candidate, bool(VOLATILE.search(ln))))
    return out


def cmd_suggest(args):
    recs = load_records(args.config_dir)
    hits = pick(recs, args.ident)
    if not hits:
        print(f"no record matches {args.ident!r}", file=sys.stderr)
        return 1
    rec = hits[-1]

    print(f"{DIM}from {rec['_file']}  ({len(hits)} record(s) in this cluster, "
          f"verdict={rec.get('verdict')}, model={rec.get('requested_model')}){RESET}")
    ua = header_of(rec, "user-agent")
    sub = header_of(rec, "x-openai-subagent")
    if ua:
        print(f"  user-agent:        {ua}")
    if sub:
        print(f"  {GREEN}x-openai-subagent: {sub}{RESET}   "
              f"{DIM}← transport-layer signal, prefer this over a prompt marker{RESET}")

    candidates = marker_candidates(rec.get("prompt_text") or "")
    # Lines that are not identical across every capture of this agent are
    # per-session noise, whatever they look like. With two or more sessions this
    # beats any heuristic; with one, fall back to the volatility pattern.
    if len(hits) > 1:
        common = set.intersection(
            *({c for c, _ in marker_candidates(h.get("prompt_text") or "")} for h in hits)
        )
        candidates = [(c, vol or c not in common) for c, vol in candidates]

    stable = [c for c, vol in candidates if not vol][:6]
    noisy = [c for c, vol in candidates if vol][:4]

    print(f"\n{BOLD}marker candidates{RESET} {DIM}(paste into SECURITY_MARKERS / "
          f"CHEAP_MARKERS / MAIN_TURN_MARKERS){RESET}\n")
    if stable:
        for i, c in enumerate(stable):
            mark = f"  {GREEN}← best{RESET}" if i == 0 else ""
            print(f'    "{c}",{mark}')
    else:
        print(f"    {DIM}(none — every line looked session-specific; "
              f"capture another run and re-check){RESET}")
    if noisy:
        print(f"\n{DIM}  rejected as session-specific:{RESET}")
        for c in noisy:
            print(f"{DIM}    {c}{RESET}")
    print(f"\n{DIM}Then redeploy the hook (see AGENTS.md) and re-run the agent to "
          f"confirm the row is no longer unknown.{RESET}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="On-demand debug access into the LiteLLM traffic router.")
    ap.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR,
                    help=f"litellm config volume, host-side (default: {DEFAULT_CONFIG_DIR})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("on", help="arm capture")
    p.add_argument("--capture", default="unknown",
                   help="verdicts to capture: unknown,security,cheap,claude,codex,hermes,"
                        "direct or 'all' (default: unknown)")
    p.add_argument("--max", type=int, default=50, help="max records per worker (default: 50)")
    p.add_argument("--minutes", type=float, default=30, help="auto-disarm after N minutes (default: 30)")
    p.add_argument("--max-text", type=int, default=4000, help="per-field text cap (default: 4000)")
    p.set_defaults(func=cmd_on)

    p = sub.add_parser("off", help="disarm capture")
    p.set_defaults(func=cmd_off)

    p = sub.add_parser("status", help="show capture state and record count")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="live-print records as they land")
    p.add_argument("--full", action="store_true", help="dump the whole record, not one line")
    p.add_argument("--replay", action="store_true", help="also print records already on disk")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("list", help="distinct callers, clustered by fingerprint")
    p.add_argument("--verdict", help="only these verdicts (comma-separated)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="full captured request for a fingerprint or file")
    p.add_argument("ident")
    p.add_argument("--count", type=int, default=1, help="show the last N matches (default: 1)")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("suggest", help="marker candidates for a fingerprint")
    p.add_argument("ident")
    p.set_defaults(func=cmd_suggest)

    p = sub.add_parser("clear", help="delete captured records")
    p.set_defaults(func=cmd_clear)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
