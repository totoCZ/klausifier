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
