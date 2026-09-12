"""Verify real pinned CPU inference telemetry and one explicitly injected query failure."""

import argparse
import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from prometheus_client.parser import text_string_to_metric_families

from creditlens.corpus import build_demo_pages
from creditlens.errors import ServiceError
from creditlens.neural_search import LocalNeuralRanker
from creditlens.observability import Telemetry
from creditlens.retrieval import chunk_page


def fail_query(*args: object, **kwargs: object) -> None:
    """An explicit fault tests observability and lock recovery, not natural model reliability."""
    raise RuntimeError("private-injected-query-failure")


def require(condition: bool, message: str) -> None:
    """Keep operational acceptance checks active under optimized Python."""
    if not condition:
        raise RuntimeError(message)


def finish(
    output: Path,
    report: dict[str, Any],
    hashes: dict[str, str],
    model: LocalNeuralRanker | None,
    telemetry: Telemetry | None,
) -> None:
    """Preserve original failures even when cleanup or final evidence inspection also fails."""
    errors = []
    for component, resource in (("model", model), ("telemetry", telemetry)):
        if resource is not None:
            try:
                resource.close()
            except Exception as error:
                errors.append({"component": component, "error_type": type(error).__name__})
    report["cleanup_errors"] = errors
    try:
        report["source_stable"] = bool(hashes) and all(
            sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in hashes.items()
        )
    except OSError as error:
        report.update(source_stable=False, provenance_error_type=type(error).__name__)
    try:
        records = (output / "traces.jsonl").read_text(encoding="utf-8")
        report["privacy_passed"] = all(
            marker not in records
            for marker in ("PRIVATE-FAULT-QUESTION", "private-injected-query-failure")
        )
    except (OSError, UnicodeError) as error:
        report.update(privacy_passed=False, privacy_error_type=type(error).__name__)
    if (
        errors
        or not report["source_stable"]
        or not report["privacy_passed"]
        or report["status"] != "passed"
    ):
        report["status"] = "failed"
    try:
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError:
        # An unwritable artifact cannot hide the original inference failure with a cleanup error.
        if "error_type" not in report:
            raise


def run(directory: Path, output: Path) -> dict[str, Any]:
    """Load existing verified weights, preserve measurements and avoid remote model services."""
    repo = Path(__file__).resolve().parents[1]
    if output.resolve().is_relative_to(repo) or output.exists():
        raise ValueError("Use a fresh private output directory")
    sources = [
        Path(__file__),
        *(
            repo / "src/creditlens" / name
            for name in (
                "neural_search.py",
                "runtime.py",
                "observability.py",
                "model_bundle.py",
                "model_manifest.json",
            )
        ),
    ]
    output.mkdir(parents=True)
    hashes: dict[str, str] = {}
    telemetry = None
    model = None
    report: dict[str, Any] = {
        "status": "running",
        "source_hashes": hashes,
        "fault_injection": "one explicit encode_query RuntimeError",
        "natural_failure_rate": "not measured",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    try:
        hashes = {str(path.resolve()): sha256(path.read_bytes()).hexdigest() for path in sources}
        report["source_hashes"] = hashes
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        telemetry = Telemetry(str(output / "traces.jsonl"))
        model = LocalNeuralRanker(directory, telemetry=telemetry)
        chunks = tuple(chunk_page(page)[0] for page in build_demo_pages()[:3])
        first = model.rank("What is the debt service coverage policy?", chunks)
        original = model.embedding.encode_query
        model.embedding.encode_query = fail_query
        try:
            model.rank("PRIVATE-FAULT-QUESTION", chunks)
        except ServiceError as error:
            require(error.code == "model_unavailable", "Unexpected model fault contract")
        else:
            raise RuntimeError("Injected query failure did not propagate")
        finally:
            model.embedding.encode_query = original
        recovered = model.rank("What is the debt service coverage policy?", chunks)
        reranked = model.rerank("What is the debt service coverage policy?", recovered)
        require(first == recovered, "Recovered deterministic ranking changed")
        metrics = telemetry.render()
        samples = {
            (sample.name, tuple(sorted(sample.labels.items()))): sample.value
            for family in text_string_to_metric_families(metrics.decode())
            for sample in family.samples
        }
        for operation, count in (("embed_documents", 1), ("embed_query", 3), ("rerank", 1)):
            labels = (("stage", "neural." + operation),)
            require(
                samples[("creditlens_stage_duration_seconds_count", labels)] == count,
                "Unexpected operation invocation count",
            )
        require(
            samples[("creditlens_stage_errors_total", (("stage", "neural.embed_query"),))] == 1,
            "Injected failure was not observed exactly once",
        )
        (output / "metrics.prom").write_bytes(metrics)
        report.update(
            status="passed",
            model_revision=model.revision,
            first_ids=[chunk.chunk_id for chunk in first],
            reranked_ids=[chunk.chunk_id for chunk in reranked],
            document_invocations=1,
            query_invocations=3,
            rerank_invocations=1,
            injected_query_failures=1,
            recovery_identical=True,
        )
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        finish(output, report, hashes, model, telemetry)
    require(report["status"] == "passed", "Neural telemetry verification failed")
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "metrics_sha256": sha256((output / "metrics.prom").read_bytes()).hexdigest(),
                "report_sha256": sha256((output / "report.json").read_bytes()).hexdigest(),
                "scope": "actual pinned local inference plus explicit query fault",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return report


def main() -> None:
    """Require existing local model files and an explicit fresh evidence destination."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.models, args.output)))


if __name__ == "__main__":
    main()
