"""Scrape durable queue gauges without sharing mutable observations across requests."""

from collections.abc import Iterator

from prometheus_client.core import GaugeMetricFamily

from creditlens.ingestion_jobs import JobStore

STATES = ("QUEUED", "RUNNING", "RETRY", "REVIEW_REQUIRED", "COMPLETED", "FAILED")
PARSERS = ("digital", "ocr")


class IngestionCollector:
    """Keep label cardinality fixed even if a database row has an unexpected value."""

    def __init__(self, store: JobStore) -> None:
        """Reuse the app-owned pool; collector registration does not read the database."""
        self.store = store

    def describe(self) -> Iterator[GaugeMetricFamily]:
        """Declare names without querying SQL during startup or registry collision checks."""
        for suffix, help_text in (
            ("jobs", "Current retained jobs by state and parser"),
            ("attempts", "Attempts attached to current retained jobs"),
            ("oldest_state_seconds", "Age of oldest updated job in each state"),
            ("expired_leases", "Running jobs whose lease has expired"),
        ):
            yield GaugeMetricFamily(
                f"creditlens_ingestion_{suffix}", help_text, labels=["state", "parser"]
            )

    def collect(self) -> Iterator[GaugeMetricFamily]:
        """Fail an unavailable scrape rather than replace lost queue visibility with zero."""
        rows = self.store.aggregates()
        values = {
            (state, parser): [0.0, 0.0, 0.0, 0.0]
            for state in (*STATES, "other")
            for parser in (*PARSERS, "other")
        }
        for row in rows:
            key = (
                row.state if row.state in STATES else "other",
                row.parser if row.parser in PARSERS else "other",
            )
            value = values[key]
            value[0] += row.count
            value[1] += row.attempts
            value[2] = max(value[2], row.oldest_seconds)
            value[3] += row.expired_leases
        for index, metric in enumerate(self.describe()):
            for labels, value in values.items():
                metric.add_metric(list(labels), value[index])
            yield metric
