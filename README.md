# OmniRoute Traffic Router

![logo](contrib/logo.jpg)

Route Claude Code and Codex CLI traffic through [OmniRoute](https://github.com/diegosouzapw/OmniRoute) without logging request content or rebuilding the gateway.

The live middleware hook classifies known background work, preserves main coding turns, and makes unknown tier traffic discoverable. It only intercepts requests whose routing model is `luna`, `terra`, or `sol`; direct provider model IDs and other combo names remain untouched.

## Routing behavior

The hook runs before combo resolution. It uses `context.model` as the requested tier, reads lower-case inbound headers from `context.headers`, and normalizes `context.body` whether it is an object or JSON string.

| Request | Signal | Destination |
| --- | --- | --- |
| Claude Code security monitor | `body.system` marker | `security` |
| Codex Guardian policy review | `x-openai-subagent: guardian` | `security` |
| Guardian when a proxy drops that header | Developer-prompt fallback marker | `security` |
| Claude title or branch task | `body.system` marker | `cheap` |
| Claude main turn or working SDK subagent | Identity marker | unchanged |
| Codex main turn | Developer instruction identity marker | unchanged |
| Codex auto review | Client-selected `codex-auto-review` model | unchanged |
| Other tier request | No recognized signal | `unknown-<origin>` |

Guardian and Codex auto review are separate: Guardian is a background reviewer that the hook routes to `security`; auto review already selects `codex-auto-review` and the hook leaves it alone. Keep those combos separate so each workload is observable and can use an appropriate model.

`unknown-luna`, `unknown-terra`, and `unknown-sol` should be combo references to their respective origin combos. They preserve backend behavior while making unmatched traffic visible through `summary.comboName`, without logging prompt contents.

| `comboName` | Meaning |
| --- | --- |
| `security` | Claude security monitor or Codex Guardian review |
| `cheap` | Claude title or branch request |
| `codex-auto-review` | Client-selected Codex auto-review request |
| `unknown-luna` / `unknown-terra` / `unknown-sol` | Unmatched tier request to inspect |
| `luna` / `terra` / `sol` | Recognized main coding request |

## Signals

Structured transport metadata is preferred over prompt matching. OmniRoute provides inbound headers to hooks in `context.headers` using lower-case names, so Guardian routing uses:

```text
x-openai-subagent: guardian
```

The Guardian prompt marker, `You are judging one planned coding-agent action`, remains only as a compatibility fallback for a client or proxy that strips the header. Preserve `x-openai-subagent` in the ingress path if Guardian routing matters.

Other stable markers intentionally used by this hook:

- Claude security: `You are a security monitor for autonomous AI coding agents`
- Claude title: `Generate a concise, sentence-case title`
- Claude branch: `Generate a short kebab-case name`
- Claude main turn: `You are Claude Code, Anthropic's official CLI`
- Codex main turn: `You are a coding agent running in the Codex CLI` or `You are Codex, an agent based on GPT-5`

Only developer content in Codex `body.input` is inspected. A user message cannot imitate an identity marker to evade unknown-request discovery.

## Required combos

Create these once through the OmniRoute UI or API. The reference target is an example; use the backend/model appropriate to your deployment.

| Combo | Refers to | Purpose |
| --- | --- | --- |
| `security` | `luna` | Claude security monitor and Codex Guardian target |
| `cheap` | `luna` | Claude title/branch target |
| `unknown-luna` | `luna` | Unmatched `luna` sentinel |
| `unknown-terra` | `terra` | Unmatched `terra` sentinel |
| `unknown-sol` | `sol` | Unmatched `sol` sentinel |
| `codex-auto-review` | chosen review model/combo | Codex auto-review target |

## Install or update the live hook

For this deployment, use the complete create/update commands in [AGENTS.md](AGENTS.md). From the repository root, the first step is:

```sh
incus file push hooks/cc-background-to-luna.js omniroute/tmp/cc_background_hook.js
```

Then create or update `cc-background-to-luna` through `/api/middleware/hooks`. Updates are live immediately; do not restart the OmniRoute container. Verify with:

```sh
incus exec omniroute -- curl -s http://localhost/api/middleware/hooks
```

## Test and observe

```sh
node test/test-hook.js
python3 classify.py            # unmatched requests only
python3 classify.py --all      # all requests
python3 classify.py --log-root /path/to/call_logs
```

`classify.py` reads `summary.comboName` from OmniRoute call logs and does not duplicate hook classification. `runCount` is unreliable; validate an update using routing outcomes instead.

When recurring traffic reaches an `unknown-*` combo, inspect it safely, add an unambiguous signal plus a regression test, and redeploy the hook. Prefer structured metadata when available; otherwise use a stable system/developer marker. Never route direct provider model IDs.

## Repository contents

```text
hooks/cc-background-to-luna.js  Runtime hook
test/test-hook.js               Zero-dependency regression tests
classify.py                     Call-log watcher
AGENTS.md                       Live deployment procedure
```
