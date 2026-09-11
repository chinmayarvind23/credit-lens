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
experiment, not a production worker. The first measured execution is in progress.

`creditlens.ocr.normalize_vl` validates bounded vendor JSON, geometry and reading
order and preserves markup as data. It requires the trusted renderer's PDF/image
hashes and trusted document metadata. It discards metadata text, retains physical
page scope, and sets extracted page confidence to zero. All normalized artifacts
require review. Layout scores do not establish text recognition confidence. A
caller must establish generation completion before normalization; merely reaching
the token limit does not satisfy that requirement. The current probe does not
automatically establish completion or publish pages to the catalog.

Contract tests exercise malformed JSON, blank output, geometry, reading order,
oversize output and uncertain completion. These tests do not measure OCR quality.
Raw model results and independent comparisons belong in the private resources
directory, separately from the 240-question RAG benchmark.

Official contracts: [PaddleOCR-VL](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PaddleOCR-VL.en.md)
and [PP-DocLayoutV3](https://huggingface.co/PaddlePaddle/PP-DocLayoutV3).
