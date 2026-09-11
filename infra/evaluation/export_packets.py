"""Freeze saved packet facts against canonical evidence before local semantic judging."""

import argparse
import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from creditlens.domain import Chunk, Packet, Page
from creditlens.evaluation import GoldCase, audit_chunks, page_key, read_inputs

FACT_FIELDS = (
    "borrower_id",
    "borrower_summary",
    "calculated_metrics",
    "applicable_policy",
    "policy_disposition",
    "missing_documents",
    "exceptions",
    "contradictions",
    "recommended_next_actions",
    "questions_for_underwriter",
    "abstained",
)


def verify_chunk(chunk: Chunk, page: Page) -> None:
    """Reject altered source text, offsets or scope metadata instead of trusting saved answers."""
    expected = page.model_dump()
    expected["text"] = page.text[chunk.start_char : chunk.end_char]
    actual = {key: getattr(chunk, key) for key in Page.model_fields}
    if chunk.end_char > len(page.text) or actual != expected:
        raise ValueError("Saved chunk differs from its canonical page")


def export_case(record: dict[str, Any], case: GoldCase, pages: dict[Any, Page]) -> dict[str, Any]:
    """Retain packet facts verbatim; deterministic citation and arithmetic checks stay separate."""
    packet = Packet.model_validate(record["outcome"]["packet"])
    if packet.borrower_id != case.borrower_id or audit_chunks(packet.evidence, case):
        raise ValueError("Saved packet violates authored scope")
    contexts = []
    for chunk in packet.evidence:
        page = pages[(chunk.tenant_id, *page_key(chunk))]
        verify_chunk(chunk, page)
        contexts.append(
            json.dumps(
                {
                    "document_id": page.document_id,
                    "document_version": page.document_version,
                    "page": page.page,
                    "valid_from": page.valid_from.isoformat(),
                    "valid_to": page.valid_to.isoformat() if page.valid_to else None,
                    "text": chunk.text,
                },
                ensure_ascii=False,
            )
        )
    data = packet.model_dump(mode="json", include=set(FACT_FIELDS))
    return {
        "id": case.case_id,
        "category": case.category,
        "input": f"Policy date: {case.effective_at.isoformat()}\n{case.question}",
        "actual_output": json.dumps(data, ensure_ascii=False),
        "retrieval_context": contexts,
    }


def export(args: argparse.Namespace) -> None:
    """Hash every input and require explicit IDs; never silently omit denied or missing rows."""
    repo = Path(__file__).resolve().parents[2]
    if args.output.resolve().is_relative_to(repo):
        raise ValueError("Saved packet exports must stay outside the repository")
    paths = {
        "gold": args.gold,
        "pages": args.pages,
        "records": args.records,
        "exporter": Path(__file__),
    }
    before = {key: sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    cases, pages = read_inputs(args.gold, args.pages)
    page_lookup = {(page.tenant_id, *page_key(page)): page for page in pages}
    if len(page_lookup) != len(pages):
        raise ValueError("Canonical page identities must be unique")
    records = [
        json.loads(line)
        for line in args.records.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    lookup = {record["case_id"]: record for record in records}
    if len(lookup) != len(records):
        raise ValueError("Saved case identities must be unique")
    selected = args.case_ids.split(",")
    if not 1 <= len(selected) <= 24 or len(set(selected)) != len(selected):
        raise ValueError("Select between one and 24 distinct case IDs")
    gold = {case.case_id: case for case in cases}
    exported = [export_case(lookup[key], gold[key], page_lookup) for key in selected]
    after = {key: sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    if before != after:
        raise ValueError("Inputs changed during export")
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in exported)
    manifest = {
        "input_hashes": before,
        "selected_ids": selected,
        "source_records": len(records),
        "exported_records": len(exported),
        "export_sha256": sha256(payload.encode()).hexdigest(),
        "selection": "Explicit pilot IDs frozen before judgment; not a population estimate",
        "output_fields": FACT_FIELDS,
        "context": "Verified original chunk spans and page metadata",
        "limits": "Faithfulness does not grade question relevance or citation applicability",
    }
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases.jsonl").write_bytes(payload.encode())
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main() -> None:
    """Keep original packet generation separate from judging to prevent benchmark drift."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gold", "pages", "records", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--case-ids", required=True)
    export(parser.parse_args())


if __name__ == "__main__":
    main()
