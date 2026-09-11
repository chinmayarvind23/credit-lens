"""Real filesystem checks protect tenant separation and immutable source-byte provenance."""

import os
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from creditlens.source_store import LocalSourceStore, SourceError


def test_source_stage_is_hash_addressed_idempotent_and_tenant_separated() -> None:
    """Same bytes do not make a source object readable through another tenant's namespace."""
    with TemporaryDirectory(prefix="creditlens-source-") as directory:
        store = LocalSourceStore(Path(directory))
        source = b"%PDF-1.7\nsynthetic bytes, parser validation is separate"
        digest = store.stage("tenant-a", source)
        assert digest == sha256(source).hexdigest()
        assert store.stage("tenant-a", source) == digest
        assert store.read("tenant-a", digest) == source
        with pytest.raises(FileNotFoundError):
            store.read("tenant-b", digest)
        assert store.stage("tenant-b", source) == digest
        assert store.path("tenant-a", digest) != store.path("tenant-b", digest)


def test_changed_bytes_and_path_injection_fail() -> None:
    """A known digest is never permission to read arbitrary paths or silently altered evidence."""
    with TemporaryDirectory(prefix="creditlens-source-") as directory:
        store = LocalSourceStore(Path(directory))
        digest = store.stage("tenant-a", b"%PDF-1.7\noriginal")
        store.path("tenant-a", digest).write_bytes(b"%PDF-1.7\nchanged")
        with pytest.raises(SourceError):
            store.read("tenant-a", digest)
        with pytest.raises(SourceError):
            store.stage("tenant-a", b"%PDF-1.7\noriginal")
        for digest in ("../other.pdf", "https://example.com/doc.pdf", "", "G" * 64):
            with pytest.raises(SourceError):
                store.read("tenant-a", digest)


def test_empty_non_pdf_and_oversize_sources_are_rejected() -> None:
    """Bound retained bytes before parsing; a metadata claim cannot establish PDF type."""
    with TemporaryDirectory(prefix="creditlens-source-") as directory:
        store = LocalSourceStore(Path(directory), max_bytes=32)
        for data in (b"", b"not a PDF", b"%PDF-1.7\n" + b"x" * 33):
            with pytest.raises(SourceError):
                store.stage("tenant-a", data)
        with pytest.raises(SourceError):
            store.stage("", b"%PDF-1.7\nsource")
        with pytest.raises(ValueError):
            LocalSourceStore(Path(directory), max_bytes=0)
        assert not list(Path(directory).rglob("*.pdf"))


def test_filesystem_redirect_between_tenants_is_rejected() -> None:
    """An actual junction or symlink inside the root cannot redirect one tenant to another."""
    with TemporaryDirectory(prefix="creditlens-source-") as directory:
        store = LocalSourceStore(Path(directory))
        digest = store.stage("tenant-b", b"%PDF-1.7\nsource")
        target = store.path("tenant-b", digest).parent
        redirect = store.root / sha256(b"tenant-a").hexdigest()
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(redirect))
        else:
            redirect.symlink_to(target, target_is_directory=True)
        try:
            with pytest.raises(SourceError, match="namespace"):
                store.read("tenant-a", digest)
        finally:
            if os.name == "nt":
                os.rmdir(redirect)
            else:
                redirect.unlink()


def test_directory_cannot_be_read_as_a_source() -> None:
    """Reject non-regular objects before opening a potentially blocking filesystem stream."""
    with TemporaryDirectory(prefix="creditlens-nonregular-") as directory:
        store = LocalSourceStore(Path(directory))
        path = store.path("tenant-a", "a" * 64)
        path.mkdir(parents=True)
        with pytest.raises(SourceError, match="regular file"):
            store.read("tenant-a", "a" * 64)
