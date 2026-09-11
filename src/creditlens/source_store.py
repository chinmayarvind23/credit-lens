"""Tenant-separated immutable PDF bytes for local ingestion, with verified reads."""

import os
import re
import stat
import tempfile
from hashlib import sha256
from pathlib import Path


class SourceError(ValueError):
    """Invalid or altered sources fail permanently; temporary I/O outages remain distinguishable."""


class LocalSourceStore:
    """Use a trusted configured root; source hashes and tenant IDs never become arbitrary paths."""

    def __init__(self, root: Path, *, max_bytes: int = 25_000_000) -> None:
        """Bound source memory and disk usage independently of PDF parser behavior."""
        if not 1 <= max_bytes <= 25_000_000:
            raise ValueError("Source size limit must be 1..25000000 bytes")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def path(self, tenant_id: str, digest: str) -> Path:
        """Reject traversal and symlink escapes even between two tenants inside the same root."""
        if (
            not tenant_id
            or len(tenant_id.encode()) > 1024
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            raise SourceError("Invalid source identity")
        tenant_root = self.root / sha256(tenant_id.encode()).hexdigest()
        try:
            candidate = (tenant_root / f"{digest}.pdf").resolve()
        except (OSError, RuntimeError) as error:
            raise SourceError("Source path cannot be resolved in its tenant namespace") from error
        if not candidate.is_relative_to(tenant_root):
            raise SourceError("Source path escapes its tenant namespace")
        return candidate

    def _validate(self, data: bytes) -> None:
        """A PDF header is a type boundary only; structural validation runs inside the parser."""
        if not data.startswith(b"%PDF-") or len(data) > self.max_bytes:
            raise SourceError("Source must be a bounded PDF")

    def stage(self, tenant_id: str, data: bytes) -> str:
        """Fsync then atomically link a new object; never overwrite an existing content identity."""
        self._validate(data)
        digest = sha256(data).hexdigest()
        target = self.path(tenant_id, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".stage-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.read(tenant_id, digest)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def read(self, tenant_id: str, digest: str) -> bytes:
        """Verify bounded actual bytes on every read so altered objects cannot enter extraction."""
        path = self.path(tenant_id, digest)
        if not stat.S_ISREG(path.stat().st_mode):
            raise SourceError("Source object is not a regular file")
        with path.open("rb") as stream:
            data = stream.read(self.max_bytes + 1)
        self._validate(data)
        if sha256(data).hexdigest() != digest:
            raise SourceError("Source bytes do not match the submitted hash")
        return data
