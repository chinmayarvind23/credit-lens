# Local Grafana and Prometheus

This optional stack visualizes actual synthetic API requests: scrape health,
request rate, p95/p99 duration, 4xx/5xx fractions, packet cache hits and dispositions.
It also shows workflow stage p95/errors, abstentions, HTTP authorization denials
and local alert states. Empty error/denial/alert series mean no samples were
recorded for those labels; they do not prove that all failure modes were tested.
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
external notification routing, semantic-quality and ingestion dashboards remain separate work.

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
