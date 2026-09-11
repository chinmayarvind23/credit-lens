"""Deployment staging must exclude private files and reject changes after review."""

# Test assertions are deliberate verification, consistent with the repository's tests policy.
# ruff: noqa: S101

import importlib.util
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def sandbox() -> Iterator[Path]:
    """Use a private temp directory without pytest's Windows current-directory symlink helper."""
    with tempfile.TemporaryDirectory(prefix="creditlens-packaging-test-") as directory:
        yield Path(directory)


@pytest.fixture
def deploy() -> ModuleType:
    """Load the standalone script without making deployment tooling an application dependency."""
    source = Path(__file__).resolve().parents[3] / "scripts" / "deploy_space.py"
    spec = importlib.util.spec_from_file_location("deploy_space", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(sandbox: Path, deploy: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use harmless file fixtures while keeping the exact production staging allowlist."""
    root = sandbox / "repo"
    for name in (*deploy.FILES, deploy.CARD):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic source\n", encoding="utf-8")
    (root / ".env").write_text("PRIVATE=do-not-upload\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "borrower.pdf").write_bytes(b"private artifact")
    monkeypatch.setattr(deploy.shutil, "which", find_git)
    monkeypatch.setattr(deploy, "run", git_result)
    return root


def find_git(_name: str) -> str:
    """Avoid probing the host toolchain when the subprocess itself is replaced in the test."""
    return "git"


def git_result(command: list[str], _cwd: Path | None = None) -> str:
    """Record a deterministic source revision without requiring nested Git repositories."""
    return "a" * 40 if "rev-parse" in command else ""


def test_stage_excludes_private_files(repo: Path, sandbox: Path, deploy: ModuleType) -> None:
    """Only exact source paths enter the package; sibling secrets and PDFs stay outside."""
    stage = sandbox / "stage"
    manifest = deploy.stage_package(repo, stage)
    assert set(manifest["files"]) == deploy.TARGETS
    assert not (stage / ".env").exists()
    assert not (stage / "data").exists()
    assert deploy.verify_package(stage)["mode"] == "synthetic-demo"


def test_stage_rejects_repository_output(repo: Path, deploy: ModuleType) -> None:
    """A build snapshot cannot contaminate the source tree or overwrite its parent."""
    with pytest.raises(ValueError, match="outside"):
        deploy.stage_package(repo, repo / "staging")


def test_stage_refuses_existing_output(repo: Path, sandbox: Path, deploy: ModuleType) -> None:
    """Packaging never deletes or overwrites a preexisting review directory."""
    stage = sandbox / "stage"
    stage.mkdir()
    with pytest.raises(ValueError, match="new staging"):
        deploy.stage_package(repo, stage)


def test_changed_source_fails_verification(repo: Path, sandbox: Path, deploy: ModuleType) -> None:
    """Review hashes reject post-staging changes before the upload can start."""
    stage = sandbox / "stage"
    deploy.stage_package(repo, stage)
    (stage / "Dockerfile").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since review"):
        deploy.verify_package(stage)


def test_extra_file_fails_verification(repo: Path, sandbox: Path, deploy: ModuleType) -> None:
    """Adding a secret or artifact after staging cannot expand the reviewed upload."""
    stage = sandbox / "stage"
    deploy.stage_package(repo, stage)
    (stage / ".env").write_text("PRIVATE=hidden\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unexpected or missing"):
        deploy.verify_package(stage)


def test_manifest_cannot_expand_allowlist(repo: Path, sandbox: Path, deploy: ModuleType) -> None:
    """A modified manifest cannot authorize a new path even if an attacker supplies its hash."""
    stage = sandbox / "stage"
    manifest = deploy.stage_package(repo, stage)
    manifest["files"][".env"] = "b" * 64
    (stage / deploy.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="audited upload allowlist"):
        deploy.verify_package(stage)


def test_embedded_credential_blocks_staging(repo: Path, sandbox: Path, deploy: ModuleType) -> None:
    """Allowlisted source is scanned too, rather than relying on file extensions alone."""
    (repo / "Dockerfile").write_text("hf_" + "a" * 30, encoding="utf-8")
    with pytest.raises(ValueError, match="Possible credential"):
        deploy.stage_package(repo, sandbox / "stage")


def test_upload_requires_explicit_destination(sandbox: Path, deploy: ModuleType) -> None:
    """Incomplete destinations fail before authentication or network use."""
    with pytest.raises(ValueError, match="namespace/space-name"):
        deploy.upload_package(sandbox, "creditlens")
