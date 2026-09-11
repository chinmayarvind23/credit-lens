"""CPU packaging records normalized source provenance and cannot drift to GPU dependencies."""

# ruff: noqa: S101
import json
import re
from hashlib import sha256
from pathlib import Path


def test_cpu_lock_matches_reviewed_sources() -> None:
    """Fail packaging review when dependencies or their generator changed without regeneration."""
    root = Path(__file__).resolve().parents[3]
    directory = root / "infra" / "retrieval"
    manifest = json.loads((directory / "cpu-lock-manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["source_hashes"].items():
        assert sha256((root / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest() == expected
    lock = (directory / "requirements-cpu.lock").read_bytes().replace(b"\r\n", b"\n")
    assert sha256(lock).hexdigest() == manifest["lock_sha256"]
    packages = dict(re.findall(r"^([a-z0-9-]+)==([^\s]+)", lock.decode(), re.MULTILINE))
    assert packages["torch"] == "2.14.0+cpu"
    assert packages["numpy"] == "2.4.6"
    assert packages["sentence-transformers"] == "5.7.0"
    assert packages["transformers"] == "5.17.0"
    assert not any(name.startswith("nvidia-") or name == "triton" for name in packages)
    assert len(packages) == 41
