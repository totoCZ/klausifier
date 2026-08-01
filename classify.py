#!/usr/bin/env python3
"""
OmniRoute live recon — prints traffic as it lands. Pure Python.

Watches the call-log directory directly on the host (the omniroute container's
backing dir pool is on the host filesystem, so no incus/node calls).

Does NOT re-derive any routing logic. The redirect hook (cc-background-to-luna)
already classified every request at request time and routed it:
  security/cheap            → known background (off premium)
  unknown-<origin>          → UNMATCHED request; sentinel combo-ref back to the
                              origin combo (luna/terra/sol), so identical backends
                              but comboName records it as unknown
  terra / sol / luna / auto* → matched main turn, on its real combo

So this script reads summary.comboName and labels accordingly. comboName survives
the 8KB call-log truncation, so this works on 100% of requests. Zero logging.

Usage:
  classify.py                    # live watch (fresh from now); unknowns only
  classify.py --all              # show every request with its label
  classify.py --log-root /path   # override the call-log directory
"""
import argparse, json, os, re, sys, time

# Path to the OmniRoute call-log directory. This is OmniRoute's DATA_DIR/call_logs,
# where each request is persisted as one JSON file (YYYY-MM-DD/<timestamp>.json).
# Override via --log-root or $OMNIROUTE_CALL_LOGS.
DEFAULT_LOG_ROOT = "/var/lib/incus/storage-pools/containers/custom/default_omniroute-data/call_logs"
POLL_SECS = 2.0

ROUTED_COMBOS = {"security", "cheap"}  # caught by the hook, off premium

DIM, BOLD, RED, CYAN, GREEN, YELLOW, RESET = \
    "\033[2m", "\033[1m", "\033[31m", "\033[36m", "\033[32m", "\033[33m", "\033[0m"


def extract_system(body):
    s = body.get("system")
    if isinstance(s, str):
        return s
    if isinstance(s, list):
        return "\n".join(b.get("text", "") for b in s if isinstance(b, dict))
    return ""


def read_log(path):
    try:
        with open(path, "r", errors="replace") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    body = d.get("requestBody") or {}
    summ = d.get("summary") or {}
    msgs = body.get("messages")
    return {
        "ts": summ.get("timestamp", ""),
        "combo": summ.get("comboName", ""),
        "model": summ.get("model") or body.get("model") or summ.get("requestedModel", ""),
        "status": summ.get("status", 0),
        "msgs": len(msgs) if isinstance(msgs, list) else body.get("messageCount", 0),
        "truncated": body.get("_truncated") is True,
        "system": extract_system(body)[:160],
    }


def label(combo):
    """Classify purely by where it landed (the hook's routing decision)."""
    if combo in ROUTED_COMBOS:
        return f"ROUTED→{combo}", "routed"
    if combo.startswith("unknown-"):
        return f"UNKNOWN({combo})", "unknown"
    if combo:
        return f"MAIN({combo})", "main"
    return "NO-COMBO", "unknown"


def fmt_snippet(s):
    if s.lower().startswith("x-anthropic-billing-header"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
    return re.sub(r"\s+", " ", s).strip()


def print_line(r):
    ts = r["ts"][11:19] or "??:??:??"
    lbl, kind = label(r["combo"])
    color = {"routed": GREEN, "unknown": RED, "main": DIM}.get(kind, DIM)
    snip = fmt_snippet(r["system"])[:50] if (not r["truncated"] and r["system"]) else \
           ("(truncated)" if r["truncated"] else "")
    print(f"{color}{lbl:18}{RESET} {DIM}{ts}{RESET} {r['model']:24} msgs={r['msgs']:<3} {CYAN}{snip}{RESET}")


def print_unknown(rel, r):
    ts = r["ts"][11:19] or "??:??:??"
    print(f"{RED}[UNKNOWN]{RESET} {DIM}{ts}{RESET} {BOLD}{r['combo']}{RESET} "
          f"{r['model']} | msgs={r['msgs']} status={r['status']}")
    print(f"       {DIM}file:{RESET} {rel}")
    if not r["truncated"] and r["system"]:
        print(f"       {CYAN}sys:{RESET} {fmt_snippet(r['system'])[:150]}")
    else:
        print(f"       {DIM}sys: (truncated — system stripped at 8KB cap){RESET}")
    print()


def day_dirs():
    try:
        return sorted(d for d in os.listdir(LOG_ROOT)
                      if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
    except OSError:
        return []


def files_for(dd):
    try:
        return os.listdir(os.path.join(LOG_ROOT, dd))
    except OSError:
        return []


def main():
    global LOG_ROOT
    ap = argparse.ArgumentParser(description="OmniRoute live recon — prints traffic by combo.")
    ap.add_argument("--all", action="store_true",
                    help="show every request with its label (default: unknowns only)")
    ap.add_argument("--log-root", default=os.environ.get("OMNIROUTE_CALL_LOGS", DEFAULT_LOG_ROOT),
                    help=f"call-log directory (default: {DEFAULT_LOG_ROOT}; or $OMNIROUTE_CALL_LOGS)")
    args = ap.parse_args()
    LOG_ROOT = args.log_root

    if not os.path.isdir(LOG_ROOT):
        sys.exit(f"log root not found: {LOG_ROOT}")

    seen = set()
    for dd in day_dirs():                       # seed → fresh from now
        for fn in files_for(dd):
            seen.add(f"{dd}/{fn}")

    what = "ALL traffic" if args.all else "UNKNOWN traffic"
    print(f"{DIM}● live watch on {LOG_ROOT} — printing {what} as it lands"
          f" (Ctrl-C to stop){RESET}\n", file=sys.stderr)
    try:
        while True:
            for dd in day_dirs():
                for fn in files_for(dd):
                    rel = f"{dd}/{fn}"
                    if rel in seen:
                        continue
                    seen.add(rel)
                    r = read_log(os.path.join(LOG_ROOT, dd, fn))
                    if not r:
                        continue
                    if args.all:
                        print_line(r)
                    else:
                        _, kind = label(r["combo"])
                        if kind == "unknown":
                            print_unknown(rel, r)
            time.sleep(POLL_SECS)
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped.{RESET}", file=sys.stderr)


if __name__ == "__main__":
    main()
