// Coding-agent traffic router.
//   known background (security/title/branch) → security / cheap combos
//   known main turn (Claude Code or Codex prefix) → untouched, stays on its combo
//   everything else (unknown)                 → unknown-<origin> sentinel combo
//                                                (a combo-ref back to the origin:
//                                                 same backends/load balancer, but
//                                                 comboName records it as unknown)
// IMPORTANT: Only intercept requests where model is one of the three tiers (luna/terra/sol).
// If a model comes in directly (e.g., antigravity/gemini-3.1-flash-lite), leave it untouched.

// OmniRoute exposes the incoming request body on context.body. Do not use an
// ambient `b` binding here: it is not the request body in every hook runtime.
var b = context.body || {};
// Depending on the ingress path, OmniRoute can expose the body either as an
// already-parsed object or as its raw JSON string. Normalize both forms before
// reading `system`; otherwise a valid security-monitor request silently falls
// through because `b.system` is undefined on a string.
if (typeof b === "string") {
  try {
    b = JSON.parse(b);
  } catch (e) {
    b = {};
  }
}
// The hook runs before combo resolution. context.model is the routing model the
// client requested; body.model may already contain the selected provider model.
var requestedModel = context.model || b.model || "";

// Codex auto-review is a Guardian review subagent — route to security like the
// other Guardian paths (header and prompt marker).
if (requestedModel === "codex-auto-review") {
  return { model: "security" };
}

// Only intercept if the requested routing model is one of the three tiers.
var TIER_MODELS = ["luna", "terra", "sol"];
if (TIER_MODELS.indexOf(requestedModel) === -1) {
  // A direct provider model ID must remain untouched.
  return {};
}

// Codex Guardian identifies its review requests at the transport layer. The
// request handler exposes incoming headers to hooks as lower-case keys, so use
// this precise metadata signal before inspecting prompt content. The marker
// below remains a compatibility fallback for clients/proxies that drop it.
var subagent = context.headers && context.headers["x-openai-subagent"];
if (Array.isArray(subagent)) subagent = subagent[0];
if (typeof subagent === "string" && subagent.toLowerCase() === "guardian") {
  return { model: "security" };
}

var rawSystem = b.system;
var prompt = "";
if (typeof rawSystem === "string") {
  prompt = rawSystem;
} else if (Array.isArray(rawSystem)) {
  for (var i = 0; i < rawSystem.length; i++) {
    var blk = rawSystem[i];
    if (blk && typeof blk.text === "string") prompt += "\n" + blk.text;
  }
}
if (typeof b.instructions === "string") {
  prompt += "\n" + b.instructions;
}
// Current Codex CLI Responses API requests put their persistent instructions in
// developer messages under `input`, rather than the former top-level
// `instructions` field. Read only developer content so user text cannot opt a
// request out of unknown-request discovery by imitating the identity marker.
if (Array.isArray(b.input)) {
  for (var j = 0; j < b.input.length; j++) {
    var item = b.input[j];
    if (!item || item.role !== "developer") continue;
    if (typeof item.content === "string") {
      prompt += "\n" + item.content;
    } else if (Array.isArray(item.content)) {
      for (var k = 0; k < item.content.length; k++) {
        var content = item.content[k];
        if (content && typeof content.text === "string") prompt += "\n" + content.text;
      }
    }
  }
}
var sys = prompt.toLowerCase();

// Background-task markers are checked before the no-op shields below. The
// Claude security monitor is itself an SDK subagent, so its marker must win
// over the SDK identity. Guardian normally matches the header above; its
// prompt marker is only a compatibility fallback.
var SECURITY_MARKERS = [
  "you are a security monitor for autonomous ai coding agents",
  // Guardian fallback when its x-openai-subagent header is unavailable.
  "you are judging one planned coding-agent action"
];
var CHEAP_MARKERS = [
  "generate a concise, sentence-case title",
  "generate a short kebab-case name"
];
for (var i = 0; i < SECURITY_MARKERS.length; i++) {
  if (sys.indexOf(SECURITY_MARKERS[i]) !== -1) {
    return { model: "security" };
  }
}
for (var i = 0; i < CHEAP_MARKERS.length; i++) {
  if (sys.indexOf(CHEAP_MARKERS[i]) !== -1) {
    return { model: "cheap" };
  }
}

// Known main coding turn — must be checked before the unknown bucket so it never
// gets rerouted. Return {} = no-op, stays on its original combo.
if (sys.indexOf("you are claude code, anthropic's official cli") !== -1) {
  return {};
}
if (sys.indexOf("you are a coding agent running in the codex cli") !== -1) {
  return {};
}
if (sys.indexOf("you are codex, an agent based on gpt-5") !== -1) {
  return {};
}
// Working subagents do real work — deliberately left untouched. (Background
// subagents like the security monitor were already routed above.)
if (sys.indexOf("you are a claude agent, built on anthropic's claude agent sdk") !== -1) {
  return {};
}

// Unknown — route to a per-origin sentinel combo. The tier gate validated the
// requested routing model above, so it is the origin even though combo resolution
// has not happened yet.
return { model: "unknown-" + requestedModel };
