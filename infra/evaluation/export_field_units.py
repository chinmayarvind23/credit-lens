"""Export every cited packet field with canonical context and an explicit unscored-field ledger."""

import argparse
import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from export_packets import export_case

from creditlens.citations import validate_citation
from creditlens.domain import Claim, FinancialMetric, Packet, Page
from creditlens.evaluation import GoldCase, page_key, read_inputs

FIELDS = {
    "request_id": "execution_metadata",
    "borrower_id": "identity",
    "borrower_summary": "claim",
    "calculated_metrics": "metric",
    "applicable_policy": "claim",
    "policy_disposition": "pending_rubric",
    "missing_documents": "pending_rubric",
    "exceptions": "claim",
    "contradictions": "claim",
    "recommended_next_actions": "pending_rubric",
    "questions_for_underwriter": "pending_rubric",
    "abstained": "pending_rubric",
    "evidence": "source_context",
    "stages": "execution_metadata",
    "provider_mode": "execution_metadata",
    "corpus_version": "execution_metadata",
    "latency_ms": "execution_metadata",
    "cache_hit": "execution_metadata",
    "cost_usd": "execution_metadata",
}


def make_unit(
    value: Claim | FinancialMetric, packet: Packet, case: GoldCase, field: str, index: int
) -> dict[str, Any]:
    """Preserve claims and numbers; provide only the field's verified cited spans as context."""
    contexts = []
    seen = set()
    for citation in value.citations:
        chunk = validate_citation(citation, packet.evidence)
        if chunk.chunk_id not in seen:
            contexts.append(
                json.dumps(
                    {
                        **citation.model_dump(),
                        "text": chunk.text,
                        "valid_from": chunk.valid_from.isoformat(),
                        "valid_to": chunk.valid_to.isoformat() if chunk.valid_to else None,
                    },
                    ensure_ascii=False,
                )
            )
            seen.add(chunk.chunk_id)
    text = (
        f"The calculated {value.name} is {value.value} {value.unit}."
        if isinstance(value, FinancialMetric)
        else value.text
    )
    unit = {
        "id": f"{case.case_id}/{field}/{index}",
        "case_id": case.case_id,
        "field_path": f"/{field}/{index}",
        "kind": FIELDS[field],
        "input": f"Policy date: {case.effective_at.isoformat()}\n{case.question}",
        "actual_output": text,
        "retrieval_context": contexts,
        "original_value": value.model_dump(mode="json"),
    }
    if isinstance(value, FinancialMetric):
        # This checks extraction fidelity only; it does not label the value as correct.
        unit["expected_statements"] = [text]
    return unit


def packet_units(
    record: dict[str, Any], case: GoldCase, pages: dict[Any, Page]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Verify scope before field lookup and reject schema drift instead of omitting new fields."""
    if set(FIELDS) != set(Packet.model_fields):
        raise ValueError("Packet schema differs from the evaluation field inventory")
    export_case(record, case, pages)
    packet = Packet.model_validate(record["outcome"]["packet"])
    if len({chunk.chunk_id for chunk in packet.evidence}) != len(packet.evidence):
        raise ValueError("Duplicate evidence identities make field context ambiguous")
    original = packet.model_dump(mode="json")
    units = []
    fields = []
    for name, kind in FIELDS.items():
        value = original[name]
        item = {"path": f"/{name}", "kind": kind, "status": "unscored"}
        if kind in {"claim", "metric"}:
            emitted = [
                make_unit(v, packet, case, name, i) for i, v in enumerate(getattr(packet, name))
            ]
            units.extend(emitted)
            item.update(
                status="exported" if emitted else "empty", unit_ids=[u["id"] for u in emitted]
            )
        elif kind == "pending_rubric":
            item.update(status="empty" if value == [] else "pending_rubric", original_value=value)
        else:
            item.update(
                status="accounted",
                value_sha256=sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(),
            )
        fields.append(item)
    return units, {"case_id": case.case_id, "fields": fields, "whole_packet_scored": False}


def export(args: argparse.Namespace) -> None:
    """Freeze all selected fields outside the repo, retaining failed or pending scoring coverage."""
    repo = Path(__file__).resolve().parents[2]
    if args.output.resolve().is_relative_to(repo):
        raise ValueError("Evaluation exports must stay outside the source repository")
    if args.output.exists():
        raise ValueError("Use a fresh output directory")
    paths = {
        "gold": args.gold,
        "pages": args.pages,
        "records": args.records,
        "exporter": Path(__file__),
        "packet_exporter": Path(__file__).with_name("export_packets.py"),
        "domain": repo / "src/creditlens/domain.py",
        "citations": repo / "src/creditlens/citations.py",
        "scope": repo / "src/creditlens/evaluation.py",
    }
    before = {name: sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    cases, pages = read_inputs(args.gold, args.pages)
    canonical = {(p.tenant_id, *page_key(p)): p for p in pages}
    gold = {c.case_id: c for c in cases}
    records = [
        json.loads(line)
        for line in args.records.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    saved = {r["case_id"]: r for r in records}
    if len(saved) != len(records) or len(canonical) != len(pages) or len(gold) != len(cases):
        raise ValueError("Input identities must be unique")
    selected = args.case_ids.split(",")
    if not 1 <= len(selected) <= 24 or len(selected) != len(set(selected)):
        raise ValueError("Select one to 24 distinct packet IDs")
    units, coverage = [], []
    for identity in selected:
        emitted, ledger = packet_units(saved[identity], gold[identity], canonical)
        units.extend(emitted)
        coverage.append(ledger)
    if before != {name: sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}:
        raise ValueError("Inputs changed during export")
    files = {
        "units.jsonl": "".join(json.dumps(u, ensure_ascii=False) + "\n" for u in units),
        "coverage.json": json.dumps(coverage, indent=2, ensure_ascii=False) + "\n",
    }
    manifest = {
        "schema_version": 1,
        "input_hashes": before,
        "selected_ids": selected,
        "unit_count": len(units),
        "scored_units": 0,
        "whole_packet_scored": False,
        "output_hashes": {name: sha256(data.encode()).hexdigest() for name, data in files.items()},
        "scope": "All cited claim/metric fields exported; pending rubrics explicitly retained",
        "limit": "Field export coverage does not establish extraction fidelity or semantic quality",
    }
    args.output.mkdir(parents=True, exist_ok=False)
    for name, payload in files.items():
        (args.output / name).write_bytes(payload.encode())
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    """Require explicit saved inputs and packet IDs; export never calls a model or network."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gold", "pages", "records", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--case-ids", required=True)
    export(parser.parse_args())


if __name__ == "__main__":
    main()
