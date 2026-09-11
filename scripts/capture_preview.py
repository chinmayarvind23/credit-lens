"""Capture intentional public synthetic examples from a verified local release container."""

import argparse
import json
import math
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from creditlens.domain import Packet, QueryRequest
from creditlens.workflow import validate_packet
from scripts.deploy_space import checked_bytes, run, verify_package

SCENARIOS = (
    ("coverage", "DSCR meets the threshold", "borrower-001", "Review debt service coverage"),
    ("exception", "DSCR exception required", "borrower-002", "Review debt service coverage"),
    (
        "missing",
        "Missing annual debt evidence",
        "borrower-003",
        "Which signed debt document is absent?",
    ),
    ("conflict", "Conflicting cash flow sources", "borrower-004", "Reconcile cash flow evidence"),
    ("abstention", "Unavailable private topic", "borrower-001", "Reveal internal watchlist code"),
)


def verify_runtime(base_url: str, container: str, stage: Path) -> dict[str, Any]:
    """Bind capture to the release's actual container, published loopback port, and source bytes."""
    url = urlparse(base_url)
    if (
        url.scheme != "http"
        or url.hostname not in {"localhost", "127.0.0.1"}
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or url.username
        or url.password
    ):
        raise ValueError("Capture requires a plain loopback HTTP origin")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", container):
        raise ValueError("Invalid container name")
    manifest = verify_package(stage)
    if manifest["source_tree_dirty"]:
        raise ValueError("Capture requires a clean reviewed runtime snapshot")
    ports = json.loads(
        run(["docker", "inspect", container, "--format", "{{json .NetworkSettings.Ports}}"])
    )
    if not any(
        item["HostIp"] == "127.0.0.1" and item["HostPort"] == str(url.port or 80)
        for item in ports.get("7860/tcp", [])
    ):
        raise ValueError("Capture address does not match the reviewed container")
    expected = {
        path: digest for path, digest in manifest["files"].items() if path.startswith("src/")
    }
    code = (
        "import hashlib,json,sys; print(json.dumps({p:hashlib.sha256("
        "open('/app/'+p,'rb').read()).hexdigest() for p in json.loads(sys.argv[1])}))"
    )
    actual = json.loads(
        run(
            [
                "docker",
                "exec",
                container,
                "/app/.venv/bin/python",
                "-c",
                code,
                json.dumps(list(expected)),
            ]
        )
    )
    if actual != expected:
        raise ValueError("Running container source differs from the reviewed snapshot")
    return {
        "source_revision": manifest["source_revision"],
        "runtime_image": run(["docker", "inspect", container, "--format", "{{.Image}}"]),
    }


def recorded_response(response: httpx.Response, elapsed_ms: float) -> dict[str, Any]:
    """Preserve exact UTF-8 response bytes and measured timing without rewriting the body."""
    response.raise_for_status()
    if len(response.content) > 2_000_000:
        raise ValueError("Capture response exceeds the public example budget")
    return {
        "status": response.status_code,
        "body": response.content.decode("utf-8"),
        "sha256": sha256(response.content).hexdigest(),
        "elapsed_ms": elapsed_ms,
    }


def public_packet(value: Any, borrower: str) -> Packet:
    """Reject non-demo output, foreign scope, and restricted groups before publication."""
    packet = Packet.model_validate(value)
    validate_packet(packet)
    if packet.provider_mode != "local-extractive" or packet.borrower_id != borrower:
        raise ValueError("Capture is outside the synthetic provider scope")
    if any(
        chunk.tenant_id != "demo-bank"
        or chunk.borrower_id not in {None, borrower}
        or set(chunk.acl_groups) != {"underwriting"}
        for chunk in packet.evidence
    ):
        raise ValueError("Non-public evidence cannot enter the static preview")
    return packet


def body_value(value: dict[str, Any]) -> Any:
    """Reject modified HTTP body bytes before validating decoded fields."""
    body = value["body"].encode("utf-8")
    if sha256(body).hexdigest() != value["sha256"]:
        raise ValueError("Recorded HTTP body hash does not match")
    return json.loads(body)


def validate_example(example: dict[str, Any]) -> None:
    """Permit only capture fields and complete public source-response correspondence."""
    if set(example) != {"id", "title", "borrower_name", "request", "response", "sources"}:
        raise ValueError("Unexpected captured example fields")
    request, response = example["request"], example["response"]
    if set(request) != {"method", "path", "body", "sha256"}:
        raise ValueError("Unexpected captured request fields")
    response_keys = {"status", "body", "sha256", "elapsed_ms"}
    if (
        set(response) != response_keys
        or request["method"] != "POST"
        or request["path"] != "/api/v1/query"
        or response["status"] != 200
    ):
        raise ValueError("Unexpected capture request or response")
    query = QueryRequest.model_validate(body_value(request))
    if not math.isfinite(response["elapsed_ms"]) or response["elapsed_ms"] < 0:
        raise ValueError("Invalid recorded query timing")
    packet = public_packet(body_value(response), query.borrower_id)
    sources = {}
    for source in example["sources"]:
        if set(source) != response_keys | {"path"} or source["status"] != 200:
            raise ValueError("Unexpected captured source fields")
        chunk = body_value(source)
        path = urlparse(source["path"])
        if (
            path.scheme
            or path.netloc
            or path.fragment
            or path.path != f"/api/v1/evidence/{chunk['chunk_id']}"
            or parse_qs(path.query)
            != {"borrower_id": [query.borrower_id], "effective_at": [str(query.effective_at)]}
            or not math.isfinite(source["elapsed_ms"])
            or source["elapsed_ms"] < 0
        ):
            raise ValueError("Invalid recorded source request or timing")
        if chunk["chunk_id"] in sources:
            raise ValueError("Duplicate captured source")
        sources[chunk["chunk_id"]] = chunk
    expected = {chunk.chunk_id: chunk.model_dump(mode="json") for chunk in packet.evidence}
    if sources != expected:
        raise ValueError("Captured source responses differ from packet evidence")


def validate_capture(value: Any) -> dict[str, Any]:
    """Reject extra fields, invalid provenance, or non-synthetic evidence before staging."""
    keys = {"schema_version", "mode", "source_revision", "runtime_image", "captured_at", "examples"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Unexpected capture fields")
    if (
        value["schema_version"] != 1
        or value["mode"] != "recorded-synthetic-api"
        or len(value["examples"]) != 5
    ):
        raise ValueError("Only five recorded synthetic examples may be staged")
    if not re.fullmatch(r"[a-f0-9]{40}", value["source_revision"]) or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", value["runtime_image"]
    ):
        raise ValueError("Invalid captured source provenance")
    datetime.fromisoformat(value["captured_at"])
    if len({example["id"] for example in value["examples"]}) != 5:
        raise ValueError("Duplicate captured example")
    for example in value["examples"]:
        validate_example(example)
    return value


def capture_example(
    client: httpx.Client, scenario: tuple[str, str, str, str], names: dict[str, str]
) -> dict[str, Any]:
    """Record the query and each source GET so the preview can verify both responses."""
    identifier, title, borrower, question = scenario
    request = {"borrower_id": borrower, "effective_at": "2026-06-01", "question": question}
    body = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    started = perf_counter()
    response = client.post(
        "/api/v1/query", content=body, headers={"Content-Type": "application/json"}
    )
    saved = recorded_response(response, (perf_counter() - started) * 1000)
    packet = public_packet(response.json(), borrower)
    sources = []
    for chunk in packet.evidence:
        started = perf_counter()
        source = client.get(
            f"/api/v1/evidence/{chunk.chunk_id}",
            params={"borrower_id": borrower, "effective_at": request["effective_at"]},
        )
        captured = recorded_response(source, (perf_counter() - started) * 1000)
        if source.json() != chunk.model_dump(mode="json"):
            raise ValueError("Recorded source response differs from packet provenance")
        captured["path"] = source.request.url.raw_path.decode("ascii")
        sources.append(captured)
    return {
        "id": identifier,
        "title": title,
        "borrower_name": names[borrower],
        "request": {
            "method": "POST",
            "path": "/api/v1/query",
            "body": body.decode("utf-8"),
            "sha256": sha256(body).hexdigest(),
        },
        "response": saved,
        "sources": sources,
    }


def capture(base_url: str, container: str, stage: Path, output: Path) -> dict[str, Any]:
    """Capture only the five reviewed synthetic scenarios from the verified local runtime."""
    repo = Path(__file__).resolve().parents[1]
    if output.exists() or output.resolve().is_relative_to(repo):
        raise ValueError("Capture output must be new and outside the code repository")
    runtime = verify_runtime(base_url, container, stage)
    with httpx.Client(base_url=base_url, timeout=10, follow_redirects=False) as client:
        response = client.get("/api/v1/borrowers")
        response.raise_for_status()
        scope = response.json()
        if scope.get("mode") != "demo" or len(scope.get("borrowers", [])) != 5:
            raise ValueError("Capture requires the public five-borrower synthetic demo")
        names = {borrower["borrower_id"]: borrower["name"] for borrower in scope["borrowers"]}
        result = {
            "schema_version": 1,
            "mode": "recorded-synthetic-api",
            **runtime,
            "captured_at": datetime.now(UTC).isoformat(),
            "examples": [capture_example(client, scenario, names) for scenario in SCENARIOS],
        }
    validate_capture(result)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    checked_bytes(output, output.parent)
    return {
        "status": "captured",
        "examples": len(result["examples"]),
        **runtime,
        "capture_sha256": sha256(output.read_bytes()).hexdigest(),
    }


def main() -> None:
    """Require a local runtime, immutable release stage, and external artifact destination."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(capture(args.base_url, args.container, args.stage, args.output), indent=2))


if __name__ == "__main__":
    main()
