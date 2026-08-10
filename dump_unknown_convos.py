#!/usr/bin/env python3
"""
dump_unknown_convos.py — list unmatched tier traffic from LiteLLM spend logs.

A request the traffic router could not identify is tagged
`traffic_router:unknown` plus a stable `traffic_router_fp:<fp8>` fingerprint
(see traffic_router.py). LiteLLM's logs UI cannot filter by tag, so the only way
to find these used to be the raw SQL in the repo README. This script runs that
query for you and clusters by fingerprint, so recurring unknown agents surface
as one row instead of a hundred.

The fingerprint matches the one recon.py writes, so a captured recon record for
the same fp shows you the caller's exact inbound shape (headers, system prompt,
tools) — pass --records to open them directly.

Runs on the host. Reads spend rows from the litellm Postgres database via the
local `psql` (peer auth — just `psql -d litellm`, no user/host needed).

Usage:
    dump_unknown_convos.py                 # last 24h, unknown only
    dump_unknown_convos.py --hours 72      # wider window
    dump_unknown_convos.py --records 02b4f220   # show recon capture(s) for an fp
    dump_unknown_convos.py --rows          # one line per spend row (no clustering)
"""

import argparse
import json
import os
import subprocess
import sys
import time

# Defaults match the other host-side tools (recon.py). Override the recon dir
# with $TRAFFIC_ROUTER_CONFIG_DIR; the DB name with $LITELLM_PG_DB.
RECON_DIR = os.path.join(
    os.environ.get("TRAFFIC_ROUTER_CONFIG_DIR",
                   "/mnt/data/containers-backed/custom/default_litellm-config"),
    "recon",
)
PG_DB = os.environ.get("LITELLM_PG_DB", "litellm")

DIM, BOLD, RED, CYAN, GREEN, YELLOW, RESET = (
    "\033[2m", "\033[1m", "\033[31m", "\033[36m", "\033[32m", "\033[33m", "\033[0m"
)
if not sys.stdout.isatty():
    DIM = BOLD = RED = CYAN = GREEN = YELLOW = RESET = ""

TAG_UNKNOWN = "traffic_router:unknown"
FP_PREFIX = "traffic_router_fp:"


def psql_query(sql: str) -> list[list[str]]:
    """Run a SQL query via the local psql, return rows as lists of strings."""
    out = subprocess.run(
        ["psql", "-d", PG_DB, "-At", "-F", "\t", "-c", sql],
        check=True, capture_output=True, text=True,
    ).stdout
    rows = []
    for line in out.splitlines():
        if not line:
            continue
        rows.append(line.split("\t"))
    return rows


def parse_tags(raw: str) -> list[str]:
    """Parse a request_tags column value into a list.

    `request_tags::text` renders as a JSON array
    (`["a","b"]`) for the text[] column LiteLLM uses."""
    if not raw or raw in ("{}", "[]"):
        return []
    # JSON-array form
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        return [str(t) for t in parsed] if isinstance(parsed, list) else []
    # postgres array-literal form (`{a,b}`) — kept as a fallback
    inner = raw[1:-1]
    if not inner:
        return []
    return [t.strip('"') for t in inner.split(",")]


def _fp_of(tags: list[str]) -> str:
    for t in tags:
        if t.startswith(FP_PREFIX):
            return t[len(FP_PREFIX):]
    return ""


def list_unknown(hours: float) -> list[dict]:
    sql = (
        f"SELECT to_char(\"startTime\" at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS') AS t, "
        f"request_tags::text, model, model_group, metadata::text "
        f"FROM \"LiteLLM_SpendLogs\" "
        f"WHERE \"startTime\" > now() - interval '{hours} hours' "
        f"AND request_tags::text LIKE '%{TAG_UNKNOWN}%' "
        f"ORDER BY \"startTime\" DESC;"
    )
    out = []
    for t, tags_raw, model, group, meta_raw in psql_query(sql):
        tags = parse_tags(tags_raw)
        meta = {}
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except ValueError:
            pass
        out.append({
            "ts": t,
            "tags": tags,
            "fp": _fp_of(tags),
            "model": model,
            "model_group": group,
            "key_alias": (meta.get("user_api_key_alias")
                          if isinstance(meta, dict) else None) or "",
        })
    return out


def cmd_list(args):
    rows = list_unknown(args.hours)
    if not rows:
        print(f"{DIM}no traffic_router:unknown rows in the last {args.hours}h{RESET}")
        return 0

    if args.rows:
        print(f"{BOLD}{'time (UTC)':20} {'fp':9} {'model':22} {'group':8} "
              f"{'key':22} prompt{RESET}")
        for r in rows:
            prompt = _recon_prompt_hint(r["fp"])
            print(f"{r['ts']:20} {BOLD}{r['fp'] or '?':9}{RESET} "
                  f"{r['model']:22} {(r['model_group'] or ''):8} "
                  f"{YELLOW}{r['key_alias']:22}{RESET} {CYAN}{prompt}{RESET}")
        print(f"\n{DIM}{len(rows)} unknown row(s){RESET}")
        return 0

    # cluster by fingerprint
    clusters: dict[str, list[dict]] = {}
    for r in rows:
        clusters.setdefault(r["fp"] or "?", []).append(r)

    print(f"{BOLD}{'count':>5}  {'fp':9} {'model':22} {'key':22} "
          f"{'first':20} recon system prompt{RESET}")
    for fp, group in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        r = group[0]  # newest (rows are DESC)
        model = r["model"] or ""
        prompt = _recon_prompt_hint(fp)
        print(f"{len(group):>5}  {BOLD}{fp:9}{RESET} {model:22} "
              f"{YELLOW}{r['key_alias']:22}{RESET} {DIM}{r['ts']:20}{RESET} "
              f"{CYAN}{prompt}{RESET}")
    print(f"\n{DIM}{len(rows)} unknown row(s) across {len(clusters)} fingerprint(s) "
          f"in the last {args.hours}h{RESET}")
    if any(fp != "?" for fp in clusters):
        print(f"{DIM}`{sys.argv[0]} --records <fp>` to open the recon capture for an fp, "
              f"`--rows` for one line per spend row.{RESET}")
    return 0


def _recon_prompt_hint(fp: str, width: int = 60) -> str:
    """First system-prompt line from a recon capture for this fp, if present."""
    if not fp or fp == "?":
        return ""
    rec = _recon_record_for_fp(fp)
    if not rec:
        return "(no recon capture — run with recon armed)"
    text = rec.get("prompt_text") or ""
    first = ""
    for line in text.splitlines():
        if line.strip():
            first = " ".join(line.split())
            break
    return (first[:width] + "…") if len(first) > width else first


def _recon_record_for_fp(fp: str) -> dict | None:
    """Newest recon record matching fp, or None. Lazy-loaded by caller."""
    try:
        names = sorted(n for n in os.listdir(RECON_DIR) if n.endswith(".json"))
    except OSError:
        return None
    # filenames end in -<fp>.json; scan newest first
    for name in reversed(names):
        if name.endswith(f"-{fp}.json"):
            try:
                with open(os.path.join(RECON_DIR, name), errors="replace") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                continue
    return None


def cmd_records(args):
    """Print recon capture(s) for a fingerprint, clustered like recon.py show."""
    try:
        names = sorted(n for n in os.listdir(RECON_DIR) if n.endswith(".json"))
    except OSError:
        names = []
    hits = [n for n in names if n.endswith(f"-{args.fp}.json")]
    if not hits:
        print(f"{RED}no recon capture for fp {args.fp!r}{RESET} in {RECON_DIR}")
        print(f"{DIM}(the spend row is tagged but no capture exists for it — "
              f"re-run the caller with `recon.py on`){RESET}")
        return 1
    for name in hits[-args.count:]:
        with open(os.path.join(RECON_DIR, name), errors="replace") as fh:
            rec = json.load(fh)
        print(f"{DIM}── {name}{RESET}")
        print(json.dumps(rec, indent=2, default=str))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="List unmatched (traffic_router:unknown) traffic from LiteLLM spend logs.")
    ap.add_argument("--hours", type=float, default=24.0,
                    help="lookback window in hours (default: 24)")
    ap.add_argument("--rows", action="store_true",
                    help="one line per spend row instead of clustered-by-fingerprint")
    ap.add_argument("--records", metavar="FP", dest="records_fp", default=None,
                    help="print the recon capture(s) for a fingerprint")
    ap.add_argument("--count", type=int, default=1,
                    help="with --records: show the last N captures (default: 1)")
    args = ap.parse_args()

    if args.records_fp:
        return cmd_records(argparse.Namespace(fp=args.records_fp, count=args.count))
    return cmd_list(args)


if __name__ == "__main__":
    sys.exit(main())
