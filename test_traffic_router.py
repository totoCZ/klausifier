#!/usr/bin/env python3
"""
Offline regression harness for traffic_router.py.

Drives classification, fingerprinting, recon capture and the logging hook
against a temp directory — no proxy, no network, no spend rows. Run it before
every restart; an import check does not prove routing works, and a restart on a
broken hook takes the proxy down for ~25s.

The module imports litellm, so run it with the proxy's interpreter:

    cp traffic_router.py test_traffic_router.py \
      /mnt/data/containers-backed/custom/default_litellm-config/
    incus exec litellm -- /app/.venv/bin/python /app/config/test_traffic_router.py
    rm /mnt/data/containers-backed/custom/default_litellm-config/test_traffic_router.py

Live end-to-end verification is still required for anything touching tags or
model groups — see AGENTS.md. This harness cannot see how LiteLLM builds a
spend row.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

HOOK = os.environ.get("TRAFFIC_ROUTER_PATH", "/app/config/traffic_router.py")

_tmp = tempfile.mkdtemp(prefix="recon-test-")
os.environ["TRAFFIC_ROUTER_RECON_FLAG"] = os.path.join(_tmp, "recon.json")
os.environ["TRAFFIC_ROUTER_RECON_DIR"] = os.path.join(_tmp, "recon")
FLAG = os.environ["TRAFFIC_ROUTER_RECON_FLAG"]
DIR = os.environ["TRAFFIC_ROUTER_RECON_DIR"]

_spec = importlib.util.spec_from_file_location("tr", HOOK)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

_failures = []


def check(ok, label, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        _failures.append(label)


def req(model, system=None, headers=None, **kw):
    d = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "proxy_server_request": {
            "headers": headers or {"user-agent": "test/1.0"},
            "url": "/v1/messages",
            "method": "POST",
        },
    }
    if system:
        d["system"] = system
    d.update(kw)
    return d


def call(data):
    return asyncio.run(m.handler.async_pre_call_hook(None, None, data, "completion"))


def records():
    try:
        return sorted(os.listdir(DIR))
    except OSError:
        return []


def arm(**cfg):
    with open(FLAG, "w") as fh:
        json.dump(cfg, fh)
    m._flag_checked_at = 0.0  # bypass the stat cache


MAIN = "You are Claude Code, Anthropic's official CLI for Claude."
SEC = "You are a security monitor for autonomous AI coding agents."

# --- classification -------------------------------------------------------
for label, data, exp_model, exp_verdict in [
    ("codex-auto-review", req("codex-auto-review"), "security", "security"),
    ("guardian header", req("gpt-5.6-luna", headers={"x-openai-subagent": "Guardian"}),
     "security", "security"),
    ("security marker", req("luna", SEC), "security", "security"),
    ("cheap marker", req("terra", "Generate a concise, sentence-case title"), "cheap", "cheap"),
    ("main turn", req("sol", MAIN), None, "main"),
    ("unknown luna", req("luna", "You are Fancy New Agent v3."), None, "unknown"),
    ("unknown terra (no system)", req("terra"), None, "unknown"),
    ("unknown sol", req("sol", "blah"), None, "unknown"),
    ("direct provider id", req("zai/glm-5.2", "whatever"), None, "direct"),
]:
    nm, verdict, _ = m.handler._classify(data)
    check(nm == exp_model and verdict == exp_verdict, f"classify: {label}",
          f"got model={nm!r} verdict={verdict!r}")

# system arriving as a chat-completions message, not a top-level field
chat = {"model": "luna", "messages": [{"role": "system", "content": SEC},
                                      {"role": "user", "content": "x"}]}
check(m.handler._classify(chat)[0] == "security", "classify: system as chat message")

# a user turn must never be able to imitate an identity marker
spoof = {"model": "luna", "messages": [{"role": "user", "content": SEC}]}
check(m.handler._classify(spoof)[1] == "unknown", "classify: user text cannot spoof a marker")

# --- fingerprinting -------------------------------------------------------
f1 = m._fingerprint("You are Fancy New Agent v3.\nCwd: /a\nToday is 2026-08-09.",
                    req("luna", headers={"user-agent": "newagent/1.2.3"}))
f2 = m._fingerprint("You  are   Fancy New Agent v3.\nCwd: /b\nToday is 2026-08-10.",
                    req("luna", headers={"user-agent": "newagent/9.9"}))
f3 = m._fingerprint("You are Other Agent.", req("luna", headers={"user-agent": "newagent/1.2.3"}))
check(f1 == f2, "fingerprint: stable across cwd/date/version drift", f"{f1} vs {f2}")
check(f1 != f3, "fingerprint: differs for a different agent")

# --- recon capture --------------------------------------------------------
call(req("luna", "unknown agent"))
check(not os.path.isdir(DIR), "recon: nothing captured while disarmed")

arm(capture=["unknown"], max_records=3, expire_minutes=30)
data = req("luna", "You are Fancy New Agent v3. Do things.",
           headers={"user-agent": "newagent/1.2.3", "x-openai-subagent": "guardian-ish",
                    "authorization": "Bearer sk-supersecret", "x-api-key": "sk-abc123"},
           tools=[{"function": {"name": "shell"}}], stream=True)
out = call(data)
check(out["model"] == "luna", "recon: unmatched request left on its own combo")
check(len(records()) == 1, "recon: one record captured", f"{len(records())}")

rec = json.load(open(os.path.join(DIR, records()[0])))
blob = json.dumps(rec)
check("sk-supersecret" not in blob and "sk-abc123" not in blob, "recon: secrets redacted")
check(rec["headers"]["x-openai-subagent"] == "guardian-ish", "recon: custom headers preserved")
check(rec["tools"] == ["shell"], "recon: tool names captured")
check(rec["requested_model"] == "luna" and rec["routed_model"] == "luna",
      "recon: requested and routed model recorded")

call(req("luna", MAIN))
check(len(records()) == 1, "recon: verdict filter excludes main turns")

for i in range(6):
    call(req("luna", f"unknown agent {i}"))
check(len(records()) == 3, "recon: max_records honoured", f"{len(records())}")

arm(capture=["unknown"], max_records=50, expire_minutes=1)
_old = time.time() - 3600
os.utime(FLAG, (_old, _old))
m._flag_checked_at = 0.0
call(req("luna", "another unknown"))
check(len(records()) == 3, "recon: expired flag file stops capture", f"{len(records())}")

# a corrupt flag file must still arm with defaults rather than crash a request
with open(FLAG, "w") as fh:
    fh.write("{not json")
m._flag_checked_at = 0.0
before = len(records())
call(req("luna", "corrupt-flag unknown"))
check(len(records()) == before + 1, "recon: unparseable flag file falls back to defaults")

# --- logging hook ---------------------------------------------------------
def logged(data, existing_tags=None, existing_group=None):
    call(data)
    # A main turn is never stashed, so it has no metadata bucket at all.
    meta = dict(data.get("metadata") or {})
    if existing_group:
        meta["model_group"] = existing_group
    kwargs = {"litellm_params": {"metadata": meta},
              "standard_logging_object": {"request_tags": list(existing_tags or [])}}
    asyncio.run(m.handler.async_logging_hook(kwargs, None, "completion"))
    return kwargs["standard_logging_object"], kwargs["litellm_params"]["metadata"]

slo, meta = logged(req("luna", "You are Fancy New Agent v3."), ["Credential: X"])
tags = slo["request_tags"]
check("traffic_router:unknown" in tags, "logging: unknown tag added")
check(any(t.startswith("traffic_router_fp:") for t in tags), "logging: fingerprint tag added")
check("Credential: X" in tags, "logging: pre-existing tags preserved")
check(meta.get("model_group") == "luna",
      "logging: unmatched request's own group restored", str(meta.get("model_group")))

slo, meta = logged(req("luna", SEC))
check(meta.get("model_group") == "security", "logging: rewritten group restored",
      str(meta.get("model_group")))

slo, meta = logged(req("luna", SEC), existing_group="already-set")
check(meta["model_group"] == "already-set", "logging: existing model_group not overwritten")

slo, _ = logged(req("luna", MAIN))
check(slo["request_tags"] == [], "logging: main turn left untagged")

slo, _ = logged(req("luna", "You are Fancy New Agent v3."),
                ["traffic_router:unknown", "traffic_router_fp:deadbeef"])
tags = slo["request_tags"]
check(len([t for t in tags if t.startswith("traffic_router:")]) == 1,
      "logging: stale traffic_router tags replaced, not duplicated", str(tags))

shutil.rmtree(_tmp, ignore_errors=True)
print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): " + ", ".join(_failures))
    sys.exit(1)
print("all checks passed")
