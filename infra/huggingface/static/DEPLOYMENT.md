# Free recorded preview

The Static Space serves five intentional public synthetic API recordings. It does not run the backend or accept arbitrary questions. Opening a source reads a captured response and does not prove live authorization. Live borrower queries remain available in the local Docker workbench.

Hugging Face documents that [Static Spaces are free for everyone and do not run compute](https://huggingface.co/docs/hub/spaces-sdks-static). This package has no remote build command, hardware request, persistent storage, model inference, or paid subscription dependency. Do not replace it with a Docker or Gradio Space under the project's no-spend constraint.

From the repository root, with Bun 1.3.10 on PATH, capture a reviewed running local container. Each destination must be new and outside the code repository:

```powershell
uv run --locked python -m scripts.capture_preview --base-url http://127.0.0.1:17862 --container creditlens-hf-release --stage ../creditlens-work/artifacts/hf-release-b0de14d --output ../creditlens-work/artifacts/static-preview-captures.json
uv run --locked python -m scripts.build_static_preview build --captures ../creditlens-work/artifacts/static-preview-captures.json --output ../creditlens-work/artifacts/hf-static-release
uv run --locked python -m scripts.build_static_preview verify --output ../creditlens-work/artifacts/hf-static-release
```

Review the six-file stage, commit the UI and packaging source, and rebuild to obtain `ui_source_tree_dirty: false`. The manifest records UI and API revisions separately. Capture validates the actual running container source against its clean deployment manifest, loopback port, synthetic scope, every query body, and every cited source response. Generated recordings remain outside the code repository; publishing this small explicitly reviewed synthetic capture is intentional.

After the current HF account has been configured through `hf auth login`, the publisher uses that cached login. Install the optional locked retrieval dependencies to obtain `huggingface_hub`, or use an existing compatible local HF CLI environment. It never prints credentials. The optional IPv4 client binding works around local connection resets without disabling TLS verification:

```powershell
uv run --locked python -m scripts.publish_static_preview --repo-id chinmayarvind/creditlens --stage ../creditlens-work/artifacts/hf-static-release --audit ../creditlens-work/audit/hf-static-release.json --ipv4
```

The publisher refuses a destination outside the current account, private or compute-backed Spaces, unexpected remote files, changed staged bytes, and dirty UI snapshots. It creates only `space_sdk="static"` when absent. One upload uses the inspected parent revision and explicit inventory without deleting remote content. It then compares every public immutable file hash and records the result outside the repository. A failed audit may still contain a created Space or uploaded revision; inspect that state before retrying.

Verify the public page and recording file over HTTP after publication. Browser visual, native dialog focus, responsive behavior, and demo video require a connected browser and must be reported separately from unit or HTTP checks.
