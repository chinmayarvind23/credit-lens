"""Rebuild scoped staging metadata and compare serial Python with actual local PySpark."""

import argparse
import hashlib
import json
from pathlib import Path
from time import perf_counter

FIELDS = (
    "tenant_id",
    "borrower_id",
    "document_id",
    "document_version",
    "page",
    "acl_groups",
    "valid_from",
    "valid_to",
    "content_hash",
    "text_chars",
)
KEY = ("tenant_id", "document_id", "document_version", "page")


def serial(source: Path, destination: Path) -> dict:
    """Validate every source row and reject conflicting duplicates before staging output."""
    started = perf_counter()
    rows = {}
    count = 0
    with source.open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            if (
                hashlib.sha256(item["text"].encode()).hexdigest() != item["content_hash"]
                or not item["acl_groups"]
                or item["page"] < 1
            ):
                raise ValueError("Invalid source hash or scope")
            item["text_chars"] = len(item["text"])
            item["acl_groups"] = sorted(item["acl_groups"])
            row = {k: item[k] for k in FIELDS}
            key = tuple(row[k] for k in KEY)
            if key in rows and rows[key] != row:
                raise ValueError("Conflicting duplicate page identity")
            rows[key] = row
            count += 1
    with destination.open("x", encoding="utf-8") as stream:
        for key in sorted(rows):
            stream.write(json.dumps(rows[key], sort_keys=True) + "\n")
    return {"input_rows": count, "output_rows": len(rows), "seconds": perf_counter() - started}


def spark_backfill(spark, source: Path, destination: Path) -> dict:
    """Use native expressions, validate hashes and uniqueness, then write staged JSON records."""
    from pyspark.sql import functions as f

    started = perf_counter()
    schema = (
        "tenant_id STRING, borrower_id STRING, document_id STRING, document_version STRING, "
        "page INT, acl_groups ARRAY<STRING>, valid_from STRING, valid_to STRING, "
        "content_hash STRING, text STRING"
    )
    raw = spark.read.schema(schema).option("mode", "FAILFAST").json(str(source))
    invalid = (
        f.col("text").isNull()
        | f.col("content_hash").isNull()
        | (f.sha2("text", 256) != f.col("content_hash"))
        | f.col("acl_groups").isNull()
        | (f.size("acl_groups") == 0)
        | f.col("page").isNull()
        | (f.col("page") < 1)
    )
    if raw.filter(invalid).limit(1).count():
        raise ValueError("Invalid source hash or scope")
    projected = (
        raw.withColumn("text_chars", f.length("text"))
        .withColumn("acl_groups", f.sort_array("acl_groups"))
        .select(*FIELDS)
        .dropDuplicates()
        .cache()
    )
    try:
        if projected.groupBy(*KEY).count().filter("count > 1").limit(1).count():
            raise ValueError("Conflicting duplicate page identity")
        count, output_count = raw.count(), projected.count()
        projected.write.mode("errorifexists").option("ignoreNullFields", "false").json(
            str(destination)
        )
        return {
            "input_rows": count,
            "output_rows": output_count,
            "seconds": perf_counter() - started,
        }
    finally:
        projected.unpersist()


def equivalent(serial_path: Path, spark_path: Path) -> dict:
    """Compare every semantic row, independent of Spark partition order and JSON whitespace."""
    expected = [json.loads(line) for line in serial_path.read_text().splitlines()]
    actual = [
        json.loads(line)
        for part in spark_path.glob("part-*.json")
        for line in part.read_text().splitlines()
    ]

    def canonical(rows):
        """Compare record multisets while retaining duplicates and all nullable fields."""
        return sorted(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)

    if canonical(expected) != canonical(actual):
        raise ValueError("Serial and Spark metadata differ")
    return {
        "equal": True,
        "rows": len(actual),
        "semantic_sha256": hashlib.sha256("\n".join(canonical(actual)).encode()).hexdigest(),
    }


def execute(source: Path, output: Path) -> None:
    """Measure two replay sizes and local worker counts, retaining startup and event evidence."""
    from pyspark.sql import SparkSession

    output.mkdir(parents=True, exist_ok=False)
    original = source.read_bytes()
    report = {
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "status": "running",
        "runs": {},
    }
    report["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    inputs = {}
    for factor in (1, 10):
        replay = output / f"input-{factor}.jsonl"
        replay.write_bytes(original * factor)
        inputs[factor] = replay
        report["runs"][f"python-{factor}"] = serial(replay, output / f"python-{factor}.jsonl")
    for workers in (1, 2):
        events = output / f"events-{workers}"
        events.mkdir()
        started = perf_counter()
        spark = (
            SparkSession.builder.master(f"local[{workers}]")
            .appName("CreditLensBackfill")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.shuffle.partitions", "4")
            .config("spark.eventLog.enabled", "true")
            .config("spark.eventLog.dir", str(events))
            .getOrCreate()
        )
        report[f"startup_seconds_{workers}"] = perf_counter() - started
        report["spark_version"] = spark.version
        spark.sparkContext.setLogLevel("ERROR")
        try:
            for factor, replay in inputs.items():
                key = f"spark-{workers}-{factor}"
                result = spark_backfill(spark, replay, output / key)
                result["equivalence"] = equivalent(output / f"python-{factor}.jsonl", output / key)
                report["runs"][key] = result
                (output / "report.json").write_text(json.dumps(report, indent=2))
                print(key + " verified", flush=True)
        finally:
            spark.stop()
    report["status"] = "completed"
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != report["script_sha256"]:
        raise ValueError("Benchmark code changed during execution")
    (output / "report.json").write_text(json.dumps(report, indent=2))


def main() -> None:
    """Require explicit source and fresh output; never mutate the source catalog."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Choose a fresh output directory")
    try:
        execute(args.pages, args.output)
    except BaseException as error:
        if args.output.is_dir():
            report_path = args.output / "report.json"
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
            report.update(status="failed", error_type=type(error).__name__)
            report_path.write_text(json.dumps(report, indent=2))
        raise


if __name__ == "__main__":
    main()
