"""Judge all exact cited fields with RAGAS, reusing only byte-identical NLI prompts."""

import argparse
import asyncio
import json
import sys
from hashlib import sha256
from pathlib import Path

from run_deepeval import restrict_network


def read_population(root):
    """Verify every frozen shard and preserve the complete source population denominator."""
    raw = (root / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    coverage = (root / "coverage.json").read_bytes()
    if sha256(coverage).hexdigest() != manifest["coverage_sha256"]:
        raise ValueError("Population ledger changed")
    units = []
    for shard in manifest["shards"]:
        path = root / shard["file"]
        if path.parent.resolve() != root.resolve():
            raise ValueError("Shard path escapes the population directory")
        data = path.read_bytes()
        rows = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
        if sha256(data).hexdigest() != shard["sha256"] or [u["id"] for u in rows] != shard["ids"]:
            raise ValueError("Population shard changed")
        units.extend(rows)
    if len(units) != manifest["unit_count"] or len({u["id"] for u in units}) != len(units):
        raise ValueError("Population field identities differ")
    return manifest, units, sha256(raw).hexdigest()


def screened_sources(control_path, model_digest):
    """Reuse only passing controls whose pinned SDK, adapter, prompts and model still match."""
    sources = {
        str(p): sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")
    }
    from ragas.metrics.collections.faithfulness import metric as sdk_metric
    from ragas.metrics.collections.faithfulness import util as sdk_util

    for path in (
        Path(sdk_metric.__file__),
        Path(sdk_util.__file__),
        Path(__file__).with_name("uv.lock"),
    ):
        sources[str(path)] = sha256(path.read_bytes()).hexdigest()
    controls_raw = control_path.read_bytes()
    controls = json.loads(controls_raw)
    by_name = {Path(path).name: digest for path, digest in sources.items()}
    if (
        controls.get("status") != "completed"
        or controls.get("controls_passed") is not True
        or controls.get("provenance_stable") is not True
        or controls.get("digest") != model_digest
        or controls.get("unit_mode") != "verbatim"
        or controls.get("profile") != "lending-v1"
        or any(by_name.get(name) != digest for name, digest in controls["source_hashes"].items())
    ):
        raise ValueError("Controls do not match the current local metric and model")
    return sources, controls, sha256(controls_raw).hexdigest()


def run(args, loop):
    """Run sequential local inference with durable per-group results and no hidden retries."""
    if args.output.exists() or args.output.resolve().is_relative_to(
        Path(__file__).resolve().parents[2]
    ):
        raise ValueError("Use fresh private output")
    manifest, units, input_hash = read_population(args.population)
    sys.addaudithook(restrict_network)
    from ragas_judge import LocalJudge, RagasJudge  # noqa: I001 -- privacy before SDK imports
    from ragas.metrics.collections.faithfulness.util import NLIStatementInput
    from ragas_profiles import configure
    from run_ragas import validate_field_score
    from verbatim_faithfulness import VerbatimFieldSupport

    # Construct the actual pinned-library prompt before reuse; question relevance is not measured.
    args.output.mkdir(parents=True)
    prototype = LocalJudge(args.model, args.digest, args.output / "preflight.jsonl", calls=1)
    try:
        prototype.verify_model()
        metric = VerbatimFieldSupport(llm=RagasJudge(prototype), name="population_field_support")
        instructions = configure(metric, "lending-v1")
        groups = {}
        for unit in units:
            prompt = metric.nli_statement_prompt.to_string(
                NLIStatementInput(
                    context="\n".join(unit["retrieval_context"]), statements=[unit["actual_output"]]
                )
            )
            key = sha256(prompt.encode()).hexdigest()
            group = groups.setdefault(key, {"prompt": prompt, "unit": unit, "ids": []})
            if group["prompt"] != prompt:
                raise ValueError("Prompt identity collision")
            group["ids"].append(unit["id"])
    finally:
        prototype.close()
    sources, controls, controls_hash = screened_sources(args.controls, args.digest)
    summary = {
        "status": "running",
        "input_sha256": input_hash,
        "case_count": manifest["case_count"],
        "case_statuses": manifest["case_statuses"],
        "unit_count": len(units),
        "unique_prompts": len(groups),
        "completed_groups": 0,
        "failed_groups": 0,
        "graded_units": 0,
        "supported_units": 0,
        "model": args.model,
        "digest": args.digest,
        "instruction_hashes": instructions,
        "source_hashes": sources,
        "controls_sha256": controls_hash,
        "screened_controls": controls["case_count"],
        "whole_packet_scored": False,
        "human_calibrated": False,
        "metric": "RAGAS verbatim lending-v1 field support",
        "reuse": "Byte-identical actual NLI prompts; weighted field counts retain every ID",
    }
    summary_path = args.output / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    consecutive_errors = 0
    try:
        for index, (key, group) in enumerate(groups.items()):
            if any(
                sha256(Path(p).read_bytes()).hexdigest() != digest for p, digest in sources.items()
            ):
                raise ValueError("Evaluator source changed during run")
            unit = group["unit"]
            journal = args.output / f"calls-{index:04d}.jsonl"
            transport = LocalJudge(args.model, args.digest, journal, calls=1)
            judge = RagasJudge(transport)
            metric = VerbatimFieldSupport(llm=judge, name="population_field_support")
            configure(metric, "lending-v1")
            result = {"prompt_sha256": key, "unit_ids": group["ids"], "status": "failed"}
            try:
                score = float(
                    loop.run_until_complete(
                        metric.ascore(
                            user_input=unit["input"],
                            response=unit["actual_output"],
                            retrieved_contexts=unit["retrieval_context"],
                        )
                    ).value
                )
                validate_field_score(score, judge.outputs, unit["actual_output"])
                calls = [
                    json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
                ]
                if len(calls) != 1 or calls[0]["prompt"] != group["prompt"]:
                    raise ValueError("Actual NLI prompt differed from the reuse key")
                result.update(status="completed", score=score, outputs=judge.outputs)
                summary["completed_groups"] += 1
                summary["graded_units"] += len(group["ids"])
                summary["supported_units"] += int(score) * len(group["ids"])
                consecutive_errors = 0
            except Exception as error:
                result.update(error_type=type(error).__name__, outputs=judge.outputs)
                summary["failed_groups"] += 1
                consecutive_errors += 1
            finally:
                transport.close()
                with (args.output / "results.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(result) + "\n")
                summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(
                f"groups={index + 1}/{len(groups)} graded_fields={summary['graded_units']}",
                flush=True,
            )
            if consecutive_errors >= 3:
                raise ValueError("Three consecutive judge failures; remaining units stay ungraded")
        summary["status"] = "completed" if not summary["failed_groups"] else "completed_with_errors"
    finally:
        summary["ungraded_units"] = len(units) - summary["graded_units"]
        summary["provenance_stable"] = all(
            sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in sources.items()
        )
        summary["input_stable"] = read_population(args.population)[2] == input_hash
        if summary["status"] == "running":
            summary["status"] = "interrupted"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    """Use installed local weights only; never launch a service or download a model."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("population", "output", "controls"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--digest", required=True)
    loop = asyncio.new_event_loop()
    try:
        result = run(parser.parse_args(), loop)
        if (
            result["status"] != "completed"
            or not result["provenance_stable"]
            or not result["input_stable"]
        ):
            raise SystemExit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
