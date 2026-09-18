# Live synthetic workbench on Hugging Face

This optional deployment embeds a locally hosted service. For self-contained browser hosting, use [the browser guide](../browser/DEPLOYMENT.md).
The HTML entry page embeds the full TypeScript workbench and FastAPI service
running on the owner's computer through a Cloudflare Quick Tunnel. Requests
execute against synthetic data and source inspection calls the live evidence API.

HF's SDK metadata remains `static` because it hosts the entry page. The application
is interactive; server computation takes place on the owner's computer. No paid
HF hardware or AWS resources are provisioned. This arrangement requires the
computer, Docker and cloudflared to remain running. It is a temporary public demo,
with no availability commitment.

## Build and run

Stage an explicit, credential-scanned source allowlist outside the repository.
Use a new staging directory for each release, review its manifest, then build it:

```powershell
.venv\Scripts\python.exe -m scripts.deploy_space stage --output ..\resources\credit_lens\artifacts\live-release
docker build -t creditlens-live:release ..\resources\credit_lens\artifacts\live-release
docker run -d --name creditlens-hf-release --restart unless-stopped --read-only --cap-drop ALL --security-opt no-new-privileges --memory 512m --cpus 1 --pids-limit 128 --tmpfs /app/data:rw,noexec,nosuid,size=64m,uid=1000,gid=1000 --tmpfs /tmp:rw,noexec,nosuid,size=32m -p 127.0.0.1:17862:7860 creditlens-live:release
```

An existing container with this name must be deliberately replaced after the new
image passes readiness, query and source checks on a separate loopback port.
The example keeps demo storage ephemeral. Restarting loses its synthetic audit
database; copy required evidence before replacing or restarting the container.
Use explicit persistent storage if local audit retention is required.

For the optional CPU model variant, build the reviewed stage with
`--target neural-runtime`. Use a separate container name and port during review:

```powershell
docker build --target neural-runtime -t creditlens-neural:release ..\resources\credit_lens\artifacts\live-release
$modelDirectory = (Resolve-Path ..\resources\credit_lens\local-models).Path
docker run -d --name creditlens-neural-release --restart unless-stopped --read-only --cap-drop ALL --security-opt no-new-privileges --memory 2g --cpus 4 --pids-limit 128 --tmpfs /app/data:rw,noexec,nosuid,size=64m,uid=1000,gid=1000 --tmpfs /tmp:rw,noexec,nosuid,size=64m --mount "type=bind,source=$modelDirectory,target=/models,readonly" -e TOKENIZERS_PARALLELISM=false -e OPENBLAS_NUM_THREADS=1 -p 127.0.0.1::7860 creditlens-neural:release
docker port creditlens-neural-release
```

Use the reported loopback port for the candidate tunnel. This image loads the
verified offline models before readiness; allow up to 120 seconds at startup.
Before making that candidate the live backend, recreate it with an explicit host
port, for example `-p 127.0.0.1:51651:7860`, and test an actual container restart.
Docker can assign a different port after restarting a container created with an
empty host port. A tunnel pointing to the previous port then returns 502 even
though the application is healthy. Random ports are for candidate checks only.
The public entrypoint ignores ambient provider settings and always uses synthetic
memory evidence and local SQLite. See [model setup](../../retrieval/README.md).
Verify actual model execution in the protected SQLite audit's
`search_provider_mode` field. The packet's `local-extractive` provider label
describes how answers are assembled, not which retrieval model is running.

Start cloudflared with `tunnel --url http://127.0.0.1:17862 --no-autoupdate --protocol http2`.
On Windows, launch the helper with `Start-Process -WindowStyle Hidden` and redirect
output to a private evidence directory. Record its PID and generated HTTPS origin.
Do not route a real-data service through this public demonstration tunnel.

## Publish or update the entry page

Use cached Hugging Face authentication with repository write access. Never put
tokens in command arguments, source files or chat. The previous verified release
audit identifies the exact remote revision, file inventory and hashes:

```powershell
.venv\Scripts\python.exe -m scripts.publish_live_space --origin https://YOUR-TUNNEL.trycloudflare.com --repo-id chinmayarvind/creditlens --previous-audit ..\resources\credit_lens\audit\hf-live-release.json --audit ..\resources\credit_lens\audit\hf-live-next-release.json
```

The publisher verifies live readiness and two fresh request IDs, rejects paid
hardware, verifies the prior remote files, then makes one revision-bound commit.
It publishes only the card and iframe page and removes the previous recorded
assets. After a tunnel restart, publish its new origin using the last verified
audit. A changed remote revision or inventory requires inspection before retrying.

Pass `--demo-gif` with the reviewed synthetic browser recording's GIF to include
`demo.gif` and show it on the Space card. The publisher decodes and bounds the
animation and records its exact hash. Include the option on later releases to
retain that media; publication uses an exact inventory and removes omitted assets.

Verify the actual HF app in a browser: load authorized borrowers, submit a custom
question, change scope/date, open a cited page and inspect mobile layout. Check browser behavior separately from HTTP health checks.

## Recover an unavailable tunnel

A Quick Tunnel is temporary. If its hostname stops resolving, the HF entry page
can still open while its embedded workbench fails. Check the named container with
`docker ps -a`, its logs, and loopback `/ready`. Restart that existing demo
container when needed, start a new tunnel, and publish its new origin using the
latest verified publication audit. Retain the reviewed GIF with `--demo-gif`.
Finally test the HF app in a browser. An existing cloudflared process alone does
not prove the hostname or backend is available. This recovery is manual; no
automatic hostname rotation or computer wake/restart service is installed.

References: [HF Static Spaces](https://huggingface.co/docs/hub/spaces-sdks-static),
[Cloudflare Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/).
