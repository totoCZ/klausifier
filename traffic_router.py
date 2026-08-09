"""
LLM traffic router — a LiteLLM proxy pre-call hook.

A CustomLogger that rewrites which model/combo a request targets, based on
inbound headers and system/developer prompt markers. Ported from the OmniRoute
hook preserved in historic/cc-background-to-luna.js. Only `data["model"]` is
rewritten; the request body is never modified (LiteLLM handles tool-protocol
translation itself, so the old Responses-downgrade mangling was not ported).

Routing table (decision order, first match wins). Tier-routed requests that are
NOT main coding turns are tagged `traffic_router:<security|cheap|unknown>` in
metadata["tags"], so each kind is distinguishable in the proxy logs UI's Tags
column even when several share one session or resolve to the same underlying
backend (e.g. security and cheap both land on deepseek-v4-flash-low). Main coding
turns are deliberately left untagged: a row without a traffic_router tag IS a
main turn.
  codex-auto-review model                       -> security   [tag: security]
  requested model not a tier (luna/terra/sol)   -> unchanged  [not tagged]
  x-openai-subagent: guardian header            -> security   [tag: security]
  system/developer marker: security monitor     -> security   [tag: security]
  system/developer marker: guardian review      -> security   [tag: security]
  system marker: title / branch                 -> cheap      [tag: cheap]
  system marker: known main coding turn         -> unchanged  [not tagged; main]
  anything else                                 -> unchanged  [tag: unknown]

`security` and `cheap` resolve via the model_group_alias in LiteLLM's
router_settings (e.g. security -> luna).

Tagging is two-phase: the pre-call hook classifies and rewrites the model, and
stashes the verdict on metadata; async_logging_hook then appends
`traffic_router:<verdict>` to the finalized standard_logging_object's
request_tags — the field the logs UI Tags column shows. The tag is written at
log time (not pre-call) because request_tags is materialized there regardless of
ingress route, which makes it work for both /v1/chat/completions and
/v1/messages. Main turns and direct model IDs are left untagged: a row without a
traffic_router tag IS a main turn (or an un-routed direct call).

Registered from config.yaml via:
  litellm_settings:
    callbacks: traffic_router.handler
"""

from typing import Any, Literal, Optional

from litellm.caching.dual_cache import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

TIER_MODELS = ("luna", "terra", "sol")

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
# Known main coding turns — left untouched so a real working turn never gets
# mistaken for background. Checked after the background markers above.
MAIN_TURN_MARKERS = (
    "you are claude code, anthropic's official cli",
    "you are a coding agent running in the codex cli",
    "you are codex, an agent based on gpt-5",
    "you are a claude agent, built on anthropic's claude agent sdk",
)


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


def _get_subagent_header(data: dict) -> Optional[str]:
    """Return the (first) x-openai-subagent header value, lower-cased, or None.

    LiteLLM exposes incoming headers at data['proxy_server_request']['headers']
    with lower-cased keys; a repeated header may arrive as a list."""
    headers = (data.get("proxy_server_request") or {}).get("headers") or {}
    val = headers.get("x-openai-subagent")
    if isinstance(val, list):
        val = val[0] if val else None
    if isinstance(val, str):
        return val.strip().lower()
    return None


def _any_in(text: str, markers) -> bool:
    """True if any substring marker is present in text."""
    return any(m in text for m in markers)


_TAG_KEY = "traffic_router_classification"
_TAG_PREFIX = "traffic_router:"


def _stash_tag(data: dict, value: str) -> None:
    """Stash the classification in metadata for async_logging_hook to read back.

    The pre-call hook classifies the request but the tag is only MADE VISIBLE in
    async_logging_hook (see below). We stash the verdict on both metadata buckets
    so the logging hook can recover it no matter which bucket survives the route
    — /v1/chat/completions keeps ``metadata``; routes in LITELLM_METADATA_ROUTES
    (/v1/messages, /responses, ...) keep ``litellm_metadata``. This key is
    internal scratch; it never needs to reach the spend row itself."""
    for bucket_name in ("metadata", "litellm_metadata"):
        bucket = data.get(bucket_name)
        if not isinstance(bucket, dict):
            bucket = {}
        bucket[_TAG_KEY] = value
        data[bucket_name] = bucket


def _read_stashed_tag(kwargs: dict) -> Optional[str]:
    """Recover the classification the pre-call hook stashed. Returns None if the
    request never went through tier routing (direct model ID) or was a main turn."""
    litellm_params = kwargs.get("litellm_params") or {}
    for bucket_name in ("metadata", "litellm_metadata"):
        bucket = litellm_params.get(bucket_name)
        if isinstance(bucket, dict) and bucket.get(_TAG_KEY):
            return bucket[_TAG_KEY]
    # litellm_params sometimes carries the buckets flat under kwargs instead.
    for bucket_name in ("metadata", "litellm_metadata"):
        bucket = kwargs.get(bucket_name)
        if isinstance(bucket, dict) and bucket.get(_TAG_KEY):
            return bucket[_TAG_KEY]
    return None


class TrafficRouter(CustomLogger):
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

        # Codex auto-review is a Guardian review subagent — route to security.
        if requested == "codex-auto-review":
            data["model"] = "security"
            _stash_tag(data, "security")
            return data

        # Only tier routing models are intercepted. A direct provider model ID
        # (e.g. "antigravity/gemini-3.1-flash-lite", "zai/glm-5.2") is left
        # alone, and not tagged — it is a direct, intentionally un-routed call.
        if requested not in TIER_MODELS:
            return data

        # From here every non-main path is tier-routed and gets a traffic_router
        # tag so each kind is distinguishable in spend logs. Determine the tag
        # (and any model rewrite), then stamp it once below.
        new_model = None
        tag = "unknown"

        # Codex Guardian identifies review requests at the transport layer.
        if _get_subagent_header(data) == "guardian":
            new_model = "security"
            tag = "security"
        else:
            prompt = _collect_prompt_text(data).lower()

            # Background-task markers win over the main-turn / SDK-identity
            # markers below. The Claude security monitor is itself an SDK
            # subagent, so its marker must take precedence.
            if _any_in(prompt, SECURITY_MARKERS):
                new_model = "security"
                tag = "security"
            elif _any_in(prompt, CHEAP_MARKERS):
                new_model = "cheap"
                tag = "cheap"
            elif _any_in(prompt, MAIN_TURN_MARKERS):
                # Recognized main coding turn — no rewrite, no tag. Main is the
                # common/baseline case; only background and unmatched requests
                # are tagged, so an untagged row in spend logs IS a main turn.
                return data
            # else: unmatched tier request — keep it on its original combo but
            # tag it unknown. This mirrors the historic hook's unknown-<origin>
            # sentinel combo (same backends, recorded as unknown) without a
            # separate model group per tier.

        if new_model is not None:
            data["model"] = new_model
        _stash_tag(data, tag)
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
        verdict = _read_stashed_tag(kwargs)
        if not verdict:
            # Direct model ID, or a main coding turn — deliberately untagged.
            return kwargs, result

        slo = kwargs.get("standard_logging_object")
        if not isinstance(slo, dict):
            return kwargs, result
        tags = slo.get("request_tags")
        if not isinstance(tags, list):
            tags = []
            slo["request_tags"] = tags
        tags = [t for t in tags if not (isinstance(t, str) and t.startswith(_TAG_PREFIX))]
        tags.append(f"{_TAG_PREFIX}{verdict}")
        slo["request_tags"] = tags
        return kwargs, result


# LiteLLM resolves `traffic_router.handler` to this module-level instance.
handler = TrafficRouter()
