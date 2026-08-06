#!/usr/bin/env node

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const hookPath = path.join(__dirname, "..", "hooks", "cc-background-to-luna.js");
const source = fs.readFileSync(hookPath, "utf8");
const sandbox = {};
vm.createContext(sandbox);
const hook = vm.runInContext("(function (context) {\n" + source + "\n})", sandbox);

const MAIN = "You are Claude Code, Anthropic's official CLI";
const CODEX = "You are a coding agent running in the Codex CLI, a terminal-based coding assistant.";
const SDK = "You are a Claude Agent, built on Anthropic's Claude Agent SDK";
const SECURITY = "You are a security monitor for autonomous AI coding agents";
const GUARDIAN = "You are judging one planned coding-agent action.";
const TITLE = "Generate a concise, sentence-case title";
const KEBAB = "Generate a short kebab-case name";

function context(model, bodyModel, system) {
  return { model: model, body: { model: bodyModel, system: system } };
}

function codexContext(model, bodyModel, instructions) {
  return { model: model, body: { model: bodyModel, instructions: instructions } };
}

function currentCodexContext(model, bodyModel, content) {
  return {
    model: model,
    body: {
      model: bodyModel,
      input: [{ role: "developer", content: [{ type: "input_text", text: content }] }]
    }
  };
}

function expect(name, input, expected) {
  const actual = hook(input);
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(actual)),
    expected,
    name
  );
  console.log("ok - " + name);
}

expect(
  "resolved provider body model still routes branch naming to cheap",
  context("luna", "glm/glm-5-turbo", [
    { type: "text", text: MAIN },
    { type: "text", text: KEBAB }
  ]),
  { model: "cheap" }
);
expect(
  "resolved provider body model routes title generation to cheap",
  context("terra", "glm/glm-4.7", TITLE),
  { model: "cheap" }
);
expect(
  "resolved provider body model routes security monitor",
  context("sol", "provider/model", SECURITY),
  { model: "security" }
);
expect(
  "JSON-encoded body routes security monitor",
  { model: "luna", body: JSON.stringify({ model: "provider/model", system: [
    { type: "text", text: SECURITY }
  ] }) },
  { model: "security" }
);
expect(
  "security marker wins over SDK identity",
  context("luna", "provider/model", SDK + "\n" + SECURITY),
  { model: "security" }
);
expect(
  "Guardian prompt marker remains a fallback",
  currentCodexContext("terra", "provider/model", GUARDIAN),
  { model: "security" }
);
expect(
  "Guardian header routes security monitor without prompt marker",
  {
    model: "terra",
    headers: { "x-openai-subagent": "guardian" },
    body: { model: "provider/model", input: [] }
  },
  { model: "security" }
);
expect(
  "Guardian header cannot redirect a direct provider request",
  {
    model: "provider/model",
    headers: { "x-openai-subagent": "guardian" },
    body: { model: "provider/model", input: [] }
  },
  {}
);
expect(
  "direct provider request with marker remains untouched",
  context("antigravity/gemini-3.1-flash-lite", "antigravity/gemini-3.1-flash-lite", KEBAB),
  {}
);
expect(
  "direct provider request without marker remains untouched",
  context("glm/glm-5-turbo", "glm/glm-5-turbo", "unrecognized"),
  {}
);
expect(
  "Codex auto-review routes to security",
  codexContext("codex-auto-review", "provider/model", "You are judging one planned coding-agent action."),
  { model: "security" }
);
expect(
  "body model remains a compatibility fallback",
  { body: { model: "luna", system: KEBAB } },
  { model: "cheap" }
);
expect(
  "main Claude Code turn remains untouched",
  context("terra", "provider/model", MAIN),
  {}
);
expect(
  "main Codex turn remains untouched",
  codexContext("terra", "provider/model", CODEX),
  {}
);
expect(
  "Codex instructions do not fall through to the unknown sentinel",
  codexContext("sol", "provider/model", "Before the coding instructions, include setup details.\n" + CODEX),
  {}
);
expect(
  "current Codex Responses API developer identity remains untouched",
  currentCodexContext("terra", "provider/model", "You are Codex, an agent based on GPT-5. You and the user share one workspace."),
  {}
);
expect(
  "Codex identity in user input does not bypass unknown discovery",
  {
    model: "terra",
    body: {
      model: "provider/model",
      input: [{ role: "user", content: [{ type: "input_text", text: "You are Codex, an agent based on GPT-5." }] }]
    }
  },
  { model: "unknown-terra" }
);
expect(
  "working SDK subagent remains untouched",
  context("sol", "provider/model", SDK),
  {}
);
expect(
  "unmatched luna request uses luna sentinel",
  context("luna", "provider/model", "unrecognized"),
  { model: "unknown-luna" }
);
expect(
  "unmatched terra request uses terra sentinel",
  context("terra", "provider/model", "unrecognized"),
  { model: "unknown-terra" }
);
expect(
  "unmatched sol request uses sol sentinel",
  context("sol", "provider/model", "unrecognized"),
  { model: "unknown-sol" }
);
expect(
  "missing body and model remain untouched",
  {},
  {}
);

// --- Guardian tool-protocol downgrade -------------------------------------
// Current Codex sends additional_tools / custom_tool_call / custom_tool_call_output /
// reasoning input items. Only real OpenAI accepts these; older Responses backends
// (Meta) reject them. The hook must downgrade to the classic protocol while
// leaving structured output (text.format) intact — both security backends
// support json_schema natively.
(function () {
  var ctx = {
    model: "terra",
    headers: { "x-openai-subagent": "guardian" },
    body: {
      model: "provider/model",
      text: { format: { type: "json_schema", schema: { type: "object" } } },
      input: [
        { type: "additional_tools", role: "developer",
          tools: [
            { type: "custom", name: "exec", description: "run js", parameters: { type: "object" } },
            { type: "function", name: "wait", strict: true }
          ] },
        { type: "reasoning", id: "rs_1", summary: [{ type: "summary_text", text: "low risk" }] },
        { type: "custom_tool_call", call_id: "call_1", name: "exec",
          input: { cmd: "ls" }, status: "completed" },
        { type: "custom_tool_call_output", call_id: "call_1",
          output: [{ type: "input_text", text: "result line 1" }, { type: "input_text", text: " x" }] }
      ]
    }
  };
  var result = hook(ctx);
  assert.strictEqual(result.model, "security", "new-protocol guardian routes to security");

  // text.format preserved (both backends support json_schema natively)
  assert.ok(ctx.body.text && ctx.body.text.format, "text.format structured output preserved");

  // additional_tools hoisted to top-level tools[], all downgraded to type:function
  assert.ok(Array.isArray(ctx.body.tools), "top-level tools[] created");
  assert.strictEqual(ctx.body.tools.length, 2, "both tool defs hoisted");
  ctx.body.tools.forEach(function (t) {
    assert.strictEqual(t.type, "function", "tool def is classic function type");
  });
  assert.strictEqual(ctx.body.tools[0].name, "exec", "exec tool preserved");
  assert.strictEqual(ctx.body.tools[1].strict, true, "strict flag preserved");

  var remaining = ctx.body.input.map(function (it) { return it && it.type; });
  assert.strictEqual(remaining.indexOf("additional_tools"), -1, "additional_tools item removed");
  assert.strictEqual(remaining.indexOf("custom_tool_call"), -1, "custom_tool_call downgraded");
  assert.strictEqual(remaining.indexOf("custom_tool_call_output"), -1, "custom_tool_call_output downgraded");
  assert.strictEqual(remaining.indexOf("reasoning"), -1, "reasoning item dropped");

  var fc = ctx.body.input.find(function (it) { return it && it.type === "function_call"; });
  assert.ok(fc, "function_call present");
  assert.strictEqual(fc.call_id, "call_1", "function_call call_id preserved");
  assert.strictEqual(fc.name, "exec", "function_call name preserved");
  assert.strictEqual(fc.arguments, '{"cmd":"ls"}', "function_call input stringified");

  var fco = ctx.body.input.find(function (it) { return it && it.type === "function_call_output"; });
  assert.ok(fco, "function_call_output present");
  assert.strictEqual(fco.call_id, "call_1", "function_call_output call_id preserved");
  assert.strictEqual(fco.output, "result line 1 x", "output blocks joined to string");

  console.log("ok - guardian new-protocol tools downgraded, structured output preserved");
})();

// A guardian request with only classic types is a pure routing no-op on the body.
(function () {
  var ctx = {
    model: "terra",
    headers: { "x-openai-subagent": "guardian" },
    body: { model: "provider/model", input: [
      { type: "message", role: "user", content: [{ type: "input_text", text: "hi" }] }
    ] }
  };
  var result = hook(ctx);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(result)), { model: "security" });
  assert.strictEqual(ctx.body.input.length, 1, "no items added or removed");
  console.log("ok - guardian with classic types left structurally unchanged");
})();

console.log("all hook routing tests passed");
