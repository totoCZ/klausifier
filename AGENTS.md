# Deploy

`traffic_router.py` runs as a LiteLLM proxy pre-call hook inside the `litellm`
Incus container on this host. It is **not** run from this repo directly — the
container loads it from its config volume at startup.

## Where it lives

| In this repo | In the live container |
| --- | --- |
| `traffic_router.py` | `/app/config/traffic_router.py` |
| `recon.py` | *(not deployed — runs on the host, reads the config volume)* |

The container's config volume `litellm-config` (Incus storage pool `containers`)
is mounted at `/app/config`. Its host-side path is:

```
/mnt/data/containers-backed/custom/default_litellm-config/
```

The proxy is registered from `config.yaml`:

```yaml
litellm_settings:
  callbacks: traffic_router.handler
```

`traffic_router.handler` resolves to the module-level `TrafficRouter` instance.
LiteLLM looks for `traffic_router.py` in the config-file directory.

## Deploy procedure

```sh
# 1. Copy the hook onto the config volume (host-side path).
cp traffic_router.py \
  /mnt/data/containers-backed/custom/default_litellm-config/traffic_router.py

# 2. Run the offline harness before restarting — an import check does not prove
#    routing, tagging or capture work, and a bad restart costs ~25s of downtime.
cp test_traffic_router.py \
  /mnt/data/containers-backed/custom/default_litellm-config/
incus exec litellm -- /app/.venv/bin/python /app/config/test_traffic_router.py
rm /mnt/data/containers-backed/custom/default_litellm-config/test_traffic_router.py

# 3. Verify it imports cleanly in the container before restarting.
incus exec litellm -- /app/.venv/bin/python -c "
import importlib.util, os
d='/app/config'; f=os.path.join(d,'traffic_router.py')
spec=importlib.util.spec_from_file_location('traffic_router', f)
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
print('loaded OK ->', mod.handler)
"

# 4. Restart the proxy to pick up the changed hook.
incus restart litellm --force

# 5. Wait for readiness.
for i in $(seq 1 25); do
  incus exec litellm -- /app/.venv/bin/python -c \
    "import urllib.request; urllib.request.urlopen('http://[::1]:80/health/readiness', timeout=2).read()" \
    2>/dev/null && { echo "READY"; break; }
  sleep 1
done
```

Restart is required — LiteLLM loads callbacks at startup, so a file copy alone
does not take effect.

## Verifying it works

The hook only fires for **tier-routed** models (`luna`/`terra`/`sol`), and only
when those names resolve to callable deployments. A live end-to-end check is the
only reliable verification — imports succeeding does not prove routing works.

### Prerequisite: tier names must be callable

In LiteLLM, a `routing_groups` group name is a strategy overlay, **not** a
callable model. For `luna` etc. to answer `/v1/chat/completions`, either the
deployments' `model_name` must be the tier, or `router_settings.model_group_alias`
must map each tier name to a callable `model_name`. The live config uses aliases:

```jsonc
// LiteLLM_Config.param_value["router_settings"]["model_group_alias"]
{
  "sol":      "zai/glm-5.2-max",
  "terra":    "zai/glm-5.2",
  "security": "cheap"
}
```

If a tier call returns `400 Invalid model name`, the alias/group config is
missing — not a hook problem.

Edit aliases through the proxy's own API rather than the DB — it merges,
applies live, and writes an audit log. `model_group_alias` is replaced
wholesale, so send the complete map:

```sh
MK=$(incus exec litellm -- sh -c 'echo "$LITELLM_MASTER_KEY"')
incus exec litellm -- /app/.venv/bin/python -c "
import urllib.request, json
alias={'sol':'zai/glm-5.2-max','terra':'zai/glm-5.2','security':'cheap'}
req=urllib.request.Request('http://[::1]:80/config/update',
  data=json.dumps({'router_settings':{'model_group_alias':alias}}).encode(),
  headers={'Authorization':'Bearer $MK','Content-Type':'application/json'})
print(urllib.request.urlopen(req,timeout=30).read().decode())
"
```

### End-to-end test

The tag must be verified on **both** ingress routes — `/v1/chat/completions`
(OpenAI format) and `/v1/messages` (Anthropic format, used by `claude-cli`).
They carry request metadata in different buckets, and a tagging bug that hits
only one route is invisible if you test only the other.

```sh
MK=$(incus exec litellm -- sh -c 'echo "$LITELLM_MASTER_KEY"')
SYS="You are a security monitor for autonomous AI coding agents."

# /v1/chat/completions  (system goes in a system message)
incus exec litellm -- /app/.venv/bin/python -c "
import urllib.request, json
body={'model':'luna','messages':[{'role':'system','content':'''$SYS'''},{'role':'user','content':'x'}],'max_tokens':5}
req=urllib.request.Request('http://[::1]:80/v1/chat/completions',
  data=json.dumps(body).encode(),
  headers={'Authorization':'Bearer $MK','Content-Type':'application/json'})
print('HTTP', urllib.request.urlopen(req,timeout=30).status)
"

# /v1/messages  (system goes in the top-level system field)
incus exec litellm -- /app/.venv/bin/python -c "
import urllib.request, json
body={'model':'luna','system':'''$SYS''','messages':[{'role':'user','content':'x'}],'max_tokens':5}
req=urllib.request.Request('http://[::1]:80/v1/messages',
  data=json.dumps(body).encode(),
  headers={'Authorization':'Bearer $MK','Content-Type':'application/json'})
print('HTTP', urllib.request.urlopen(req,timeout=30).status)
"

# Read the spend rows back: request_tags (the UI Tags column) must carry the tag.
incus exec postgresql -- psql -U litellm -d litellm -c '
SELECT model_group, request_tags, "startTime"
FROM "LiteLLM_SpendLogs"
ORDER BY "startTime" DESC LIMIT 2;'
```

Expected: both rows show `model_group = security` and a `request_tags` array
containing `"traffic_router:security"`. A main turn (a `system` message
beginning `You are Claude Code, Anthropic's official CLI`) should show
`model_group` unchanged and **no** `traffic_router:` tag. An unmatched tier
request keeps its own `model_group` and shows `"traffic_router:unknown"` plus a
`"traffic_router_fp:<fp8>"` tag.

Check `call_type` alongside `model_group`: the two ingress routes log as
`acompletion` and `anthropic_messages`, and the `model_group` restore only
matters on the former. A `security` or `cheap` row with a blank `model_group`
means that restore regressed.

### Recon capture

```sh
./recon.py status        # is capture armed? how many records?
./recon.py on            # arm (writes recon.json on the config volume)
./recon.py list          # captured callers, clustered by fingerprint
./recon.py off && ./recon.py clear
```

Runs on the host against
`/mnt/data/containers-backed/custom/default_litellm-config/` — override with
`--config-dir` or `$TRAFFIC_ROUTER_CONFIG_DIR`. Arming takes effect within ~3s
with no restart. Verify a capture landed by arming, sending an unmatched
request, and checking `./recon.py list` shows it.

## Gotchas learned from live deploys

- **`routing_groups` overlap breaks ALL group loading.** LiteLLM (1.95.0)
  enforces "each model belongs to at most one group" at router init and *throws*
  on the first overlap, which aborts the entire `routing_groups` setup (every
  tier group disappears, not just the conflicting one). If `security` and
  `cheap` (or any two groups) list the same `model_name`, fix it in the Admin UI
  before restarting — keep one as a concrete group and make the other a
  `model_group_alias`. A clean-looking boot log is not proof groups loaded;
  always confirm with a live tier call.
- **Tags go in `request_tags` via `async_logging_hook`, NOT in the pre-call hook.**
  The UI Tags column / spend-row `request_tags` are materialized at log time.
  A pre-call write to `data["metadata"]["tags"]` only survives on
  `/v1/chat/completions`; `/v1/messages` carries metadata in `litellm_metadata`
  and the tag silently vanishes. `async_logging_hook` edits
  `standard_logging_object["request_tags"]` directly, so it works for both.
  (A `spend_logs_metadata` dict persists to the spend-row metadata column but is
  **not** shown in the UI Tags column.)
- **Test both routes.** A tagging regression that hits only `/v1/messages` (the
  `claude-cli` path) is invisible if you verify only `/v1/chat/completions`.
  Confirm real client traffic with
  `metadata->>'user_api_key_alias' = 'CC Switch Claude'`.
- **`model_group` is NOT read from the standard logging object.** Unlike
  `request_tags`, the spend row takes it from the litellm metadata bucket
  (`spend_tracking_utils.get_logging_payload`). A pre-call rewrite of
  `data["model"]` blanks it on the `/v1/chat/completions` (`acompletion`) path;
  `/v1/messages` and `/responses` are unaffected. The logging hook writes it
  back into the metadata bucket — editing only the log object looks correct and
  silently does nothing.
- **Recon capture must never raise.** It runs inside the request path, so every
  capture path is wrapped in a bare `except`.
- **The slim LiteLLM image has no `curl`/`wget`.** Use
  `/app/.venv/bin/python -c "import urllib.request; ..."` for in-container HTTP.
- **`incus exec` does not propagate host env vars** into the container — read
  container env (e.g. `$LITELLM_MASTER_KEY`) with
  `incus exec litellm -- sh -c 'echo "$VAR"'`, then inline the value.
- **The container console log (`/var/log/incus/litellm/console.log`) is
  cumulative across boots.** When checking for a current-boot error, look at the
  tail / timestamp, not a grep over the whole file.

## LiteLLM container reference

- Container name: `litellm`, service URL: `litellm.s.hetmer.net:80`
- Image: `ghcr.io/berriai/litellm:latest` (plain, not `-database`)
- Backed by the shared `postgresql` container (DB + role `litellm`), so the
  spend log and config live there — query spend rows and `router_settings`
  against the `postgresql` container, not the `litellm` container.
- Runs as root; config volume needs no idmap chown.
- See the `incus-admin` skill for full container management details.
