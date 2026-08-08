"""
LLM traffic router — a LiteLLM proxy pre-call hook.

A CustomLogger that rewrites which model/combo a request targets, based on
inbound headers and system/developer prompt markers. Ported from the OmniRoute
hook preserved in historic/cc-background-to-luna.js. Only `data["model"]` is
rewritten; the request body is never modified (LiteLLM handles tool-protocol
translation itself, so the old Responses-downgrade mangling was not ported).

Routing table (decision order, first match wins):
  codex-auto-review model                       -> security
  requested model not a tier (luna/terra/sol)   -> unchanged (direct provider IDs)
  x-openai-subagent: guardian header            -> security
  system/developer marker: security monitor     -> security
  system/developer marker: guardian review      -> security   (header fallback)
  system marker: title / branch                 -> cheap
  system marker: known main coding turn         -> unchanged
  anything else                                 -> unchanged, but tagged unknown

`security` and `cheap` resolve via the model_group_alias in LiteLLM's
router_settings (e.g. security -> luna).

Registered from config.yaml via:
  litellm_settings:
    callbacks: traffic_router.handler
"""

from typing import Literal, Optional

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
            return data

        # Only tier routing models are intercepted. A direct provider model ID
        # (e.g. "antigravity/gemini-3.1-flash-lite", "zai/glm-5.2") is left alone.
        if requested not in TIER_MODELS:
            return data

        # Codex Guardian identifies review requests at the transport layer.
        if _get_subagent_header(data) == "guardian":
            data["model"] = "security"
            return data

        prompt = _collect_prompt_text(data).lower()

        # Background-task markers win over the main-turn / SDK-identity markers
        # below. The Claude security monitor is itself an SDK subagent, so its
        # marker must take precedence.
        for marker in SECURITY_MARKERS:
            if marker in prompt:
                data["model"] = "security"
                return data
        for marker in CHEAP_MARKERS:
            if marker in prompt:
                data["model"] = "cheap"
                return data

        # Recognized main coding turn — no-op, stays on its original combo.
        for marker in MAIN_TURN_MARKERS:
            if marker in prompt:
                return data

        # Unmatched tier request — keep it on its original combo but tag it
        # unknown for spend attribution. This mirrors the historic hook's
        # unknown-<origin> sentinel combo (same backends, but recorded as
        # unknown) without needing a separate model group per tier. LiteLLM
        # drops arbitrary top-level metadata keys when building the spend row;
        # only the nested `spend_logs_metadata` dict is persisted, so the tag
        # must go there. The model_group still names the origin (luna/terra/
        # sol) while this flags it unmatched.
        metadata = data.get("metadata") or {}
        spend_meta = metadata.get("spend_logs_metadata") or {}
        spend_meta["traffic_router"] = "unknown"
        metadata["spend_logs_metadata"] = spend_meta
        data["metadata"] = metadata
        return data


# LiteLLM resolves `traffic_router.handler` to this module-level instance.
handler = TrafficRouter()
