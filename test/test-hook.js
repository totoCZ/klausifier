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
const TITLE = "Generate a concise, sentence-case title";
const KEBAB = "Generate a short kebab-case name";

function context(model, bodyModel, system) {
  return { model: model, body: { model: bodyModel, system: system } };
}

function codexContext(model, bodyModel, instructions) {
  return { model: model, body: { model: bodyModel, instructions: instructions } };
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
  "security marker wins over SDK identity",
  context("luna", "provider/model", SDK + "\n" + SECURITY),
  { model: "security" }
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
  "Codex auto-review combo remains independent of the tier hook",
  codexContext("codex-auto-review", "provider/model", "You are judging one planned coding-agent action."),
  {}
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

console.log("all hook routing tests passed");
