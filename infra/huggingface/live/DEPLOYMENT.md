# Live synthetic workbench on Hugging Face

The public Space is https://huggingface.co/spaces/chinmayarvind/creditlens.
Its app origin is https://chinmayarvind-creditlens.static.hf.space.
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
Production durable retention is separate unfinished work.

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
question, change scope/date, open a cited page and inspect mobile layout. HTTP
checks alone missed the native `fetch` receiver failure found during this release.

References: [HF Static Spaces](https://huggingface.co/docs/hub/spaces-sdks-static),
[Cloudflare Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/).
