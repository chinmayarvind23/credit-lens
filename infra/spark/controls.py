"""Real Spark controls for replay, Unicode, nullable borrower and corrupt source rejection."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from backfill import equivalent, serial, spark_backfill
from pyspark.sql import SparkSession


def main() -> None:
    """Run both implementations on equivalent duplicate, conflicting and corrupted inputs."""
    spark = SparkSession.builder.master("local[1]").appName("CreditLensControls").getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")
    row = {
        "tenant_id": "synthetic",
        "borrower_id": None,
        "document_id": "policy",
        "document_version": "v1",
        "page": 1,
        "acl_groups": ["b", "a"],
        "valid_from": "2026-01-01",
        "valid_to": None,
        "text": "Caf\u00e9 \U0001f4da",
    }
    row["content_hash"] = hashlib.sha256(row["text"].encode()).hexdigest()
    try:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input.jsonl"
            source.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
            serial(source, root / "serial.jsonl")
            spark_backfill(spark, source, root / "spark")
            result = equivalent(root / "serial.jsonl", root / "spark")
            if result["rows"] != 1:
                raise RuntimeError("Identical replay did not collapse")
            for name, bad in (
                ("hash", {**row, "content_hash": "wrong"}),
                ("conflict", {**row, "acl_groups": ["other"]}),
            ):
                source.write_text(json.dumps(row) + "\n" + json.dumps(bad) + "\n")
                for label, method in (("serial", serial), ("spark", spark_backfill)):
                    try:
                        if label == "spark":
                            method(spark, source, root / (name + label))
                        else:
                            method(source, root / (name + label))
                    except ValueError:
                        continue
                    raise RuntimeError("Invalid fixture was accepted")
            print(
                "Unicode/null equivalence, replay deduplication, hash and conflict controls passed"
            )
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
