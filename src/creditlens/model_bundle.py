"""Admit only the reviewed local model/configuration bytes before any model code loads them."""

import json
from hashlib import file_digest, sha256
from pathlib import Path

from creditlens.errors import ServiceError

MANIFEST = Path(__file__).with_name("model_manifest.json")


def verify_snapshot(directory: Path, expected: dict[str, str]) -> Path:
    """Exact file inventory prevents unreviewed adapters or configuration from altering loading."""
    root = directory.resolve(strict=True)
    parents = {str(Path(name).parent) for name in expected} | {"."}
    for parent in parents:
        folder = root / parent
        if not folder.resolve(strict=True).is_relative_to(root):
            raise ValueError("Model directory escapes the reviewed snapshot")
        for item in folder.iterdir():
            relative = item.relative_to(root).as_posix()
            if relative == ".cache":
                continue
            if relative not in expected and relative not in parents:
                raise ValueError("Unexpected model file or directory")
    for name, digest in expected.items():
        path = root / name
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("Model file escapes the reviewed snapshot")
        with path.open("rb") as handle:
            if file_digest(handle, "sha256").hexdigest() != digest:
                raise ValueError("Model file does not match the reviewed hash")
    return root


def verify_bundle(directory: Path) -> tuple[Path, Path, str]:
    """Use the repository's pinned manifest; callers cannot supply alternative model hashes."""
    try:
        payload = MANIFEST.read_bytes()
        manifest = json.loads(payload)
        embedding = verify_snapshot(directory / "embedding", manifest["embedding"]["files"])
        reranker = verify_snapshot(directory / "reranker", manifest["reranker"]["files"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ServiceError(
            "model_bundle_invalid", "Verified local model files are required"
        ) from exc
    return embedding, reranker, sha256(payload).hexdigest()
