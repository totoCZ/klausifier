// Claude Code traffic router.
//   known background (security/title/branch) → security / cheap combos
//   known main turn (Claude Code prefix)      → untouched, stays on its combo
//   everything else (unknown)                 → unknown-<origin> sentinel combo
//                                                (a combo-ref back to the origin:
//                                                 same backends/load balancer, but
//                                                 comboName records it as unknown)
// IMPORTANT: Only intercept requests where model is one of the three tiers (luna/terra/sol).
// If a model comes in directly (e.g., antigravity/gemini-3.1-flash-lite), leave it untouched.

// Get the requested model from the request
var requestedModel = b.model || "";

// Only intercept if the model is one of the three tiers
var TIER_MODELS = ["luna", "terra", "sol"];
if (TIER_MODELS.indexOf(requestedModel) === -1) {
  // Model is not one of our tiers - leave it untouched
  return {};
}

var raw = b.system;
var sys = "";
if (typeof raw === "string") {
  sys = raw.toLowerCase();
} else if (Array.isArray(raw)) {
  for (var i = 0; i < raw.length; i++) {
    var blk = raw[i];
    if (blk && typeof blk.text === "string") sys += "\n" + blk.text;
  }
  sys = sys.toLowerCase();
}

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
// Working subagents do real work — deliberately left untouched. (Background
// subagents like the security monitor were already routed above.)
if (sys.indexOf("you are a claude agent, built on anthropic's claude agent sdk") !== -1) {
  return {};
}

// Unknown — route to a per-origin sentinel combo. context.combo is the resolved
// origin (luna/terra/sol); unknown-<origin> is a combo-ref back to it, so behavior
// is identical but comboName marks the request as unmatched. Fallback "luna" if
// no combo resolved.
var origin = context.combo || "luna";
return { model: "unknown-" + origin };
