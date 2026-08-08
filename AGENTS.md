# Deploy

`traffic_router.py` runs as a LiteLLM proxy pre-call hook inside the `litellm`
Incus container on this host. It is **not** run from this repo directly — the
container loads it from its config volume at startup.

## Where it lives

| In this repo | In the live container |
| --- | --- |
| `traffic_router.py` | `/app/config/traffic_router.py` |

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

# 2. Verify it imports cleanly in the container before restarting.
incus exec litellm -- /app/.venv/bin/python -c "
import importlib.util, os
d='/app/config'; f=os.path.join(d,'traffic_router.py')
spec=importlib.util.spec_from_file_location('traffic_router', f)
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
print('loaded OK ->', mod.handler)
"

# 3. Restart the proxy to pick up the changed hook.
incus restart litellm --force

# 4. Wait for readiness.
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
  "luna":     "deepseek/deepseek-v4-flash",
  "cheap":    "deepseek/deepseek-v4-flash-low",
  "security": "deepseek/deepseek-v4-flash-low"
}
```

If a tier call returns `400 Invalid model name`, the alias/group config is
missing — not a hook problem.

### End-to-end test

```sh
MK=$(incus exec litellm -- sh -c 'echo "$LITELLM_MASTER_KEY"')

# Plain unmatched request -> stays luna, tagged unknown.
incus exec litellm -- /app/.venv/bin/python -c "
import urllib.request, json
body={'model':'luna','messages':[{'role':'user','content':'plain unmatched'}],'max_tokens':5}
req=urllib.request.Request('http://[::1]:80/v1/chat/completions',
  data=json.dumps(body).encode(),
  headers={'Authorization':'Bearer $MK','Content-Type':'application/json'})
r=urllib.request.urlopen(req,timeout=30); print('HTTP', r.status)
"

# Then read the spend row: model_group + the unknown tag.
incus exec postgresql -- psql -U litellm -d litellm -c '
SELECT model_group,
       metadata->'"'"'spend_logs_metadata'"'"'->'"'"'traffic_router'"'"' AS unknown_tag
FROM "LiteLLM_SpendLogs"
ORDER BY "startTime" DESC LIMIT 1;'
```

Expected for the unmatched request above: `model_group = luna`,
`unknown_tag = "unknown"`.

To confirm the hook rewrites a matched request, send the same `luna` call with a
`system` message beginning `You are a security monitor for autonomous AI coding
agents.` — the spend row should show `model_group = security` and no unknown tag.

## Gotchas learned from live deploys

- **`routing_groups` overlap breaks ALL group loading.** LiteLLM (1.95.0)
  enforces "each model belongs to at most one group" at router init and *throws*
  on the first overlap, which aborts the entire `routing_groups` setup (every
  tier group disappears, not just the conflicting one). If `security` and
  `cheap` (or any two groups) list the same `model_name`, fix it in the Admin UI
  before restarting — keep one as a concrete group and make the other a
  `model_group_alias`. A clean-looking boot log is not proof groups loaded;
  always confirm with a live tier call.
- **Only `metadata["spend_logs_metadata"]` is persisted to the spend row.**
  Arbitrary top-level `data["metadata"]` keys are dropped when LiteLLM builds the
  spend log. Any tag the hook wants visible in spend logs must nest under
  `metadata["spend_logs_metadata"]`.
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
