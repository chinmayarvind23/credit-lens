"""Small file fixtures test model admission; actual pinned-model execution has separate evidence."""

import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from creditlens import model_bundle
from creditlens.errors import ServiceError


def test_exact_inventory_and_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changed bytes, extra loading configuration and missing files prevent model admission."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for name in ("embedding", "reranker"):
            (root / name / "1_Pooling").mkdir(parents=True)
            (root / name / "config.json").write_bytes(b"config")
            (root / name / "1_Pooling" / "config.json").write_bytes(b"pooling")
            (root / name / ".cache").mkdir()
        files = {
            "config.json": sha256(b"config").hexdigest(),
            "1_Pooling/config.json": sha256(b"pooling").hexdigest(),
        }
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps({name: {"files": files} for name in ("embedding", "reranker")}),
            encoding="utf-8",
        )
        monkeypatch.setattr(model_bundle, "MANIFEST", manifest)
        embedding, reranker, revision = model_bundle.verify_bundle(root)
        assert embedding == (root / "embedding").resolve()
        assert reranker == (root / "reranker").resolve()
        assert revision == sha256(manifest.read_bytes()).hexdigest()
        (embedding / "config.json").write_bytes(b"altered")
        with pytest.raises(ServiceError, match="model_bundle_invalid"):
            model_bundle.verify_bundle(root)
        (embedding / "config.json").write_bytes(b"config")
        extra = embedding / "adapter_config.json"
        extra.write_text("{}", encoding="utf-8")
        with pytest.raises(ServiceError, match="model_bundle_invalid"):
            model_bundle.verify_bundle(root)
        extra.unlink()
        (embedding / "config.json").unlink()
        with pytest.raises(ServiceError, match="model_bundle_invalid"):
            model_bundle.verify_bundle(root)
        manifest.write_text("invalid", encoding="utf-8")
        with pytest.raises(ServiceError, match="model_bundle_invalid"):
            model_bundle.verify_bundle(root)


@pytest.mark.parametrize("escape", ["directory", "file", "symlink"])
def test_model_path_escape_fails(escape: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject path resolution faults to exercise containment without depending on Windows links."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        nested = root / "pool"
        nested.mkdir()
        path = nested / "config.json"
        path.write_bytes(b"config")
        original = Path.resolve

        def resolve(self, *args, **kwargs):
            """Model an external resolved target at exactly one checked filesystem boundary."""
            target = nested if escape == "directory" else path
            return (
                root.parent
                if self == target and escape != "symlink"
                else original(self, *args, **kwargs)
            )

        monkeypatch.setattr(Path, "resolve", resolve)
        if escape == "symlink":
            monkeypatch.setattr(Path, "is_symlink", lambda self: self == path)
        with pytest.raises(ValueError, match="escapes"):
            model_bundle.verify_snapshot(root, {"pool/config.json": sha256(b"config").hexdigest()})
