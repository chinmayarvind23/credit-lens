"""Compare actual OCR output with independent synthetic annotations without changing gold data."""

import argparse
import json
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path


def normalized(value: str) -> str:
    """Ignore whitespace only; decimal digits, punctuation and financial labels remain exact."""
    return " ".join(value.split())


class TableReader(HTMLParser):
    """Read cell associations as data without executing or rendering model-produced HTML."""

    def __init__(self) -> None:
        """Track rows explicitly so a correct number in the wrong year still fails."""
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.cell: list[str] | None = None
        self.has_spans = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Flag merged cells rather than flattening their ambiguous column associations."""
        if tag == "tr":
            self.rows.append([])
        if tag in {"td", "th"}:
            self.cell = []
            self.has_spans |= any(key in {"rowspan", "colspan"} for key, _ in attrs)

    def handle_data(self, data: str) -> None:
        """Retain only cell text; scripts outside cells cannot become executable content."""
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Finalize each physical row/cell without sorting numbers into expected positions."""
        if tag in {"td", "th"} and self.cell is not None:
            if not self.rows:
                self.rows.append([])
            self.rows[-1].append(normalized("".join(self.cell)))
            self.cell = None


def score_table(blocks: list[dict], expected: list[list[str]]) -> dict:
    """Score every annotated cell at its exact row and column, preserving mismatches for review."""
    tables = []
    for block in blocks:
        if block["block_label"] == "table":
            reader = TableReader()
            reader.feed(block["block_content"])
            tables.append(reader)
    actual = tables[0].rows if len(tables) == 1 and not tables[0].has_spans else []
    comparisons = []
    for row, cells in enumerate(expected):
        for column, value in enumerate(cells):
            found = actual[row][column] if row < len(actual) and column < len(actual[row]) else None
            comparisons.append({"row": row, "column": column, "expected": value, "actual": found})
    numeric = [item for item in comparisons if item["row"] > 0 and item["column"] > 0]
    return {
        "expected_cells": len(comparisons),
        "exact_cells": sum(item["expected"] == item["actual"] for item in comparisons),
        "numeric_cells": len(numeric),
        "exact_numeric_cells": sum(item["expected"] == item["actual"] for item in numeric),
        "exact_shape": [len(row) for row in actual] == [len(row) for row in expected],
        "comparisons": comparisons,
    }


def score_output(raw: bytes, expected: dict) -> dict:
    """Measure text presence and column order separately from table-cell accuracy."""
    payload = json.loads(raw)
    blocks = payload["parsing_res_list"]
    text = normalized(" ".join(block["block_content"] for block in blocks))
    result: dict = {
        "raw_sha256": sha256(raw).hexdigest(),
        "blocks": len(blocks),
        "has_readable_evidence": bool(text),
        "required_text": {
            phrase: normalized(phrase) in text for phrase in expected.get("required_text", [])
        },
    }
    if "table" in expected:
        result["table"] = score_table(blocks, expected["table"])
    if "columns" in expected:
        lines = [line for column in expected["columns"] for line in column]
        positions = [text.find(normalized(line)) for line in lines]
        result["column_lines_found"] = sum(position >= 0 for position in positions)
        result["column_lines_expected"] = len(lines)
        result["column_reading_order_correct"] = all(
            position >= 0 for position in positions
        ) and all(left < right for left, right in zip(positions, positions[1:], strict=False))
        result["column_positions"] = positions
    if "must_fail" in expected:
        result["blank_correctly_empty"] = not text
    return result


def main() -> None:
    """Write a new comparison artifact bound to both annotations and raw inference output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    annotations = args.annotations.read_bytes()
    fixture = next(
        item for item in json.loads(annotations)["fixtures"] if item["name"] == args.fixture
    )
    result = score_output(args.raw.read_bytes(), fixture["expected"])
    result["annotations_sha256"] = sha256(annotations).hexdigest()
    result["fixture"] = args.fixture
    result["pdf_sha256"] = fixture["pdf_sha256"]
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "table"}))


if __name__ == "__main__":
    main()
