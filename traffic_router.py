"""
LLM traffic router — a LiteLLM proxy pre-call hook.

A CustomLogger that rewrites which model/combo a request targets, based on
inbound headers and system/developer prompt markers. Ported from the OmniRoute
hook preserved in historic/cc-background-to-luna.js. Only `data["model"]` is
rewritten; the request body is never modified (LiteLLM handles tool-protocol
translation itself, so the old Responses-downgrade mangling was not ported).

Routing table (decision order, first match wins). Classification runs on EVERY
request regardless of the model name sent — Codex does not always send a bare
tier slug (its auto-approval reviewer requests fully-qualified ids like
'gpt-5.6-luna'), so the tier name is not used to gate classification.
  codex-auto-review model                       -> security     [tag: security]
  x-openai-subagent: guardian header            -> security     [tag: security]
  system/developer marker: security monitor     -> security     [tag: security]
  system/developer marker: guardian review      -> security     [tag: security]
  system marker: title / branch                 -> cheap        [tag: cheap]
  system marker: known coding agent             -> unchanged    [tag: <agent>]
      claude / codex / hermes (see AGENT_MARKERS)
  unmatched + bare tier slug (luna/terra/sol)   -> unchanged    [tag: unknown]
  anything else (direct provider model ID)      -> unchanged    [not tagged]

`security` and `cheap` are left as the requested model and resolved by LiteLLM
via `model_group_alias` in `router_settings` (security -> luna, cheap -> luna).
A patched `/config/update` keeps that alias map from being wiped on unrelated
router-settings edits — see the litellm patch repo.

## Finding unmatched traffic (and why there is no sentinel model group)

LiteLLM 1.95.0's logs UI cannot filter by tag, so a `traffic_router:unknown` tag
is invisible until you open an individual row. The obvious workaround —
rewriting unmatched requests to a sentinel `unknown-<tier>` model group, the
trick the original OmniRoute hook used — does NOT work here, and was tried and
reverted. The Logs filter panel only ever sends `key_alias`, `model_id`,
`end_user`, `user_id`, `team_id`, `request_id`, `session_id`, `error_code`,
`error_message` and status; `model_group` is never sent, and it is not a column
in the logs table either (it appears only in the row detail panel — the same
drilldown the sentinel was supposed to avoid). `/spend/logs/ui` accepts a
`model_group` filter, but nothing in the UI produces one.

The Model filter is populated from real deployments (label `model_name`, value
`model_id`), so making the sentinels filterable would mean creating duplicate
deployments rather than aliases — three more configs to keep in sync with the
tier deployments. Not worth it: use recon.py, or query the spend rows directly.

    SELECT to_char("startTime",'HH24:MI') t, model, request_tags
    FROM "LiteLLM_SpendLogs"
    WHERE request_tags::text LIKE '%traffic_router:unknown%'
      AND "startTime" > now() - interval '1 day'
    ORDER BY "startTime" DESC;

## Tagging

Two-phase: the pre-call hook classifies and rewrites the model and stashes the
verdict on metadata; async_logging_hook then appends `traffic_router:<verdict>`
to the finalized standard_logging_object's request_tags — the field the logs UI
Tags column shows. The tag is written at log time (not pre-call) because
request_tags is materialized there regardless of ingress route, which makes it
work for both /v1/chat/completions and /v1/messages. Direct model IDs are left
untagged: a row with NO traffic_router tag IS an un-routed direct call.
Unmatched requests additionally carry
`traffic_router_fp:<fp8>` — a stable fingerprint of the caller (see below) that
tells recurring unknown agents apart in the Tags column and correlates a spend
row with a captured recon record.

## Recon capture (on demand)

LiteLLM stores neither headers nor request structure: with
`store_prompts_in_spend_logs` off, `proxy_server_request` and `messages` are
literally `{}` in every spend row, and turning it on still records only rendered
messages. That makes writing a router profile for a new coding agent guesswork.

So the hook can dump the full inbound shape of selected requests — on demand,
never by default. It is armed by writing a flag file (default
`/app/config/recon.json`); the hook re-stats it at most every few seconds, so
the cost when disarmed is one cached stat per request. Each captured request is
written as one JSON file in the recon directory. Capture self-disables after
`max_records` captures or `expire_minutes`, so an armed session cannot run away.

Drive it with `recon.py` (in this repo) — `recon.py on`, run the agent once,
`recon.py list` / `show` / `suggest`, then add a marker below.

Flag file schema (all fields optional):
    {"capture": ["unknown"],   // verdicts to capture, or ["all"]
     "max_records": 50,        // per proxy worker process
     "expire_minutes": 30,     // measured from the flag file's mtime
     "max_text": 4000}         // per-field prompt text cap, in characters

Registered from config.yaml via:
  litellm_settings:
    callbacks: traffic_router.handler
"""

import hashlib
import json
import os
import time
from typing import Any, Literal, Optional

from litellm.caching.dual_cache import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

TIER_MODELS = ("luna", "terra", "sol", "vision")

# Substring markers, matched against lower-cased system/instruction/developer
# text. Order within each list is not significant; the SECURITY list is checked
# before CHEAP so the Claude security monitor (an SDK subagent) wins over the
# SDK identity marker.
SECURITY_MARKERS = (
    "you are a security monitor for autonomous ai coding agents",
    # Codex Guardian fallback when its x-openai-subagent header is unavailable.
    "you are judging one planned coding-agent action",
)
CHEAP_MARKERS = (
    "generate a concise, sentence-case title",
    "generate a short kebab-case name",
)
# Recognized coding agents, matched against lower-cased system/instruction/
# developer text. Each group maps to a NAMED verdict — used both as the
# spend-log tag and (in recon.py) as the cluster label — so traffic from each
# agent is distinguishable in the logs UI instead of collapsing into one
# undifferentiated "main" bucket. The request is never rewritten; it is only
# labelled. Add a group here to teach the router a new agent.
#
# Order across groups is not significant — an agent's prompt does not contain
# another agent's identity sentence — but this table is checked AFTER the
# background markers above, so the Claude security monitor (itself an SDK
# subagent) still wins as security.
AGENT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude", (
        "you are claude code, anthropic's official cli",
        "you are a claude agent, built on anthropic's claude agent sdk",
    )),
    ("codex", (
        "you are a coding agent running in the codex cli",
        "you are codex, an agent based on gpt-5",
    )),
    ("hermes", (
        # "You run on Hermes Agent (by Nous Research)." is the framework-injected
        # identity line and is stable across sessions — unlike the user-authored
        # "Soul" personality above it, which changes per deployment and must not
        # be used as a marker.
        "you run on hermes agent (by nous research)",
    )),
)
_AGENT_VERDICTS = frozenset(name for name, _ in AGENT_MARKERS)

# Verdicts that get a traffic_router:<verdict> tag in spend logs. Every
# classified request is tagged except "direct" — a bare provider model ID that
# is intentionally un-routed. So a row with NO traffic_router tag IS a direct
# call.
TAGGED_VERDICTS = _AGENT_VERDICTS | {"security", "cheap", "unknown"}


def _block_text(block) -> str:
    """Extract text from a content block that may be a string, or a dict with
    a 'text'/'content' field (Anthropic system blocks, Responses input items)."""
    if block is None:
        return ""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        if isinstance(block.get("text"), str):
            return block["text"]
        if isinstance(block.get("content"), str):
            return block["content"]
    return ""


def _collect_prompt_text(data: dict) -> str:
    """Gather all developer/operator-authored text the router is allowed to read.

    Mirrors the original hook: the Anthropic `system` field, the Responses
    `instructions` field, and developer messages in the Responses `input` array.
    `messages` system/developer entries are also included so the router works on
    OpenAI chat-completions requests that LiteLLM has normalized. User text is
    deliberately never read — it must not be able to imitate an identity marker.
    """
    parts = []

    system = data.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        parts.extend(_block_text(b) for b in system)

    instr = data.get("instructions")
    if isinstance(instr, str):
        parts.append(instr)

    inp = data.get("input")
    if isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict) or item.get("role") != "developer":
                continue
            content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(_block_text(c) for c in content)

    messages = data.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") not in ("system", "developer"):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(_block_text(c) for c in content)

    return "\n".join(p for p in parts if p)


def _headers(data: dict) -> dict:
    """Incoming HTTP headers as LiteLLM exposes them (lower-cased keys)."""
    headers = (data.get("proxy_server_request") or {}).get("headers")
    return headers if isinstance(headers, dict) else {}


def _get_subagent_header(data: dict) -> Optional[str]:
    """Return the (first) x-openai-subagent header value, lower-cased, or None.

    A repeated header may arrive as a list."""
    val = _headers(data).get("x-openai-subagent")
    if isinstance(val, list):
        val = val[0] if val else None
    if isinstance(val, str):
        return val.strip().lower()
    return None


def _any_in(text: str, markers) -> bool:
    """True if any substring marker is present in text."""
    return any(m in text for m in markers)


def _fingerprint(prompt: str, data: dict) -> str:
    """Stable 8-hex-char id for 'this kind of caller'.

    Hashes the first non-empty line of the operator-authored prompt plus the
    product token of the user-agent. Two runs of the same agent doing the same
    job collide on purpose — that is what makes recurring unknowns cluster
    instead of appearing as a hundred unrelated rows.

    Only the FIRST LINE is hashed, not a longer prefix: the identity sentence is
    always at the very start, while everything after it is per-session (cwd,
    date, git branch, file lists) and would fragment one agent into a new
    fingerprint on every request. The user-agent is reduced to its product token
    for the same reason — a version bump must not look like a new agent."""
    ua = _headers(data).get("user-agent")
    if isinstance(ua, list):
        ua = ua[0] if ua else ""
    ua = (ua or "").split("/")[0].strip().lower()
    head = ""
    for line in prompt.splitlines():
        if line.strip():
            head = " ".join(line.split())[:200]
            break
    return hashlib.sha256(f"{ua}\n{head}".encode("utf-8", "replace")).hexdigest()[:8]


_TAG_KEY = "traffic_router_classification"
_FP_KEY = "traffic_router_fingerprint"
_GROUP_KEY = "traffic_router_group"
_TAG_PREFIX = "traffic_router:"
_FP_TAG_PREFIX = "traffic_router_fp:"


def _stash(data: dict, key: str, value: str) -> None:
    """Stash a value in metadata for async_logging_hook to read back.

    The pre-call hook classifies the request but the tag is only MADE VISIBLE in
    async_logging_hook (see below). We stash on both metadata buckets so the
    logging hook can recover it no matter which bucket survives the route —
    /v1/chat/completions keeps ``metadata``; routes in LITELLM_METADATA_ROUTES
    (/v1/messages, /responses, ...) keep ``litellm_metadata``. These keys are
    internal scratch; they never need to reach the spend row itself."""
    for bucket_name in ("metadata", "litellm_metadata"):
        bucket = data.get(bucket_name)
        if not isinstance(bucket, dict):
            bucket = {}
        bucket[key] = value
        data[bucket_name] = bucket


def _metadata_buckets(kwargs: dict):
    """Yield the metadata dicts a logging-time kwargs may carry the stash in."""
    litellm_params = kwargs.get("litellm_params") or {}
    for source in (litellm_params, kwargs):
        for bucket_name in ("metadata", "litellm_metadata"):
            bucket = source.get(bucket_name)
            if isinstance(bucket, dict):
                yield bucket


def _read_stashed(kwargs: dict, key: str) -> Optional[str]:
    """Recover a value the pre-call hook stashed, or None."""
    for bucket in _metadata_buckets(kwargs):
        if bucket.get(key):
            return bucket[key]
    return None


# --------------------------------------------------------------------------
# Recon capture
# --------------------------------------------------------------------------

RECON_FLAG = os.environ.get("TRAFFIC_ROUTER_RECON_FLAG", "/app/config/recon.json")
RECON_DIR = os.environ.get("TRAFFIC_ROUTER_RECON_DIR", "/app/config/recon")

_FLAG_STAT_TTL = 3.0  # seconds between re-stats of the flag file
_RECON_DEFAULTS = {
    "capture": ["unknown"],
    "max_records": 50,
    "expire_minutes": 30,
    "max_text": 4000,
}

# Header names whose values are secrets. Matched case-insensitively, exact name
# or substring, so provider-specific variants (`x-goog-api-key`,
# `openai-organization` is fine but `...-api-key` is not) are covered.
_SECRET_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key"}
)
_SECRET_SUBSTRINGS = ("api-key", "apikey", "token", "secret", "password", "credential")

# Per-process capture state. LiteLLM may run several worker processes; each
# keeps its own counter, so `max_records` is a per-worker bound.
_flag_checked_at = 0.0
_flag_mtime: Optional[float] = None
_flag_cfg: Optional[dict] = None
_flag_deadline = 0.0
_recon_count = 0
_recon_seq = 0


def _recon_config() -> Optional[dict]:
    """Return the active recon config, or None when capture is off.

    Cheap by design: at most one stat every _FLAG_STAT_TTL seconds when the flag
    file is absent, which is the steady state. The file is re-read only when its
    mtime changes, and re-arming (touching the file) resets the record counter.
    """
    global _flag_checked_at, _flag_mtime, _flag_cfg, _flag_deadline, _recon_count

    now = time.monotonic()
    if now - _flag_checked_at < _FLAG_STAT_TTL:
        cfg = _flag_cfg
    else:
        _flag_checked_at = now
        try:
            mtime = os.stat(RECON_FLAG).st_mtime
        except OSError:
            _flag_mtime, _flag_cfg, _flag_deadline = None, None, 0.0
            return None
        if mtime != _flag_mtime:
            _flag_mtime = mtime
            _recon_count = 0
            cfg = dict(_RECON_DEFAULTS)
            try:
                with open(RECON_FLAG, "r", errors="replace") as fh:
                    text = fh.read().strip()
                loaded = json.loads(text) if text else {}
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except (OSError, ValueError):
                pass  # unreadable/!json flag file still arms with the defaults
            capture = cfg.get("capture")
            if isinstance(capture, str):
                capture = [capture]
            cfg["capture"] = {str(c).lower() for c in capture} if isinstance(capture, list) else set()
            try:
                minutes = float(cfg.get("expire_minutes") or 0)
            except (TypeError, ValueError):
                minutes = _RECON_DEFAULTS["expire_minutes"]
            # Deadline is measured from the flag file's own mtime, so an old
            # forgotten flag file is already expired when the proxy restarts.
            cfg["deadline"] = (mtime + minutes * 60.0) if minutes > 0 else float("inf")
            _flag_cfg = cfg
        cfg = _flag_cfg

    if cfg is None:
        return None
    if time.time() > cfg["deadline"]:
        return None
    try:
        if _recon_count >= int(cfg.get("max_records") or 0) > 0:
            return None
    except (TypeError, ValueError):
        pass
    return cfg


def _redact_headers(headers: dict) -> dict:
    """Copy headers with secret values replaced by a length marker.

    Everything else is kept verbatim — the custom `x-*` headers are exactly what
    a router profile needs (x-openai-subagent is how Codex Guardian identifies
    itself at the transport layer)."""
    out = {}
    for name, value in headers.items():
        lname = str(name).lower()
        if lname in _SECRET_HEADERS or any(s in lname for s in _SECRET_SUBSTRINGS):
            length = len(value) if isinstance(value, str) else 0
            out[name] = f"<redacted len={length}>"
        else:
            out[name] = value
    return out


def _shape(value, max_text: int, depth: int = 0):
    """Describe a request body value: keep small scalars, summarise bulk.

    Long strings are truncated and lists of message/content blocks are reduced
    to role + type + size, so a captured record stays readable and small while
    still showing the exact structure a client sent."""
    if isinstance(value, str):
        return value if len(value) <= max_text else value[:max_text] + f"…<+{len(value) - max_text} chars>"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        if depth >= 2:
            return f"<list len={len(value)}>"
        return [_shape(v, max_text, depth + 1) for v in value[:20]] + (
            [f"<+{len(value) - 20} more>"] if len(value) > 20 else []
        )
    if isinstance(value, dict):
        if depth >= 3:
            return f"<dict keys={sorted(value)[:10]}>"
        return {k: _shape(v, max_text, depth + 1) for k, v in value.items()}
    return f"<{type(value).__name__}>"


def _message_summary(messages, max_text: int):
    """Role + content-type + size per message, newest few kept verbatim-ish.

    The whole conversation is never captured — the router only ever reads
    operator-authored text, and dumping user turns would put real work content
    on disk for no diagnostic gain."""
    out = []
    for msg in messages[:40]:
        if not isinstance(msg, dict):
            out.append({"type": type(msg).__name__})
            continue
        content = msg.get("content")
        entry = {"role": msg.get("role")}
        if isinstance(content, str):
            entry["content"] = f"<str len={len(content)}>"
        elif isinstance(content, list):
            entry["content"] = [
                (c.get("type") if isinstance(c, dict) else type(c).__name__) for c in content[:12]
            ]
        elif content is not None:
            entry["content"] = f"<{type(content).__name__}>"
        for key in ("name", "tool_call_id"):
            if msg.get(key):
                entry[key] = msg[key]
        if msg.get("tool_calls"):
            entry["tool_calls"] = len(msg["tool_calls"])
        # Operator-authored turns are the ones that drive classification, so
        # those are captured in full (subject to max_text).
        if msg.get("role") in ("system", "developer") and isinstance(content, str):
            entry["text"] = content[:max_text]
        out.append(entry)
    if len(messages) > 40:
        out.append({"note": f"<+{len(messages) - 40} more messages>"})
    return out


def _tool_names(data: dict):
    tools = data.get("tools")
    if not isinstance(tools, list):
        return None
    names = []
    for tool in tools[:100]:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            names.append(fn["name"])
        elif tool.get("name"):
            names.append(tool["name"])
    return names


def _capture(data: dict, verdict: str, requested: str, routed: str, prompt: str, fp: str) -> None:
    """Write one recon record. Never raises into the request path."""
    global _recon_count, _recon_seq

    cfg = _recon_config()
    if cfg is None:
        return
    capture = cfg["capture"]
    if "all" not in capture and "*" not in capture and verdict not in capture:
        return
    # Only unknowns are fingerprinted for tagging; capture needs one for every
    # verdict so `recon.py list` can cluster whatever was recorded.
    fp = fp or _fingerprint(prompt, data)

    try:
        max_text = int(cfg.get("max_text") or _RECON_DEFAULTS["max_text"])
    except (TypeError, ValueError):
        max_text = _RECON_DEFAULTS["max_text"]

    try:
        psr = data.get("proxy_server_request") or {}
        messages = data.get("messages")
        record = {
            "ts": time.time(),
            "fingerprint": fp,
            "verdict": verdict,
            "requested_model": requested,
            "routed_model": routed,
            "url": psr.get("url"),
            "method": psr.get("method"),
            "headers": _redact_headers(_headers(data)),
            "key_alias": (data.get("metadata") or {}).get("user_api_key_alias")
            or (data.get("litellm_metadata") or {}).get("user_api_key_alias"),
            "body_keys": sorted(k for k in data if not k.startswith("_")),
            "prompt_text": prompt[:max_text],
            "prompt_len": len(prompt),
            "stream": data.get("stream"),
            "max_tokens": data.get("max_tokens"),
            "temperature": data.get("temperature"),
            "tools": _tool_names(data),
            "tool_choice": _shape(data.get("tool_choice"), 200),
            "message_count": len(messages) if isinstance(messages, list) else None,
            "messages": _message_summary(messages, max_text) if isinstance(messages, list) else None,
            "system": _shape(data.get("system"), max_text),
            "instructions": _shape(data.get("instructions"), max_text),
            "input": _shape(data.get("input"), max_text) if data.get("input") is not None else None,
        }
        os.makedirs(RECON_DIR, exist_ok=True)
        _recon_seq += 1
        name = f"{int(record['ts'] * 1000):015d}-{os.getpid()}-{_recon_seq:04d}-{fp}.json"
        # One file per record: no cross-worker append interleaving to worry
        # about, and `recon.py show` can hand you a single file verbatim.
        with open(os.path.join(RECON_DIR, name), "w") as fh:
            json.dump(record, fh, indent=2, default=str)
        _recon_count += 1
    except Exception:  # noqa: BLE001 — recon must never break a live request
        pass


class TrafficRouter(CustomLogger):
    def _classify(self, data: dict) -> tuple[Optional[str], str, str]:
        """Return (new_model or None, verdict, prompt_text).

        Pure classification — no mutation of `data`. `new_model` is None when
        the request keeps whatever model it asked for."""
        requested = data.get("model") or ""

        # Codex auto-review is a Guardian review subagent — route to security.
        if requested == "codex-auto-review":
            return "security", "security", ""

        # Classify EVERY request, regardless of the model name the client sent.
        # Codex does not always send a bare tier slug: its auto-approval
        # reviewer, for instance, requests a fully-qualified id like
        # 'gpt-5.6-luna'. A bare-tier-only gate here would pass that through
        # untouched and the proxy would reject it with 'Invalid model name'
        # before the Codex classification below ever ran.

        # Codex Guardian identifies review requests at the transport layer.
        if _get_subagent_header(data) == "guardian":
            return "security", "security", ""

        prompt = _collect_prompt_text(data)
        lowered = prompt.lower()

        # Background-task markers win over the main-turn / SDK-identity markers
        # below. The Claude security monitor is itself an SDK subagent, so its
        # marker must take precedence.
        if _any_in(lowered, SECURITY_MARKERS):
            return "security", "security", prompt
        if _any_in(lowered, CHEAP_MARKERS):
            return "cheap", "cheap", prompt
        for name, markers in AGENT_MARKERS:
            if _any_in(lowered, markers):
                # Recognized coding agent — no rewrite, but tagged with the
                # agent's name so its traffic is distinguishable in spend logs.
                return None, name, prompt

        # Nothing matched. A bare tier slug stays on its own combo and is
        # tagged unknown so the unmatched request is still discoverable.
        # Anything else is a direct provider model ID (e.g.
        # "antigravity/gemini-3.1-flash-lite", "zai/glm-5.2") — left alone and
        # not tagged, a direct, intentionally un-routed call.
        if requested in TIER_MODELS:
            return None, "unknown", prompt
        return None, "direct", prompt

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
        ],
    ) -> Optional[dict]:
        requested = data.get("model") or ""
        new_model, verdict, prompt = self._classify(data)

        if new_model is not None:
            data["model"] = new_model
        if verdict in TAGGED_VERDICTS:
            _stash(data, _TAG_KEY, verdict)
            # Stashing itself is what costs us model_group on the
            # /v1/chat/completions path: LiteLLM's
            # get_litellm_metadata_from_kwargs returns `litellm_metadata` if it
            # exists and only falls back to `metadata`, but that route carries
            # model_group in `metadata` — so creating a litellm_metadata bucket
            # here shadows it and the spend row logs a blank group. Stash the
            # effective group (rewritten or as requested) so async_logging_hook
            # can put it back. Requests we do not stash are unaffected, which is
            # why untagged main turns always recorded their group correctly.
            _stash(data, _GROUP_KEY, data.get("model") or "")

        fp = ""
        if verdict == "unknown":
            fp = _fingerprint(prompt, data)
            _stash(data, _FP_KEY, fp)

        # Disarmed in the steady state: _capture's first act is the cached stat.
        _capture(data, verdict, requested, data.get("model") or "", prompt, fp)
        return data

    async def async_logging_hook(
        self,
        kwargs: dict,
        result: Any,
        call_type: str,
    ) -> tuple:
        """Append `traffic_router:<verdict>` to the finalized request_tags.

        This is where the tag becomes VISIBLE — in the proxy logs UI Tags column
        and the spend-log request_tags field. It runs in async_success_handler
        AFTER LiteLLM has built the standard_logging_object, so it can edit that
        object's request_tags directly. Doing it here (rather than in the
        pre-call hook) is route-independent: it works for /v1/chat/completions
        AND /v1/messages, streaming AND non-streaming, because by this point the
        request_tags list is already materialized on the standard_logging_object
        regardless of which metadata bucket the ingress route used.

        Returns (kwargs, result) unchanged in shape; only the stashed
        request_tags list is extended. Any pre-existing traffic_router:* tag is
        replaced rather than duplicated."""
        slo = kwargs.get("standard_logging_object")
        if not isinstance(slo, dict):
            return kwargs, result

        # Restore the model group our own stash shadowed (see _GROUP_KEY).
        # The spend row reads model_group from the litellm metadata bucket
        # (spend_tracking_utils.get_logging_payload: `metadata.get("model_group")`),
        # NOT from the standard_logging_object — unlike request_tags, which
        # prefers the log object. So write it to the metadata bucket; the log
        # object is updated too for downstream callbacks. Only fills a blank —
        # never overwrites a group LiteLLM did record.
        group = _read_stashed(kwargs, _GROUP_KEY)
        if group:
            for bucket in _metadata_buckets(kwargs):
                if not bucket.get("model_group"):
                    bucket["model_group"] = group
            if not slo.get("model_group"):
                slo["model_group"] = group

        verdict = _read_stashed(kwargs, _TAG_KEY)
        if not verdict:
            # Direct provider model ID — deliberately untagged.
            return kwargs, result

        tags = slo.get("request_tags")
        if not isinstance(tags, list):
            tags = []
        tags = [
            t
            for t in tags
            if not (isinstance(t, str) and t.startswith((_TAG_PREFIX, _FP_TAG_PREFIX)))
        ]
        tags.append(f"{_TAG_PREFIX}{verdict}")
        fp = _read_stashed(kwargs, _FP_KEY)
        if fp:
            # Tells recurring unknown agents apart at a glance, and links the
            # spend row to a captured recon record with the same fingerprint.
            tags.append(f"{_FP_TAG_PREFIX}{fp}")
        slo["request_tags"] = tags
        return kwargs, result


# LiteLLM resolves `traffic_router.handler` to this module-level instance.
handler = TrafficRouter()
