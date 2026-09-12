# Local scanned-document experiments

This offline CPU experiment is separate from the live demo. It does not expose a
file-upload endpoint. The API environment does not import Paddle or load OCR models.

Create a separate Python 3.11 environment and install the Windows dependency lock:

```powershell
uv venv ../resources/credit_lens/.venv-ocr --python 3.11
uv pip sync --python ../resources/credit_lens/.venv-ocr/Scripts/python.exe --require-hashes --link-mode copy infra/ocr/requirements-win.lock
uv pip check --python ../resources/credit_lens/.venv-ocr/Scripts/python.exe
```

The measured environment uses PaddleOCR 3.7.0, PaddlePaddle CPU 3.3.1 and PaddleX
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
at 144 DPI. The four resulting 1240x1754 PNGs were visually inspected. Public
untrusted-file ingestion still requires a complete job sandbox and supervisor.

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
Its cleanup was tested against a real child and grandchild process.

The first actual financial-page run recovered 15/15 cells at their exact row and
column, including 8/8 numeric cells. Model loading took 60.97 seconds and prediction
227.60 seconds on this workstation. This is one exposed synthetic scan, not a
general OCR accuracy estimate. The degraded table also recovered 15/15 cells,
including 8/8 numeric cells, but misread the printed footer page number as a blank
and a separate `9`. The actual PDF has one physical page. The two-column policy
recovered all 14 annotated lines in correct column order. The blank page returned
zero blocks. Prediction times were 292.45 seconds degraded, 316.83 seconds policy
and 5.95 seconds blank; those runs overlapped, so these are not isolated latency
benchmarks. No broad OCR accuracy or speedup claim follows from four fixtures.
`scripts/evaluate_scanned_fixtures.py` compares annotations with raw vendor JSON,
retaining every expected and actual table cell. Swapping years reduces accuracy
even when all numbers remain present.

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
do not establish readable evidence. The probe still does not publish pages or
automatically execute durable OCR jobs.

Contract tests exercise malformed JSON, blank output, geometry, reading order,
oversize output and uncertain completion. These tests do not measure OCR quality.
Raw model results and independent comparisons belong in the private resources
directory, separately from the 240-question RAG benchmark.

Run the platform-specific experiment tests separately with
`python -m pytest infra/ocr/tests`. The core normalization tests are in
`tests/test_ocr.py` and do not load models.

The planned PP-OCRv6 fallback was tested on the observed degraded-footer failure,
using tiny detector `d3177d4e5551463292a61e27cfca2b53e7c3fe9d` and recognizer
`0736086f72f666350ebcdc0c3a504eeac89cdfad`. Pass `--engine ocr-v6` to the supervisor.
It recovered all eight numeric strings and the footer's `Physical page: 1`
substring in 2.49 seconds prediction, but none of the four row labels matched
exactly. It did not establish table associations. Incorrect lines sometimes scored
above 0.9. It therefore remains a diagnostic comparison, not automatic fallback
or admission. Its load timer includes imports and manifest verification, unlike
the earlier VL runs; these small runs do not establish a comparative speedup.

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
This completes manual reviewed admission, not automatic OCR queue execution.

## Observed native completion

The instrumented financial-page run observed seven EOS-terminated sequences of
9 to 119 tokens, all below the 1,024-token cap. The run recovered 15/15 table cells
and 8/8 numeric cells, and normalized into a review-required artifact with zero
extraction confidence. No publication occurred. Actual supervised time was 368.94
seconds; this single synthetic fixture does not establish general OCR accuracy or
throughput. Fourteen OCR contract and supervisor tests passed.
