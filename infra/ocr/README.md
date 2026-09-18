# Local scanned-document experiments

This offline CPU experiment is separate from the live demo. It does not expose a
file-upload endpoint. The API environment does not import Paddle or load OCR models.

Create a separate Python 3.11 environment and install the Windows dependency lock:

```powershell
uv venv ../resources/credit_lens/.venv-ocr --python 3.11
uv pip sync --python ../resources/credit_lens/.venv-ocr/Scripts/python.exe --require-hashes --link-mode copy infra/ocr/requirements-win.lock
uv pip check --python ../resources/credit_lens/.venv-ocr/Scripts/python.exe
```

The Windows lock uses PaddleOCR 3.7.0, PaddlePaddle CPU 3.3.1 and PaddleX
3.7.2. The lock targets Windows, not Linux. No remote inference API is used.

`scripts/build_scanned_fixtures.py` creates four image-only, one-page synthetic
PDFs: a financial table, two-column policy, blurred/rotated table and blank page.
It writes the expected values separately from source images and records all
hashes. A local Arial font path is required and its hash is recorded. The clean
financial fixture contains eight numeric cells. These are printed scans, not
handwriting or real lender documents.

`Dockerfile.render` runs Poppler outside the application. Use `--network none`,
`--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, explicit memory,
CPU and PID limits, read-only input and a dedicated writable output mount. Render
at 144 DPI. Use only trusted operator inputs with this native pipeline.

The model snapshots used by the probe are:

| Official repository | Revision |
| --- | --- |
| PaddlePaddle/PP-DocLayoutV3 | 7b48a7566925fa464281f930c58eee04fe2c862a |
| PaddlePaddle/PaddleOCR-VL-1.6 | c5630abae1d940eafe0697512a0325494b02ab42 |

Store snapshots outside the repository. Download those exact revisions, excluding
repository Python code, and record each downloaded file's SHA-256 in
`<model-name>-manifest.json` beside its directory. `probe.py` verifies those
manifests and uses the installed Paddle implementation with local safetensors,
native CPU inference, four threads and a 1024-token generation bound. A caller
must supervise the process tree with a wall-clock limit; the probe itself is an
experiment, not a production worker. `supervise.py` implements a Windows process-tree
deadline and log-size bound and removes cloud credentials from the child environment.

`creditlens.ocr.normalize_vl` validates bounded vendor JSON, geometry and reading
order and preserves markup as data. It requires the trusted renderer's PDF/image
hashes and trusted document metadata. It discards metadata text, retains physical
page scope, and sets extracted page confidence to zero. All normalized artifacts
require review. Layout scores do not establish text recognition confidence. A
caller must establish generation completion before normalization; merely reaching
the token limit does not satisfy that requirement. The probe now observes native
token sequences before decoding: every sequence must reach EOS strictly before
the cap, with only padding afterwards. Missing EOS, a cap-length sequence or an
unsupported generation configuration fails closed. `completion.json` retains the
actual token IDs; `measurement.json` reports page-level completion. An incomplete
VL result exits with code 2 and must not be normalized. Zero recognized sequences
do not establish readable evidence. The probe does not publish pages. The opt-in worker below executes durable OCR
jobs and stages completed artifacts for review.

Run the platform-specific experiment tests separately with
`python -m pytest infra/ocr/tests`. The core normalization tests are in
`tests/test_ocr.py` and do not load models.

For the diagnostic PP-OCRv6 engine, pass `--engine ocr-v6` to the supervisor. It is not automatic admission or a replacement for scoped review.

Official contracts: [PaddleOCR-VL](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PaddleOCR-VL.en.md)
and [PP-DocLayoutV3](https://huggingface.co/PaddlePaddle/PP-DocLayoutV3).

## Reviewed admission

The server now supports durable OCR review on the configured PostgreSQL catalog.
Run offline recognition first and save an `OcrDocument` JSON object with a `pages`
array of normalized `ScannedPage` records. These retain PDF, rendered image, raw
output and model hashes. A trusted operator registers an OCR manifest and stages
its PDF with the existing `submit` command, then runs:

```powershell
python scripts/ingest_documents.py --source-root <source-root> stage-ocr --job-id <job-id> --subject <admin-subject> --input <ocr-document.json>
python scripts/ingest_documents.py --source-root <source-root> show-ocr --job-id <job-id> --subject <admin-subject>
python scripts/ingest_documents.py --source-root <source-root> review-ocr --job-id <job-id> --subject <admin-subject> --input <decision.json>
```

Use the returned `artifact_sha256` in the decision:

```json
{"artifact_sha256":"<returned hash>","decision":"approve","reason":"Compared every page against the scanned source"}
```

Use `reject` to terminate without publication. Optional `corrected_text` supplies
one complete replacement string for every physical page, in order. Corrections
cannot change borrower, ACL, dates, document identity or physical page numbers.
The reviewer must actually inspect the source; a model confidence score is not approval.

The same scoped review is available through GET and POST
`/api/v1/admin/index-jobs/{job_id}/review`. GET returns the artifact and decision
hash. POST accepts the decision above. Ordinary demo users receive 403. The HTTP
body limit remains 16 KiB; use the trusted CLI for larger corrections. Artifact
and CLI decision payloads are bounded at 8 MB. Review is operator/API based, with
no public upload or browser review interface.

Approval locks current reviewer and submitter grants, publishes the batch, advances
the catalog revision and completes the job in one transaction. Original extraction,
reviewer, grant revision, reason and corrections remain in the job result; the job
update time records completion. The parser suffix `-human-reviewed` and admission
confidence 1 indicate human attestation, not measured OCR accuracy. Repeated
terminal decisions return 409. Legacy hash-only quarantines need a new staged job.
Publication requires explicit review.

## Opt-in durable OCR worker

The existing worker can now claim OCR jobs when a native extractor is explicitly
configured. It reads the staged source under the current SQL lease, renders every
physical page in a pinned Poppler container, runs local recognition, verifies all
EOS records and source/image hashes, and atomically stages `REVIEW_REQUIRED`.
The catalog remains unchanged until the existing scoped review approves publication.
A broker notification can be acknowledged after durable quarantine; duplicate
notifications do not repeat OCR or bypass review. Digital-only workers continue to
leave OCR jobs alone.

From the repository root, with the existing PostgreSQL ingestion settings configured:

```powershell
python -m scripts.ingest_documents --source-root <private-source-root> work-one --image <digital-parser-image-sha256> --job-id <job-id> --ocr-python <ocr-venv-python.exe> --ocr-models <verified-model-root> --ocr-renderer <poppler-image-sha256> --ocr-output <private-artifact-directory> --ocr-timeout 900
```

The same four OCR settings are supported on `work-loop` and `work-queue`.
Run as a module as shown so the optional repository-local OCR implementation is
importable. The API environment does not load Paddle; only the configured child
interpreter does. The child receives no cloud or database environment variables.

This Windows path accepts at most eight pages and 25 MB of source PDF, with a
30-1800 second total deadline, a 60-second rendering deadline and a 64 MB retained
output bound per attempt. The parent renews ownership during child execution and
terminates its owned process tree if permission or lease checks fail. Rendered page
count must match the immutable manifest. Raw results and normalized artifacts are
retained under a unique attempt directory for review.

Use this native path only for trusted synthetic/operator inputs. PDF rendering is
container-isolated, but the native Windows model process is supervised rather than
filesystem-sandboxed. This is not a public untrusted-file upload service. Recognition
completion does not establish accuracy or grant permission to publish. Unsupported,
empty, malformed or truncated recognition fails before quarantine/publication.

`python -m infra.ocr.check_worker` reproduces the full local fixture check with an
explicit loopback `creditlens_test` database and fixture/model paths. It creates only
synthetic grants and a uniquely named catalog/queue, retaining the resulting review
artifact and verification outside the repository.
