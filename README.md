# LLM Traffic Router

Routes coding-agent traffic (Claude Code, Codex CLI) through a [LiteLLM](https://github.com/BerriAI/litellm)
proxy by rewriting which model/combo each request targets — before LiteLLM
resolves it and load-balances upstream.

The hook classifies known background work (security reviews, title/branch
generation) and sends it to cheaper combos, while preserving main coding turns.
It only intercepts requests whose routing model is `luna`, `terra`, or `sol`;
direct provider model IDs and other combo names remain untouched. Only the model
name is rewritten — the request body is never modified.

## Routing behavior

The hook runs as a LiteLLM `async_pre_call_hook`, before model-group resolution.
It reads the requested model from `data["model"]`, the lower-cased inbound
headers from `data["proxy_server_request"]["headers"]`, and developer/operator
prompt text from the request body.

| Request | Signal | Destination |
| --- | --- | --- |
| Claude Code security monitor | `system` marker | `security` |
| Codex Guardian policy review | `x-openai-subagent: guardian` | `security` |
| Guardian when a proxy drops that header | Developer-prompt fallback marker | `security` |
| Codex auto review | Client-selected `codex-auto-review` model | `security` |
| Claude title or branch task | `system` marker | `cheap` |
| Claude main turn or working SDK subagent | Identity marker | unchanged |
| Codex main turn | Developer instruction identity marker | unchanged |
| Other tier request | No recognized signal | unchanged (stays on its combo) |

`security` and `cheap` are virtual names that resolve via the
`model_group_alias` in LiteLLM's `router_settings` (e.g. `security -> luna`).
Configure those aliases on the LiteLLM side; this hook only emits the names.

## Signals

Structured transport metadata is preferred over prompt matching. Guardian
routing uses the inbound header:

```text
x-openai-subagent: guardian
```

The Guardian prompt marker, `You are judging one planned coding-agent action`,
remains only as a compatibility fallback for a client or proxy that strips the
header. Preserve `x-openai-subagent` in the ingress path if Guardian routing
matters.

Other stable markers used by this hook:

- Claude security: `You are a security monitor for autonomous AI coding agents`
- Claude title: `Generate a concise, sentence-case title`
- Claude branch: `Generate a short kebab-case name`
- Claude main turn: `You are Claude Code, Anthropic's official CLI`
- Codex main turn: `You are a coding agent running in the Codex CLI` or `You are Codex, an agent based on GPT-5`

Only developer/operator-authored text (`system`, `instructions`, Responses
`input` developer messages, and `system`/`developer` roles in `messages`) is
inspected. A user message cannot imitate an identity marker to evade routing.

## Install

`traffic_router.py` lives next to the LiteLLM `config.yaml` (the config volume).
Register it via `litellm_settings.callbacks`:

```yaml
litellm_settings:
  callbacks: traffic_router.handler
```

LiteLLM resolves `traffic_router.handler` to the module-level `TrafficRouter`
instance by looking for `traffic_router.py` in the config-file directory. No
restart-time registration script is needed — LiteLLM loads it at startup from
the config. Restart the proxy to pick up changes to the hook.

## Test

```sh
incus exec litellm -- /app/.venv/bin/python -c "
import importlib.util, os
d='/app/config'; f=os.path.join(d,'traffic_router.py')
spec=importlib.util.spec_from_file_location('traffic_router', f)
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
print('loaded OK ->', mod.handler)
"
```

## History

This is a port of an earlier OmniRoute middleware hook.
`historic/cc-background-to-luna.js` is the original OmniRoute runtime hook,
preserved verbatim for reference; `historic/test-hook.js` is its regression test.
The port carries over the routing decisions but drops the OmniRoute-specific
Responses tool-protocol downgrade (LiteLLM is expected to handle tool-protocol
translation) and the `unknown-<origin>` sentinel routing (an OmniRoute-only
observability feature).
