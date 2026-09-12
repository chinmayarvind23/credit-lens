# CreditLens

**Lending evidence with page-level citations, deterministic calculations and permission checks.**

[Open the interactive demo](https://huggingface.co/spaces/chinmayarvind/creditlens)

[![Recorded browser demonstration](https://huggingface.co/spaces/chinmayarvind/creditlens/resolve/main/demo.gif)](https://huggingface.co/spaces/chinmayarvind/creditlens)

Select a fictional borrower, ask a question, change the policy date and open the
cited pages. The free Hugging Face demo runs the Python evidence engine in your
browser. It needs no API key, backend connection, tunnel or running owner computer.
After startup, queries and source inspection work offline. The first load downloads
about 18 MB of runtime assets from Hugging Face.

## Why this exists

An underwriter needs to connect borrower facts to the right policy version. A
relevant answer can still be wrong if it uses another borrower's document, overlooks
a missing debt schedule or divides incompatible financial inputs. CreditLens
prepares an evidence packet with source quotes, financial metrics, missing inputs,
contradictions, exceptions and next actions. The underwriter makes the lending decision.

The server resolves current grants before retrieval, filters tenant/borrower/ACL/date
scope, computes DSCR with Decimal, checks exact citations, rechecks permissions and
persists an audit before returning a packet. Passing a DSCR threshold does not
approve a loan or establish compliance with every policy requirement.

## What works

- Questions, borrower selection, historical policy dates and exact source inspection.
- Deterministic DSCR, missing-input abstention, conflicting-source detection and exceptions.
- FastAPI server with current SQL grants, tenant/ACL filters and protected audit records.
- Optional local hybrid retrieval, cross-encoder reranking and selective query grounding.
- Bounded full-response caching with fresh authorization, citations, request IDs and audits.
- Optional Redis retrieval caching and a shared PostgreSQL canonical catalog.
- Durable digital-PDF ingestion, fenced worker leases, atomic publication and SQS-compatible notifications.
- Local OTel traces, Prometheus metrics, security/regression tests and CI quality/latency gates.
- Actual DeepEval and RAGAS runners using a local judge; validation limits are listed below.

## Measured results

| Measurement | Result | Scope |
| --- | --- | --- |
| Corpus and evaluation set | 3,840 physical pages, 203 PDFs, 240 questions | Synthetic, authored and exposed during development |
| Lexical retrieval | Recall@10 74.05%, nDCG@10 .6775 | 220 eligible questions; unchanged in the latest full replay |
| Hybrid + reranking + selective query grounding | Recall@10 86.54%, nDCG@10 .7878 | Local composed workflow; separate from the browser's lexical mode |
| Structured workflow outcomes | 240/240 fixture passes | Deterministic checks, not semantic groundedness |
| Response cache, disabled / warm | HTTP p95 23.14 / 22.67 ms (repeat) | 150 requests per mode, serial loopback, disk audit and telemetry enabled |
| Cache variability | 18.35% first run; 2.04% repeat | Same five-borrower workload; 330 distinct audits per run including warmups |
| Custom RAGAS field support | 85/85 fields supported; 24/24 controls | Six selected historical packets, exact fields preserved, local judge |
| Public browser verification | 12 checks passed | Desktop/mobile, all five dispositions, sources and questions with network disabled after startup |

Cache gains varied across runs on this shared workstation; a reliable production
speedup is not established. The response-cache comparison preserves every substantive packet field and creates
a fresh audit for every request. These timings do not establish production latency
or cloud cost savings. The corpus uses 98 short templates and is not a real lender dataset.

Requested targets of 94.1% recall, .89 nDCG, 96.8% semantic citation precision,
92.5% grounded answers and 1.7% unsupported claims are **not established results**.
DeepEval/RAGAS are integrated and have real pilot runs, but human-calibrated,
full-240 semantic grading remains open. The 85-field result uses a custom
whole-field support rubric, not stock atomic-claim faithfulness or whole-packet accuracy.
See [evaluation definitions](docs/evaluation.md) and [judge runners](infra/evaluation/README.md).

## Run the server locally

Use Python 3.11, uv and Bun 1.3.10. The synthetic demo needs no cloud credentials.

```powershell
uv sync --locked
cd apps/web
bun install --frozen-lockfile
bun run build
cd ../..
uv run --no-sync uvicorn creditlens.api:create_app --factory --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 and http://127.0.0.1:8000/docs.
For the free browser deployment, follow the [build and publish guide](infra/huggingface/browser/DEPLOYMENT.md).

To enable response caching and local telemetry:

```powershell
uv sync --locked --extra observability
$env:CREDITLENS_RESPONSE_CACHE_ENABLED = 'true'
$env:CREDITLENS_TELEMETRY_ENABLED = 'true'
$env:CREDITLENS_TRACE_FILE = 'C:/private/creditlens/traces.jsonl'
uv run --no-sync uvicorn creditlens.api:create_app --factory --host 127.0.0.1 --port 8000
```

Choose your own private trace directory. Traces contain stage timing and correlation,
not questions, documents, tokens or borrower identities. `/api/v1/metrics` requires a
current admin grant. [Observability](docs/observability.md) explains the exporter and metrics.

Optional service instructions: [hybrid retrieval](infra/retrieval/README.md),
[Redis](infra/redis/README.md), [PostgreSQL and digital ingestion](infra/postgres/README.md),
[SQS-compatible workers](infra/sqs/README.md), [reviewed OCR ingestion](infra/ocr/README.md).

## Architecture

```mermaid
flowchart LR
    HF[Free HF Static hosting] --> UI[TypeScript workbench]
    UI --> Worker[Python in a browser worker]
    Worker --> Demo[Public fictional evidence + session SQLite]
    ServerUI[Server workbench] --> API[FastAPI]
    API --> Scope[Current SQL grants + canonical evidence]
    Scope --> Cache[Optional response cache]
    Cache --> Retrieval[BM25 or local hybrid + reranking]
    Retrieval --> Packet[Decimal finance + quoted evidence]
    Packet --> Validate[Current permissions + citation checks]
    Validate --> Audit[Fresh protected SQL audit]
    API --> Telemetry[Local OTel + Prometheus]
```

Both deployments reuse the Python workflow. The browser distributes only public
fictional pages; its scope checks cannot protect confidential assets against the
visitor. The authenticated server is the path for protected data. Server production
wiring to live Cognito/Cortex is not complete, so the public demo must never accept
real borrower files.

The stack is Python, FastAPI, Pydantic, SQLAlchemy, Decimal, TypeScript and Bun;
Pyodide for the free demo; optional LlamaIndex, Sentence Transformers, Redis,
PostgreSQL, OpenSearch, SQS, DeepEval/RAGAS, OTel and Prometheus for local experiments
and service paths. Kubernetes is not used.

## Checks and reproduction

```powershell
uv sync --locked --extra queue --extra retrieval --extra observability
uv run --no-sync ruff check src
uv run --no-sync ruff format --check src
uv run --no-sync mypy src
uv run --no-sync pytest tests infra/huggingface/tests .github/tests --ignore=tests/test_retrieval_lab.py
uv run --no-sync python -m scripts.benchmark_response_cache --output ../creditlens-http-results
```

The benchmark records actual HTTP responses, packet equality, unique audits, local
traces, metrics, source hashes and a 2.7-second p95 ceiling. Its observed latency is
reported independently of that ceiling. CI also runs the frozen 240-question
retrieval/security regression gate and coverage checks. Hosted CI is manual-dispatch
only under the no-spending constraint; local checks do not imply a hosted run.

## Optional AWS setup

The demo is hosted on Hugging Face's free plan. AWS is optional, has not been
provisioned, and is not required to use or develop CreditLens. The
[AWS setup guide](infra/aws/README.md) covers the Docker image, IAM bootstrap,
Terraform plan, `us-east-1`, health checks, cost considerations and teardown for
operators who choose their own deployment. That reference is not guaranteed free.

## Remaining work and tradeoffs

The working demo is a prototype, not a finished production lending system.
The main gaps are a calibrated full semantic benchmark, more diverse documents and
unseen questions, automated OCR queue execution, live governed search/identity integrations
and production operations. Process-local response caches and quotas require further
coordination before horizontal scaling. LangSmith/CloudWatch and managed dashboards
are not deployed. None of these planned components are presented as running services.

The modular monolith keeps permission checks and audit close to evidence generation.
That makes the current workflow easier to verify; a larger ingestion workload may
justify independent workers and shared quotas before adding more service boundaries.

## Documentation

[System design](docs/system-design.md) ? [HLD](docs/HLD.md) ? [LLD](docs/LLD.md) ?
[Security](docs/security.md) ? [Evaluation](docs/evaluation.md) ?
[Performance](docs/performance.md) ? [Deployment](docs/deployment.md)

Raw evidence, architecture decisions, the blog draft, interview notes and recordings
are maintained outside the repository in `resources/credit_lens`.
