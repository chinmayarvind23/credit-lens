"""Public preview staging rejects altered captures and unauthorized evidence."""

import copy
import json
from collections.abc import Iterator
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import scripts.publish_static_preview as publisher
from creditlens.api import create_app
from creditlens.settings import Settings
from scripts.build_static_preview import MANIFEST, static_inventory, verify_static
from scripts.capture_preview import SCENARIOS, capture_example, validate_capture, verify_runtime
from scripts.publish_static_preview import free_static, verify_remote


@pytest.fixture
def temp_dir() -> Iterator[Path]:
    """Avoid pytest's unsupported current-directory symlink on this Windows filesystem."""
    with TemporaryDirectory(prefix="creditlens-preview-") as directory:
        yield Path(directory)


@pytest.fixture(scope="module")
def captures() -> dict[str, Any]:
    """Exercise actual API serialization using only the built-in synthetic demo."""
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        names = {
            b["borrower_id"]: b["name"] for b in client.get("/api/v1/borrowers").json()["borrowers"]
        }
        examples = [capture_example(client, scenario, names) for scenario in SCENARIOS]
    return {
        "schema_version": 1,
        "mode": "recorded-synthetic-api",
        "source_revision": "a" * 40,
        "runtime_image": "sha256:" + "b" * 64,
        "captured_at": "2026-09-11T12:00:00+00:00",
        "examples": examples,
    }


def replace_body(record: dict[str, Any], body: Any) -> None:
    """Rehash deliberate structured changes to exercise validation beyond integrity checks."""
    record["body"] = json.dumps(body)
    record["sha256"] = sha256(record["body"].encode()).hexdigest()


def test_actual_api_recordings_validate(captures: dict[str, Any]) -> None:
    """All five actual API examples retain their source-response correspondence."""
    assert validate_capture(captures) == captures
    assert {
        json.loads(e["response"]["body"])["policy_disposition"] for e in captures["examples"]
    } == {"MEETS_POLICY", "EXCEPTION_REQUIRED", "INSUFFICIENT_EVIDENCE", "MATERIAL_CONFLICT"}


@pytest.mark.parametrize("mutation", ["hash", "acl", "scope", "source", "path", "time", "extra"])
def test_invalid_recording_rejected(captures: dict[str, Any], mutation: str) -> None:
    """Integrity, publication scope, source correspondence, and exact fields fail closed."""
    payload = copy.deepcopy(captures)
    example = payload["examples"][0]
    if mutation == "hash":
        example["response"]["body"] += " "
    elif mutation in {"acl", "scope"}:
        body = json.loads(example["response"]["body"])
        if mutation == "acl":
            body["evidence"][0]["acl_groups"] = ["restricted"]
        else:
            body["evidence"][0]["tenant_id"] = "private-bank"
        replace_body(example["response"], body)
    elif mutation == "source":
        example["sources"].pop()
    elif mutation == "path":
        example["sources"][0]["path"] = "/api/v1/evidence/wrong"
    elif mutation == "time":
        example["response"]["elapsed_ms"] = float("nan")
    else:
        payload["unexpected"] = "unwanted field"
    with pytest.raises(ValueError):
        validate_capture(payload)


@pytest.mark.parametrize(
    "address", ["https://127.0.0.1", "http://example.com", "http://127.0.0.1/#x"]
)
def test_capture_requires_loopback_origin(address: str, temp_dir: Path) -> None:
    """Reject remote, TLS, and fragment-bearing capture origins before running Docker."""
    with pytest.raises(ValueError, match="loopback"):
        verify_runtime(address, "test", temp_dir)


def test_static_package_exact_inventory(temp_dir: Path, captures: dict[str, Any]) -> None:
    """A validated artifact fails verification when an unexpected file or changed byte appears."""
    tmp_path = temp_dir
    content = {
        "index.html": "Recorded synthetic API examples",
        "README.md": "---\nsdk: static\napp_file: index.html\n---\n",
        "recordings.json": json.dumps(captures),
        "preview-abc.js": "void 0;",
        "preview-def.css": "body{}",
    }
    for name, value in content.items():
        (tmp_path / name).write_text(value, encoding="utf-8")
    manifest = {
        "mode": "recorded-synthetic-preview",
        "files": {name: sha256((tmp_path / name).read_bytes()).hexdigest() for name in content},
    }
    (tmp_path / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_static(tmp_path) == manifest
    (tmp_path / "extra.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected files"):
        verify_static(tmp_path)
    (tmp_path / "extra.txt").unlink()
    (tmp_path / "index.html").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="file changed"):
        verify_static(tmp_path)


def test_backend_file_cannot_enter_static_inventory() -> None:
    """An executable backend is not part of the permitted free Static artifact."""
    with pytest.raises(ValueError):
        static_inventory({"index.html", "README.md", "recordings.json", "app.py", "preview-a.css"})


@pytest.mark.parametrize(
    "sdk,private,hardware",
    [("docker", False, None), ("static", True, None), ("static", False, "cpu-basic")],
)
def test_publisher_refuses_compute_or_private_space(
    sdk: str, private: bool, hardware: str | None
) -> None:
    """Even nominally free CPU compute is outside the authorized Static publication path."""

    class Api:
        """Expose only read methods so this gate cannot mutate destination settings."""

        def space_info(self, repo_id: str) -> Any:
            """Return the configured destination metadata."""
            return SimpleNamespace(sdk=sdk, private=private)

        def get_space_runtime(self, repo_id: str) -> Any:
            """Return hardware reported by the server, including pending requests."""
            return SimpleNamespace(hardware=hardware, requested_hardware=None)

    with pytest.raises(ValueError, match="without compute"):
        free_static(Api(), "owner/space")


def test_publisher_rejects_changed_remote_bytes() -> None:
    """HTTP success cannot stand in for matching the reviewed artifact digest."""

    class Api:
        """Supply the expected inventory for a deliberately altered response body."""

        def list_repo_files(self, *args: Any, **kwargs: Any) -> list[str]:
            """Keep this test focused on remote content integrity."""
            return ["index.html"]

    def response(request: httpx.Request) -> httpx.Response:
        """Simulate a successful download whose contents changed after staging."""
        return httpx.Response(200, content=b"changed")

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(ValueError, match="Published bytes differ"):
            verify_remote(
                Api(),
                client,
                "owner/space",
                "a" * 40,
                {"index.html": sha256(b"original").hexdigest()},
                set(),
            )


@pytest.mark.parametrize("fail_download", [False, True])
def test_publish_lifecycle(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_download: bool,
    create: bool = False,
    error_type: Any = httpx.HTTPStatusError,
) -> None:
    """A single bounded upload retains its revision in the audit even if verification fails."""
    stage = temp_dir / "stage"
    stage.mkdir()
    (stage / "index.html").write_bytes(b"reviewed")
    manifest = {
        "ui_source_tree_dirty": False,
        "files": {"index.html": sha256(b"reviewed").hexdigest()},
    }
    (stage / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    def verified(path: Path) -> dict[str, Any]:
        """Isolate lifecycle behavior from independently tested stage parsing."""
        assert path == stage
        return manifest

    monkeypatch.setattr(publisher, "verify_static", verified)
    calls: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []

    class Api:
        """Expose only approved reads and one upload; other cloud mutations cannot occur."""

        def whoami(self) -> dict[str, str]:
            """Use the authenticated owner identity without exposing a credential."""
            return {"name": "owner"}

        def space_info(self, repo_id: str) -> Any:
            """Return a public Static destination with a pinned initial revision."""
            assert repo_id == "owner/space"
            if create and not created:
                request = httpx.Request("GET", "https://huggingface.co/api/spaces/owner/space")
                raise error_type("Not found", response=httpx.Response(404, request=request))
            return SimpleNamespace(sdk="static", private=False, sha="parent")

        def create_repo(self, **kwargs: Any) -> None:
            """Capture only the explicitly free Static creation parameters."""
            created.append(kwargs)

        def get_space_runtime(self, repo_id: str) -> Any:
            """Represent a destination with no compute or pending upgrade."""
            return SimpleNamespace(hardware=None, requested_hardware=None)

        def list_repo_files(self, repo_id: str, **kwargs: Any) -> list[str]:
            """Inspect initial files before upload and exact result afterward."""
            return (
                [".gitattributes"]
                if kwargs["revision"] == "parent"
                else [".gitattributes", "index.html", MANIFEST]
            )

        def upload_folder(self, **kwargs: Any) -> Any:
            """Record the sole consequential call and return its immutable revision."""
            calls.append(kwargs)
            return SimpleNamespace(oid="uploaded")

    def response(request: httpx.Request) -> httpx.Response:
        """Download only exact expected public revision paths without authorization."""
        assert "authorization" not in request.headers
        assert "/resolve/uploaded/" in request.url.path
        name = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(
            200, content=b"wrong" if fail_download else (stage / name).read_bytes()
        )

    audit = temp_dir / "audit.json"
    outcome = (
        pytest.raises(ValueError, match="Published bytes differ")
        if fail_download
        else nullcontext()
    )
    with httpx.Client(transport=httpx.MockTransport(response)) as client, outcome:
        assert publisher.publish(Api(), client, "owner/space", stage, audit)["status"] == "verified"
    assert len(calls) == 1
    assert calls[0]["parent_commit"] == "parent"
    assert calls[0]["allow_patterns"] == sorted(["index.html", MANIFEST])
    assert calls[0]["repo_id"] == "owner/space"
    saved = json.loads(audit.read_text(encoding="utf-8"))
    assert saved["revision"] == "uploaded"
    assert saved["status"] == ("failed" if fail_download else "verified")
    assert saved["finished_at"]
    expected_creation = (
        [
            {
                "repo_id": "owner/space",
                "repo_type": "space",
                "space_sdk": "static",
                "private": False,
            }
        ]
        if create
        else []
    )
    assert created == expected_creation


def test_pending_hardware_request_is_rejected() -> None:
    """A queued upgrade is refused even before compute hardware becomes active."""

    class Api:
        """Provide a static destination that reports a pending hardware change."""

        def space_info(self, repo_id: str) -> Any:
            """Return otherwise permitted destination metadata."""
            return SimpleNamespace(sdk="static", private=False)

        def get_space_runtime(self, repo_id: str) -> Any:
            """Expose the pending upgrade to the guard."""
            return SimpleNamespace(hardware=None, requested_hardware="cpu-upgrade")

    with pytest.raises(ValueError, match="without compute"):
        free_static(Api(), "owner/space")
