// Claude Code traffic router.
//   known background (security/title/branch) → security / cheap combos
//   known main turn (Claude Code prefix)      → untouched, stays on its combo
//   everything else (unknown)                 → unknown-<origin> sentinel combo
//                                                (a combo-ref back to the origin:
//                                                 same backends/load balancer, but
//                                                 comboName records it as unknown)
// No logging. Routing IS the classification record — call-log comboName distinguishes
// unknown-<origin> from a matched main turn on the same origin.
var b = context.body || {};
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

// Known main coding turn — must be checked first so it never falls to the unknown
// bucket. Return {} = no-op, stays on its original combo.
if (sys.indexOf("you are claude code, anthropic's official cli") !== -1) {
  return {};
}
// Subagents do real work — deliberately left untouched.
if (sys.indexOf("you are a claude agent, built on anthropic's claude agent sdk") !== -1) {
  return {};
}

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

// Unknown — route to a per-origin sentinel combo. context.combo is the resolved
// origin (luna/terra/sol); unknown-<origin> is a combo-ref back to it, so behavior
// is identical but comboName marks the request as unmatched. Fallback "luna" if
// no combo resolved.
var origin = context.combo || "luna";
return { model: "unknown-" + origin };
