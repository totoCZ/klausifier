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
// Responses tool-protocol downgrade. Current Codex sends tool definitions as
// an `additional_tools` input item and tool exchanges as `custom_tool_call` /
// `custom_tool_call_output` items, plus `reasoning` items. Only real OpenAI
// accepts these newer Responses-API types; older Responses backends (Meta)
// reject the whole request with "input[0] did not match any supported type".
// We downgrade to the classic types they accept:
//   additional_tools        -> top-level `tools[]` (defs become type:function)
//   custom_tool_call        -> function_call        (input stringified)
//   custom_tool_call_output -> function_call_output (output blocks -> string)
//   reasoning               -> dropped              (encrypted-only on Meta)
//
// Structured output (text.format json_schema) is left intact: both security
// backends support it natively (Meta via text.format on /responses, OpenRouter
// via response_format on /chat/completions after OmniRoute's translation). See
// Meta's structured-output docs. This runs before combo resolution, so it
// covers every backend the combo can land on. No-op for Claude/Anthropic-format
// requests (no `input`).
//
// This rewrite is NOT security-specific. The same Codex Responses payload that
// breaks Guardian breaks any tier-routed main turn that the load balancer lands
// on a Meta-class backend — Meta rejects `additional_tools` with
// "input[0] did not match any supported type". So downgradeResponsesTools() is
// called once on every tier-routed path, not only from routeSecurity().
//
// `reasoning` is dropped unconditionally: Meta replay is encrypted-only and the
// blob is backend-specific, so across the heterogeneous (OpenAI/OpenRouter/Meta)
// balancer the encrypted_content is undecryptable — and real Codex payloads
// carry none anyway. Codex regenerates chain-of-thought each turn.
function downgradeResponsesTools() {
  if (Array.isArray(b.input)) {
    var next = [];
    for (var i = 0; i < b.input.length; i++) {
      var item = b.input[i];
      if (!item || typeof item !== "object") { next.push(item); continue; }

      if (item.type === "additional_tools") {
        // hoist tool definitions out of the input array to the top-level tools[]
        if (Array.isArray(item.tools)) {
          if (!Array.isArray(b.tools)) b.tools = [];
          for (var t = 0; t < item.tools.length; t++) {
            var td = item.tools[t];
            if (!td || typeof td !== "object") continue;
            var fn = (td.type === "function") ? td
              : { type: "function", name: td.name, description: td.description || "",
                  parameters: td.parameters || { type: "object", properties: {} } };
            if (td.strict !== undefined) fn.strict = td.strict;
            b.tools.push(fn);
          }
        }
        continue; // drop the item itself
      }

      if (item.type === "custom_tool_call") {
        next.push({
          type: "function_call",
          call_id: item.call_id || item.id,
          name: item.name,
          arguments: typeof item.input === "string" ? item.input : JSON.stringify(item.input || {}),
          status: item.status
        });
        continue;
      }

      if (item.type === "custom_tool_call_output") {
        var out = item.output;
        if (Array.isArray(out)) {
          out = out.map(function (c) {
            return (c && typeof c.text === "string") ? c.text : "";
          }).join("");
        } else if (out && typeof out === "object") {
          out = JSON.stringify(out);
        } else if (typeof out !== "string") {
          out = String(out == null ? "" : out);
        }
        next.push({ type: "function_call_output", call_id: item.call_id, output: out });
        continue;
      }

      if (item.type === "reasoning") {
        continue; // encrypted-only on Meta; undecryptable across the balancer
      }

      next.push(item);
    }
    b.input = next;
  }
}

// Security route = downgrade the body, then route to the security combo.
function routeSecurity() {
  downgradeResponsesTools();
  return { model: "security" };
}

// The hook runs before combo resolution. context.model is the routing model the
// client requested; body.model may already contain the selected provider model.
var requestedModel = context.model || b.model || "";

// Codex auto-review is a Guardian review subagent — route to security like the
// other Guardian paths (header and prompt marker).
if (requestedModel === "codex-auto-review") {
  return routeSecurity();
}

// Only intercept if the requested routing model is one of the three tiers.
var TIER_MODELS = ["luna", "terra", "sol"];
if (TIER_MODELS.indexOf(requestedModel) === -1) {
  // A direct provider model ID must remain untouched.
  return {};
}

// Every tier-routed request forwards to the origin combo, whose balancer may
// land on a Meta-class Responses backend. Downgrade new Codex Responses item
// types to the classic ones those backends accept. No-op without `b.input`
// (Claude/Anthropic format). Idempotent, so the security paths' own call inside
// routeSecurity() is harmless. Direct provider IDs (returned above) are excluded.
downgradeResponsesTools();

// Codex Guardian identifies its review requests at the transport layer. The
// request handler exposes incoming headers to hooks as lower-case keys, so use
// this precise metadata signal before inspecting prompt content. The marker
// below remains a compatibility fallback for clients/proxies that drop it.
var subagent = context.headers && context.headers["x-openai-subagent"];
if (Array.isArray(subagent)) subagent = subagent[0];
if (typeof subagent === "string" && subagent.toLowerCase() === "guardian") {
  return routeSecurity();
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
    return routeSecurity();
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
