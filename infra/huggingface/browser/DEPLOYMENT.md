# Free interactive deployment

The browser package runs CreditLens's Python workflow in a Web Worker. Hugging Face
serves the UI, Python source, synthetic evidence and an 18 MB pinned Pyodide runtime.
Queries, calculations, citation lookup and session audits run on the visitor's device.
The server deployment and hybrid model experiments remain separate options.

The package uses Hugging Face's [Static Spaces SDK](https://huggingface.co/docs/hub/spaces-overview).
Static describes the hosting SDK; this application computes a new answer for each
question. It does not replay recorded answers or depend on a tunnel.

## Build and inspect

Use the repository's Python environment and Bun 1.3.10. Runtime assets are fetched
from the pinned Pyodide release and checked against `runtime-lock.json`; they are
then served from the Space itself. No model API or paid inference is used.

```powershell
uv sync --locked
cd apps/web
bun install --frozen-lockfile
bun run typecheck
bun test
cd ../..
uv run python -m scripts.build_browser_space --output ../creditlens-browser-release --runtime-cache ../creditlens-browser-runtime
uv run python -m http.server 8000 --bind 127.0.0.1 --directory ../creditlens-browser-release
```

Open http://127.0.0.1:8000. Verify borrower selection, new questions, DSCR results,
missing evidence, conflicting sources, prior policy dates and source inspection.
The release must be built from a clean committed tree before publishing.
Outputs use a fresh directory; existing builds are never overwritten.

## Publish to an existing free Space

Create a public **Static** Space in your own HF account. Authenticate locally with
`hf auth login`; do not put tokens in source, browser assets or command arguments.
Read the destination revision with `hf spaces info YOUR_ACCOUNT/YOUR_SPACE`.

```powershell
uv run python -m scripts.publish_browser_space --stage ../creditlens-browser-release --repo-id YOUR_ACCOUNT/YOUR_SPACE --expected-revision REVIEWED_HF_COMMIT --audit ../creditlens-browser-publication.json
```

The publisher checks ownership, public Static mode, absent compute hardware,
unchanged remote revision, local source cleanliness, synthetic fixture and every
staged hash. It replaces the reviewed app files in one parent-bound commit and
verifies immutable uploaded hashes. It never upgrades a plan or requests hardware.
For a rebuild, use a fresh stage directory and audit path. Test the actual deployed
Space after its CDN has updated. Retain the previous HF revision for rollback.

## Boundaries

- Every shipped document is public and synthetic. No restricted pages are bundled.
- Browser permission filtering is an educational demonstration, not access control
  for confidential data. Use the authenticated server for protected information.
- The browser uses BM25 and quoted evidence with deterministic Decimal calculations.
  It does not run the optional embedding model, cross-encoder, Cortex or LLM judge.
- Each tab has its own SQLite database in worker memory. Reloading resets it.
- Cold startup has a two-minute deadline, queries have a 30-second deadline, and
  cancellation suppresses stale results. Refresh borrowers retries failed startup.
- Pyodide 0.27.7 uses Python 3.12.7, Pydantic 2.10.5 and SQLAlchemy 2.0.29.
  Browser verification covers that runtime separately from the local server lock.

Pyodide is distributed under [MPL 2.0](https://github.com/pyodide/pyodide/blob/0.27.7/LICENSE).
Its [release source](https://github.com/pyodide/pyodide/tree/0.27.7) and the original
package wheels retain their licenses. The deployment manifest records exact hashes.

## Optional AWS deployment

AWS is optional. Operators can follow the [AWS reference](../../aws/README.md):
configure an account/profile and `us-east-1`, validate Terraform, bootstrap the
execution role and ECR repository, publish an immutable Docker image, review the
resource plan and its costs, then deploy and verify the API. The reference documents
IAM setup, private state handling, health checks and teardown. It is a synthetic
demo reference, not a production lender deployment or a guaranteed free tier.
