"""Failed live-check setup and cleanup must leave readable, explicitly failed evidence."""

import argparse
import json
import runpy
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from creditlens.corpus import build_demo_pages


def phase_response(request: httpx.Request, phase: str, output: Path) -> httpx.Response:
    """Ensure a manifest predates network traffic and inject the selected lifecycle failure."""
    assert (output / "manifest.json").is_file()
    if phase == "connection":
        raise httpx.ConnectError("private detail", request=request)
    if request.method == "DELETE":
        if phase == "cleanup":
            raise httpx.ReadTimeout("private detail", request=request)
        return httpx.Response(200, json={"acknowledged": True})
    if request.method == "PUT" and phase == "index":
        return httpx.Response(500, json={"private": "detail"})
    if request.url.path.endswith("/_count"):
        return httpx.Response(200, json={"count": 1})
    return httpx.Response(200, json={"version": {"number": "fixture"}})


@pytest.mark.parametrize("phase", ["connection", "index", "cleanup", "changed_input"])
def test_failed_checks_preserve_manifest(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    """Exercise the real CLI finalizer with network failures and input mutation around it."""
    script = Path(__file__).resolve().parents[1] / "scripts/check_opensearch.py"
    namespace = runpy.run_path(str(script))["main"].__globals__
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        pages = base / "pages.jsonl"
        pages.write_text(build_demo_pages()[0].model_dump_json() + "\n")
        output = base / "evidence"

        def arguments(self: Any) -> argparse.Namespace:
            """Supply isolated input/output paths without changing the actual manifest writer."""
            return argparse.Namespace(pages=pages, output=output)

        def handler(request: httpx.Request) -> httpx.Response:
            """Bind the phase and evidence directory to the mock transport."""
            return phase_response(request, phase, output)

        def no_upload(*args: Any) -> None:
            """The failure test focuses on lifecycle rather than redoing bulk ingestion tests."""

        def successful_queries(*args: Any) -> dict[str, str]:
            """Optionally mutate an input after a successful query to test manifest integrity."""
            if phase == "changed_input":
                pages.write_text(pages.read_text() + "\n")
            return {"test": "pass"}

        original_client = httpx.Client

        def client_factory(**kwargs: Any) -> httpx.Client:
            """Keep the real HTTPX request lifecycle but replace only the network transport."""
            return original_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(argparse.ArgumentParser, "parse_args", arguments)
        monkeypatch.setattr(httpx, "Client", client_factory)
        monkeypatch.setitem(namespace, "upload_chunks", no_upload)
        monkeypatch.setitem(namespace, "check_queries", successful_queries)
        with pytest.raises(RuntimeError, match="inspect saved evidence"):
            namespace["main"]()
        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["status"] == "fail"
        assert "private detail" not in json.dumps(manifest)
        assert (output / "requests.jsonl").is_file()
        assert (output / "transport-requests.jsonl").is_file()
        if phase == "cleanup":
            assert manifest["index_deleted"] is False
            assert manifest["cleanup_error_type"] == "ReadTimeout"
        if phase == "changed_input":
            assert manifest["inputs_changed_during_run"] is True
