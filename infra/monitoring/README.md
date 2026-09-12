# Local Grafana and Prometheus

This optional stack visualizes actual synthetic API requests: scrape health,
request rate, p95/p99 duration, 4xx/5xx fractions, packet cache hits and dispositions.
It also shows workflow stage p95/errors, abstentions, HTTP authorization denials
and local alert states. Empty error/denial/alert series mean no samples were
recorded for those labels; they do not prove that all failure modes were tested.
Five ingestion panels also show queue states, oldest pending state, expired leases,
failed jobs by parser and attempts attached to retained jobs. They require a
PostgreSQL ingestion-enabled server; the default SQLite traffic fixture does not
manufacture ingestion samples. Configure its authenticated metrics scrape using
the protected deployment instructions below.
It does not claim neural latency, semantic accuracy, cloud savings or real lender traffic.

Install the `observability` extra alongside any other extras you use. Docker Desktop
must be running. The compose file pins image digests and disables automatic pulls.
If the images are absent, download the two exact images listed in `compose.yaml` first.

From the repository root, in one PowerShell terminal:

```powershell
.venv\Scripts\python.exe -m scripts.monitoring_fixture --output ../resources/credit_lens/evals/my-monitoring-run --seconds 300
```

Choose a fresh output directory. The fixture uses public synthetic documents,
normal application quotas, actual HTTP requests, SQLite audit and local traces.
It sends one query every two seconds and periodically checks a malformed query.
Port 19101 serves only aggregate metrics on `/metrics`; it binds all interfaces
so Docker Desktop can reach it. This temporary fixture is not for protected data.
It stops automatically after the selected duration (30 to 1800 seconds).

In another PowerShell terminal:

```powershell
$env:CREDITLENS_GRAFANA_PASSWORD = [guid]::NewGuid().ToString('N')
# Optional when another project uses port 19090:
$env:CREDITLENS_PROMETHEUS_PORT = '19091'
docker compose -f infra/monitoring/compose.yaml up -d
```

Open http://127.0.0.1:13000/d/creditlens-operations. Allow at least two scrapes
(10 seconds) for rate panels. Grafana grants anonymous Viewer access; its random
admin password is local and login forms are disabled. Both container ports bind
to loopback. Prometheus retains at most one day or 128 MB of time series.
Grafana reporting, update checks and automatic plugin installation are disabled.

Stop only this stack when finished:

```powershell
docker compose -f infra/monitoring/compose.yaml down
Remove-Item Env:CREDITLENS_GRAFANA_PASSWORD
```

Data is disposable; no persistent volume is configured. The fixture retains its
actual responses, audit database, traces and final metrics in the chosen directory.

## Interpretation and deployment boundary

Histogram percentiles are bucket estimates, unlike exact client-side benchmark
percentiles. Zero error rates are not evidence of fault tolerance. An unavailable
fixture appears as scrape health zero; other panels may have no data. Do not read
missing traffic as a successful service. The demo registry has finite labels and
excludes borrower identities, questions, credentials and document text.

For a protected deployment, replace the synthetic scrape target with the real
`/api/v1/metrics` endpoint and configure HTTPS plus an operator-managed current
admin token using Prometheus's authorization credentials file. Do not remove the
application's admin check or expose its data through this fixture. Token rotation,
external notification routing, semantic-quality, cost and detailed indexing/throughput
dashboards remain separate work.

Verified locally with Grafana 12.1.0: promtool accepted the configuration, the
target was up, and all eight provisioned expressions returned nonempty results
through Grafana's Prometheus proxy. Browser DOM inspection confirmed loaded panels
and series; screenshot capture was unavailable in the connected browser.

## Local alerts and diagnosis

Prometheus loads `alerts.yml` on startup. Restart its owned container after editing
rules. The Grafana alert-state panel and Prometheus `/alerts` show pending/firing
states. No email, Slack, webhook or Alertmanager destination is configured.

- `CreditLensUnavailable`: scrape down or target absent for 30 seconds. Check the
  fixture process, configured port and current admin credentials for protected deployments.
- `CreditLensQuerySlow`: query p95 over 2.7 seconds for one minute. Inspect stage
  p95 and correlated local traces; this threshold is the local latency gate.
- `CreditLensServerErrors`: HTTP 5xx fraction above 5% for one minute. Inspect
  stage errors and protected server diagnostics before retrying requests.
- `CreditLensCitationValidationFailure`: a citation-stage error in the last two
  minutes. Preserve the protected request audit/trace and inspect canonical source
  provenance. Never disable citation validation to clear the alert.

Abstention alone does not trigger an alert: requesting missing evidence can be
the correct outcome. Token/cost and semantic-quality alerts require actual
measurements and calibrated thresholds; none are invented for this provider.

Run `promtool test rules alerts.test.yml` from this directory using the pinned
Prometheus container's `/bin/promtool`. Tests cover outage persistence, recovery,
missing targets, slow queries, server errors and citation-stage failures. The
current local verification executed all 13 expressions through Grafana's real
Prometheus proxy, with stage timing and abstention samples from actual API traffic.

## Ingestion operations

Enable both `CREDITLENS_INGESTION_ENABLED` and `CREDITLENS_TELEMETRY_ENABLED`
with the documented PostgreSQL catalog and queue settings. A current admin can
scrape `/api/v1/metrics`; ordinary users remain denied. Aggregates are scoped to
that configured queue and omit identities and payloads. They are gauges, not
monotonic job-completion counters: pruning or state transitions can lower them.

`CreditLensIngestionLeaseExpired` fires after an expired running lease persists
for 30 seconds. Inspect worker health and let the normal fenced claim path recover
it; do not manually bypass the lease token. `CreditLensIngestionBacklog` fires
when queued/retry state age exceeds five minutes for one minute. Check worker
availability, retry reasons and staging access. OCR REVIEW_REQUIRED is a deliberate
human-review state and is shown separately, without an automatic publication action.
Failed OCR jobs may reflect permission, source or publication errors as well as
recognition failure. Investigate the protected job record before assigning cause.

Real PostgreSQL integration tests cover idempotent submissions, queue isolation,
lease expiration, terminal failure transitions and current-admin endpoint access.
Promtool covers ingestion-alert persistence and recovery. The SQL drill also verified all five ingestion expressions through the actual
Grafana Prometheus proxy, using retained scrapes from the bounded run. Both
ingestion alerts reached firing under the explicitly injected timestamp faults.

To reproduce that synthetic drill, start a fresh owned PostgreSQL fixture using
[the PostgreSQL guide](../postgres/README.md), then run from the repo root:

```powershell
uv run --no-sync python -m infra.monitoring.ingestion_fixture --port 15432 --output ../creditlens-ingestion-drill --seconds 120
```

Start this monitoring compose stack while the fixture runs. The fixture submits
three real SQL jobs, records a digital failure through the normal worker API, and
injects an aged queued timestamp plus an expired OCR lease. Those injections test
alert behavior; they are not measured production faults or OCR recognition runs.
Port 19101 serves only synthetic aggregate metrics for Docker to scrape. The
fixture ends automatically and retains its snapshot and final exposition. Stop
only the owned PostgreSQL and monitoring containers after verification.

## Reviewed semantic evaluation and token snapshots

The separate `CreditLens reviewed evaluation snapshots` dashboard uses six panels.
`scripts/export_evaluation_metrics.py --report PATH --sha256 REVIEWED_HASH --output FRESH_DIR`
accepts complete reconciled reports and verifies bound raw local model journals before
exporting measured token usage. Run it from the repository root so historical journal
paths resolve. Exporting requires no inference or external credentials.

`python infra/monitoring/serve_snapshot.py FRESH_DIR --host 0.0.0.0 --port 19102 --seconds 180`
serves only hash-verified metrics to the owned local Docker scraper. The default host
is loopback; the explicit host permits Docker access. Use port19102 for RAGAS and
19103 for a separately reconciled DeepEval report. The fixture automatically exits;
production operators should integrate the verified `.prom` file with their existing
textfile collector and configure refresh/retention independently.

Field support, strict whole-packet verdicts and human calibration are separate
measurements. Tokens are actual local evaluation usage, not serving estimates.
`cost_known=0` means dollar cost is unknown, not free electricity or zero operating
cost. Snapshot disappearance means no current scrape, not quality zero. The local
verification checked all six Grafana queries; RAGAS support, both token directions,
cost-unknown and absent-human-calibration appeared. Whole-packet panels remained
empty pending full reconciliation. Raw verification is retained privately in
`evals/semantic-monitoring-v1`; no source text or case IDs appear in metric labels.

### Completed local verification

The earlier operations dashboard had20 panels after adding two cost panels. Actual
HTTP packets incremented the unknown-cost observation count while the established
mean-cost query returned no series. No zero dollar value was substituted. The
retained-scrape check uses an explicit recorded query timestamp after the bounded
90-second fixture exited (`evals/monitoring-cost-v1/cost-panel-verification.json`).

The six-panel indexing dashboard uses the [isolated indexing drill](indexing.md).
Serve its reviewed metrics on19104. The actual Prometheus scrape initially rejected
Windows CRLF lines, despite the Python parser accepting them. The writer now emits
LF bytes, its test asserts no carriage returns, and an explicitly recorded LF-only
derivative preserves the original report/bulk bytes. All six corrected queries
returned actual data through Grafana. No indexing workload was rerun for this fix.

The full DeepEval reconciliation now accounts for235 graded packets and five
permission denials. Its snapshot reports102 raw passes and passing synthetic
controls. Those two panels were verified through Grafana/Prometheus. The raw43.4%
rate is a judge diagnostic: a fixed sample review found contradicted/ambiguous
reasons, and independent human calibration is absent. No metric was changed to
match r?sum? targets. The RAGAS and DeepEval observations remain separate.

Final query evidence is retained privately in
`evals/deepeval-monitoring-v1/grafana-verification.json`. All dashboard values are
local observations or explicitly labeled retained snapshots. End-to-end indexing
lag, embedding failures, production alert routing and hosted dollar costs are not
established by these fixtures.

## Final neural and full-publication verification

The current bundle has 38 panels: 23 operations, six evaluation and nine indexing.
The three added [neural panels](neural.md) report real embedding/reranker invocations,
errors and mean durations. The actual pinned CPU smoke recorded document1/query3/
reranker1 calls, including one explicitly injected query failure and identical
ranking after recovery. Cached document vectors were reused. All three queries
were verified through Grafana/Prometheus. Natural model failure prevalence is not
inferred from a fault drill.

The [full publication drill](publication-visibility.md) measured from PostgreSQL
commit return through canonical SQL readback, bulk upload and exact search
verification of every one of 3,840 input chunks. Two retained runs observed 4.890 s
and 2.910 s; SQL publication itself took 32.057 s and 34.564 s separately. These are
local observed upper bounds, not production latency percentiles. All input text
and metadata matched; there was no forced refresh or real-time GET substitute.

Three new indexing panels expose complete visibility delay, expected/verified
coverage and drill status. Four queries were checked at a retained Prometheus
scrape timestamp 2026-09-12T05:05:00Z through Grafana. The bounded exporter had
already exited when that historical query was made. Final evidence is retained
in `monitoring/publication-visibility-v2/grafana-verification.json` privately.
Serve that hash-bound snapshot on 19106 when reproducing the local dashboard.
Continuous production indexing watermarks and managed alert delivery remain
operator deployment concerns; queue age is still not used as index lag.
