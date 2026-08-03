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

// Only intercept if the requested routing model is one of the three tiers.
var TIER_MODELS = ["luna", "terra", "sol"];
if (TIER_MODELS.indexOf(requestedModel) === -1) {
  // A direct provider model ID must remain untouched.
  return {};
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
var sys = prompt.toLowerCase();

// Background-task markers are checked BEFORE the no-op shields below. The
// security monitor is itself a Claude Agent SDK subagent, so its full system
// prompt carries the SDK identity line — if the SDK no-op ran first it would
// shield the security monitor and leave it on a premium combo. These markers
// are unambiguous (never in a real main turn), so they must win.
var SECURITY_MARKERS = [
  "you are a security monitor for autonomous ai coding agents"
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
// Working subagents do real work — deliberately left untouched. (Background
// subagents like the security monitor were already routed above.)
if (sys.indexOf("you are a claude agent, built on anthropic's claude agent sdk") !== -1) {
  return {};
}

// Unknown — route to a per-origin sentinel combo. The tier gate validated the
// requested routing model above, so it is the origin even though combo resolution
// has not happened yet.
return { model: "unknown-" + requestedModel };
