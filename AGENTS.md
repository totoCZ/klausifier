# Agent Deployment Notes

## OmniRoute hook deployment

The live OmniRoute instance runs in the Incus container `omniroute`, available at
`omniroute.s.hetmer.net`. The Claude Code traffic hook is managed through
OmniRoute's middleware-hook API; do not restart the container for routine hook
updates.

### Install or update the hook

1. Copy the repository hook into the live container's temporary path:

   ```sh
   incus file push hooks/cc-background-to-luna.js omniroute/tmp/cc_background_hook.js
   ```

2. Create the hook if it does not exist. Run inside the container and build the
   request body from the copied file:

   ```sh
   incus exec omniroute -- sh -lc '
     curl -s -X POST http://localhost/api/middleware/hooks \
       -H "Content-Type: application/json" \
       -d "$(node -e '\''const fs=require("fs"); process.stdout.write(JSON.stringify({
         name:"cc-background-to-luna", priority:100, enabled:true,
         code:fs.readFileSync("/tmp/cc_background_hook.js","utf8")
       }))'\'')"
   '
   ```

3. If the API reports that the hook already exists, update it with `PUT` using
   the hook name in the URL:

   ```sh
   incus exec omniroute -- node - <<'NODE'
   const http = require("http");
   const fs = require("fs");
   const payload = JSON.stringify({
     name: "cc-background-to-luna",
     priority: 100,
     enabled: true,
     code: fs.readFileSync("/tmp/cc_background_hook.js", "utf8")
   });
   const req = http.request({
     hostname: "localhost",
     port: 80,
     path: "/api/middleware/hooks/cc-background-to-luna",
     method: "PUT",
     headers: {
       "Content-Type": "application/json",
       "Content-Length": Buffer.byteLength(payload)
     }
   }, res => {
     let body = "";
     res.on("data", chunk => body += chunk);
     res.on("end", () => {
       console.log(`Status: ${res.statusCode}`);
       console.log(body);
     });
   });
   req.on("error", error => { console.error(error); process.exitCode = 1; });
   req.end(payload);
   NODE
   ```

4. Verify the installed hook:

   ```sh
   incus exec omniroute -- curl -s http://localhost/api/middleware/hooks
   ```

The hook must only intercept requests whose routing model (`context.model`) is
one of the tier names `luna`, `terra`, or `sol`. The request body may already carry
a resolved provider ID in `context.body.model`; use `context.body.system` only for
classification. Combo resolution happens after this hook, so `context.combo` is
not a reliable origin here. Direct provider model IDs such as
`antigravity/gemini-3.1-flash-lite` must return `{}` and remain untouched. The API
update is live immediately; a container restart is not required.

### Automatic hook registration after container restart

OmniRoute stores hooks in SQLite (persistent) but does not populate the
in-memory runtime registry (`globalThis.__omniroutePreRequestRegistry`) from
the database on startup. After every restart, `registryCount` drops to 0 while
`dbCount` stays > 0 — hooks appear `enabled: true` in the API but silently stop
executing.

This is fixed via an entrypoint wrapper on the persistent data volume:

1. Place `hook-sync.js` and `entrypoint-wrapper.sh` in `/app/data/` (the
   persistent volume). Sources are in `/root/patch/omniroute/`.

2. The wrapper launches `hook-sync.js` in the background, then execs the
   original entrypoint (`/tmp/check-permissions.sh`) unchanged.

3. `hook-sync.js` polls localhost until HTTP responds, checks `registryStats`,
   and if `registryCount=0` with `dbCount>0`, re-registers each hook via PUT.

4. Set the container entrypoint to the wrapper:

   ```sh
   incus config set omniroute \
     oci.entrypoint='/app/data/entrypoint-wrapper.sh node dev/run-standalone.mjs'
   ```

Both files survive container rebuilds because they live on the data volume.
The `oci.entrypoint` config persists in Incus's database.

### Verify hook registration after a container restart

OmniRoute may fail to load hooks from the database into the runtime registry on
restart. The hook will appear `enabled: true` in the API but will silently stop
executing. Check the registry stats:

```sh
incus exec omniroute -- node -e "
  const http=require('http');
  http.get('http://localhost/api/middleware/hooks', r=>{
    let d='';
    r.on('data',c=>d+=c);
    r.on('end',()=>{
      const s=JSON.parse(d).registryStats;
      console.log(s);
      if(s.registryCount===0 && s.dbCount>0) {
        console.error('HOOK NOT LOADED — re-register via step 3 above');
        process.exitCode=1;
      }
    });
  });
"
```

If `registryCount` is 0 while `dbCount` is > 0, re-push and re-PUT the hook
(step 1 and step 3 above). A simple `PUT` with the same code forces OmniRoute
to reload the hook into the runtime registry. Confirm `registryCount` becomes 1.
