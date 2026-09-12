"""Opt-in logical restore drill owns its container and never connects to shared services."""

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from datetime import date
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, select
from sqlalchemy.engine import Engine

from creditlens.corpus import build_demo_pages
from creditlens.domain import Packet, QueryRequest
from creditlens.errors import ServiceError
from creditlens.source_store import LocalSourceStore, SourceError
from creditlens.sql_catalog import SqlEvidenceCatalog, initialize_catalog
from creditlens.storage import GrantStore, audit_events, grants, open_database
from creditlens.workflow import QueryWorkflow

IMAGE = (
    "postgres:16.4-alpine@sha256:5660c2cbfea50c7a9127d17dc4e48543eedd3d7a41a595a2dfa572471e37e64c"
)


def command(*args: str, data: bytes | None = None) -> bytes:
    """Use fixed local subprocess arguments and bounded waits; no shell or service downloads."""
    return subprocess.run(  # noqa: S603
        args, input=data, capture_output=True, check=True, timeout=45
    ).stdout


@pytest.fixture
def owned_postgres() -> Iterator[tuple[str, str, str]]:
    """Create one bounded private fixture only after explicit opt-in and cached-image validation."""
    if os.getenv("CREDITLENS_RUN_RECOVERY_DRILL") != "1":
        pytest.skip("Opt in to a new isolated cached-image PostgreSQL recovery fixture")
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Docker is required for the explicitly requested recovery drill")
    command(docker, "image", "inspect", IMAGE)
    owner = uuid4().hex
    container = (
        command(
            docker,
            "run",
            "--detach",
            "--pull",
            "never",
            "--rm",
            "--name",
            "creditlens-recovery-" + owner,
            "--label",
            "creditlens.recovery=" + owner,
            "--memory",
            "256m",
            "--cpus",
            "0.5",
            "-e",
            "POSTGRES_PASSWORD=recovery-test-only",
            "-e",
            "POSTGRES_DB=creditlens_test",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        )
        .decode()
        .strip()
    )
    try:
        deadline = monotonic() + 35
        while True:
            try:
                # The image's temporary bootstrap server is socket-only; wait for final TCP service.
                command(
                    docker,
                    "exec",
                    container,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "postgres",
                    "-d",
                    "creditlens_test",
                )
                break
            except subprocess.CalledProcessError:
                if monotonic() > deadline:
                    raise
                sleep(0.25)
        state = json.loads(command(docker, "inspect", container))[0]
        binding = state["NetworkSettings"]["Ports"]["5432/tcp"][0]
        assert binding["HostIp"] == "127.0.0.1"
        url = "postgresql+psycopg://postgres:recovery-test-only@127.0.0.1:"
        yield docker, container, url + binding["HostPort"] + "/"
    finally:
        state = json.loads(command(docker, "inspect", container))[0]
        assert state["Config"]["Labels"]["creditlens.recovery"] == owner
        command(docker, "rm", "--force", container)


def table_fingerprints(engine: Engine) -> dict[str, dict[str, str | int]]:
    """Compare every restored application row, including protected audit JSON and revoked flags."""
    metadata = MetaData()
    metadata.reflect(engine)
    result: dict[str, dict[str, str | int]] = {}
    with engine.connect() as connection:
        for table in metadata.sorted_tables:
            rows = [dict(row) for row in connection.execute(select(table)).mappings()]
            serialized = sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
            result[table.name] = {
                "rows": len(rows),
                "sha256": sha256("\n".join(serialized).encode()).hexdigest(),
            }
    return result


def verify_audit(engine: Engine) -> None:
    """Recompute the saved pre-timing packet digest instead of trusting copied audit hash fields."""
    with engine.connect() as connection:
        records = connection.execute(select(audit_events.c.event)).scalars().all()
    assert records
    for event in records:
        packet = Packet.model_validate(event["protected_packet"])
        assert sha256(packet.model_dump_json().encode()).hexdigest() == event["packet_hash"]


def source_round_trip(root: Path) -> dict[str, str | bool]:
    """Recover an independent source object and prove altered backup bytes fail hash admission."""
    source = LocalSourceStore(root / "source")
    # This is an object-store identity fixture, not a PDF parsing or semantic-quality claim.
    payload = b"%PDF-1.4\n% recovery object fixture\n%%EOF\n"
    digest = source.stage("demo-bank", payload)
    backup = root / "source-backup.pdf"
    backup.write_bytes(source.read("demo-bank", digest))
    source.path("demo-bank", digest).unlink()
    with pytest.raises(FileNotFoundError):
        source.read("demo-bank", digest)
    restored = LocalSourceStore(root / "restored-source")
    assert restored.stage("demo-bank", backup.read_bytes()) == digest
    assert restored.read("demo-bank", digest) == payload
    restored.path("demo-bank", digest).write_bytes(payload + b"altered")
    with pytest.raises(SourceError, match="hash"):
        restored.read("demo-bank", digest)
    return {"source_sha256": digest, "restored_bytes_equal": True, "tampering_rejected": True}


def test_logical_restore_preserves_snapshot_and_exposes_recovery_point(
    owned_postgres: tuple[str, str, str],
) -> None:
    """Restore saved revocations/audits while explicitly showing post-backup changes are absent."""
    docker, container, base = owned_postgres
    engine = open_database(base + "creditlens_test")
    restored_engine: Engine | None = None
    try:
        store = GrantStore(engine)
        store.seed_demo()
        principal = store.resolve("synthetic-demo")
        with engine.begin() as connection:
            connection.execute(
                grants.insert().values(
                    **principal.model_dump(exclude={"subject"}),
                    subject="disabled-reviewer",
                    enabled=False,
                )
            )
        catalog = initialize_catalog(engine, "synthetic-recovery")
        catalog.publish(build_demo_pages())
        query = QueryRequest(
            borrower_id="borrower-001",
            question="Prepare DSCR underwriting packet",
            effective_at=date(2026, 6, 1),
        )
        workflow = QueryWorkflow(catalog, store)
        first = workflow.query(query, principal)
        revoked = next(chunk for chunk in first.evidence if chunk.section == "financial_summary")
        catalog.revoke(revoked.chunk_id)
        before = workflow.query(query, principal)
        assert before.abstained and all(c.chunk_id != revoked.chunk_id for c in before.evidence)
        version = catalog.version
        expected = table_fingerprints(engine)
        verify_audit(engine)
        started = monotonic()
        dump = command(
            docker, "exec", container, "pg_dump", "-U", "postgres", "-Fc", "creditlens_test"
        )
        dump_seconds = monotonic() - started
        # A logical backup is a recovery point: later grant revocations are not inside it.
        with engine.begin() as connection:
            connection.execute(
                grants.update()
                .where(grants.c.subject == principal.subject)
                .values(enabled=False, revision=2)
            )
        with pytest.raises(ServiceError, match="access_denied"):
            store.resolve(principal.subject)
        engine.dispose()
        command(docker, "exec", container, "dropdb", "-U", "postgres", "creditlens_test")
        command(
            docker, "exec", container, "createdb", "-U", "postgres", "-T", "template0", "restored"
        )
        started = monotonic()
        command(
            docker,
            "exec",
            "-i",
            container,
            "pg_restore",
            "-U",
            "postgres",
            "-d",
            "restored",
            "--single-transaction",
            "--exit-on-error",
            data=dump,
        )
        restore_seconds = monotonic() - started
        restored_engine = open_database(base + "restored")
        assert table_fingerprints(restored_engine) == expected
        verify_audit(restored_engine)
        recovered_store = GrantStore(restored_engine)
        recovered_store.seed_demo()
        with pytest.raises(ServiceError, match="access_denied"):
            recovered_store.resolve("disabled-reviewer")
        recovered_catalog = SqlEvidenceCatalog(restored_engine, "synthetic-recovery")
        assert recovered_catalog.version == version
        recovered_catalog.publish(build_demo_pages())
        assert recovered_catalog.version == version
        recovered_principal = recovered_store.resolve(principal.subject)
        assert (
            recovered_principal == principal
        )  # Post-backup revocation is lost, not silently repaired.
        packet = QueryWorkflow(recovered_catalog, recovered_store).query(query, recovered_principal)
        assert packet.abstained and all(c.chunk_id != revoked.chunk_id for c in packet.evidence)
        assert packet.request_id not in {first.request_id, before.request_id}
        with restored_engine.connect() as connection:
            audit_ids = connection.execute(select(audit_events.c.request_id)).scalars().all()
        assert set(audit_ids) == {first.request_id, before.request_id, packet.request_id}
        verify_audit(restored_engine)
        with TemporaryDirectory(prefix="creditlens-source-recovery-") as temporary:
            source_result = source_round_trip(Path(temporary))
        report = {
            "status": "passed",
            "image": IMAGE,
            "tables_at_backup": expected,
            "dump_sha256": sha256(dump).hexdigest(),
            "dump_bytes": len(dump),
            "dump_seconds": dump_seconds,
            "restore_seconds": restore_seconds,
            "catalog_version": version,
            "saved_revocation_preserved": True,
            "saved_disabled_grant_preserved": True,
            "audit_hashes_verified": True,
            "fresh_post_restore_audit": True,
            "post_backup_grant_revocation_lost": True,
            "source_object": source_result,
            "scope": "Local logical snapshot; no PITR, roles, retention or failover proof",
        }
        print("RECOVERY_RESULT=" + json.dumps(report, sort_keys=True))
    finally:
        engine.dispose()
        if restored_engine is not None:
            restored_engine.dispose()
