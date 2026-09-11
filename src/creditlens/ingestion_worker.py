"""Execute digital jobs in bounded parser containers while the parent owns database access."""

import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol
from uuid import uuid4

from creditlens.domain import StrictModel
from creditlens.errors import ServiceError
from creditlens.ingestion_jobs import FailureCode, IngestionInput, JobLease, JobStore
from creditlens.pdf_worker import ParsedDocument
from creditlens.source_store import LocalSourceStore, SourceError
from creditlens.sql_catalog import SqlEvidenceCatalog


class WorkerFailure(Exception):
    """Only curated failure codes and retry policy cross the isolated parser boundary."""

    def __init__(self, code: FailureCode, *, retryable: bool) -> None:
        """Preserve recovery intent without retaining arbitrary child-process exception text."""
        self.code, self.retryable = code, retryable
        super().__init__(code)


class PdfExtractor(Protocol):
    """The worker can test failure boundaries without granting the parser database access."""

    def extract(
        self, data: bytes, source: IngestionInput, heartbeat: Callable[[], None]
    ) -> ParsedDocument:
        """Return verified source-bound pages or a typed failure while keeping ownership current."""
        ...


class WorkerResult(StrictModel):
    """Report durable outcomes separately from a worker that lost ownership or storage access."""

    job_id: str
    state: Literal["COMPLETED", "RETRY", "FAILED", "LEASE_LOST"]
    error_code: str | None = None


class DockerPdfExtractor:
    """A pinned image gets one read-only input directory and no network or host credentials."""

    def __init__(self, image: str, *, timeout_seconds: float = 60) -> None:
        """Disallow mutable image tags and unbounded runtime before accepting any job."""
        executable = shutil.which("docker")
        if executable is None or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("A local Docker executable and pinned image digest are required")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("Digital extraction deadline must be 1..120 seconds")
        self.executable, self.image, self.timeout_seconds = executable, image, timeout_seconds

    def _command(self, name: str, directory: Path) -> list[str]:
        """No document field can select a host path, command or image in these literal arguments."""
        if "," in str(directory):
            raise ValueError("Docker input mount path contains an unsupported comma")
        return [
            self.executable,
            "run",
            "--rm",
            "--name",
            name,
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "1000:1000",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--no-healthcheck",
            "--log-driver",
            "none",
            "--mount",
            f"type=bind,source={directory},target=/input,readonly",
            self.image,
            "/usr/bin/timeout",
            "--kill-after=2s",
            f"{self.timeout_seconds:g}s",
            "/app/.venv/bin/python",
            "-m",
            "creditlens.pdf_worker",
        ]

    def _remove(self, name: str) -> None:
        """Remove only this extractor's randomly named container, including timeout cases."""
        subprocess.run(  # noqa: S603 - trusted Docker executable and an internally generated name.
            [self.executable, "rm", "--force", name],
            capture_output=True,
            timeout=10,
            check=False,
        )

    def _wait(
        self,
        process: subprocess.Popen[bytes],
        paths: tuple[Path, Path],
        heartbeat: Callable[[], None],
    ) -> None:
        """Poll bounded logs and renew authorization while the isolated parser performs CPU work."""
        started, renewed = time.monotonic(), 0.0
        while process.poll() is None:
            now = time.monotonic()
            if now - started > self.timeout_seconds:
                raise WorkerFailure("worker_timeout", retryable=True)
            if paths[0].stat().st_size > 8_388_608 or paths[1].stat().st_size > 1_000_000:
                raise WorkerFailure("extraction_failed", retryable=False)
            if now - renewed >= 1:
                heartbeat()
                renewed = now
            time.sleep(0.1)

    def extract(
        self, data: bytes, source: IngestionInput, heartbeat: Callable[[], None]
    ) -> ParsedDocument:
        """Copy verified bytes into one private job directory and validate bounded child output."""
        name = "creditlens-parser-" + uuid4().hex
        with TemporaryDirectory(prefix="creditlens-parser-") as temporary:
            directory = Path(temporary)
            inputs = directory / "input"
            inputs.mkdir()
            (inputs / "source.pdf").write_bytes(data)
            (inputs / "manifest.json").write_text(source.model_dump_json(), encoding="utf-8")
            output, errors = directory / "output.json", directory / "errors.log"
            with output.open("wb") as stdout, errors.open("wb") as stderr:
                process = subprocess.Popen(  # noqa: S603 - fixed executable and literal owned paths.
                    self._command(name, inputs),
                    stdout=stdout,
                    stderr=stderr,
                )
                try:
                    self._wait(process, (output, errors), heartbeat)
                finally:
                    try:
                        self._remove(name)
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)
            if process.returncode == 124:
                raise WorkerFailure("worker_timeout", retryable=True)
            if process.returncode == 125:
                raise WorkerFailure("worker_unavailable", retryable=True)
            if (
                process.returncode != 0
                or output.stat().st_size > 8_388_608
                or errors.stat().st_size > 1_000_000
            ):
                raise WorkerFailure("extraction_failed", retryable=False)
            return ParsedDocument.model_validate_json(output.read_bytes())


class IngestionWorker:
    """Claim supported jobs, verify current grants and source bytes, then atomically publish."""

    def __init__(
        self,
        jobs: JobStore,
        sources: LocalSourceStore,
        catalog: SqlEvidenceCatalog,
        extractor: PdfExtractor,
    ) -> None:
        """Keep database access in the parent; the child receives bounded source data only."""
        self.jobs, self.sources, self.catalog, self.extractor = jobs, sources, catalog, extractor

    def _failed(self, lease: JobLease, failure: WorkerFailure) -> WorkerResult:
        """Acknowledge failure only after the durable transition succeeds with the current token."""
        self.jobs.fail(lease, failure.code, retryable=failure.retryable)
        return WorkerResult(
            job_id=lease.job_id,
            state="RETRY" if failure.retryable and lease.attempt < 3 else "FAILED",
            error_code="attempts_exhausted"
            if failure.retryable and lease.attempt >= 3
            else failure.code,
        )

    def _execute(self, lease: JobLease) -> None:
        """Reauthorize before and during parsing; reject output bound to another source."""

        def heartbeat() -> None:
            """The database checks current ownership and grant before extending this attempt."""
            self.jobs.heartbeat(lease)

        heartbeat()
        data = self.sources.read(lease.input.pages[0].tenant_id, lease.input.source_sha256)
        result = self.extractor.extract(data, lease.input, heartbeat)
        if result.source_sha256 != lease.input.source_sha256:
            raise WorkerFailure("invalid_source", retryable=False)
        try:
            self.jobs.complete(lease, self.catalog, result.pages)
        except ValueError as error:
            raise WorkerFailure("publication_failed", retryable=False) from error

    def run_one(self, job_id: str | None = None) -> WorkerResult | None:
        """Unsupported OCR jobs remain queued; uncertain storage failures are never acknowledged."""
        lease = self.jobs.claim(job_id, parser="digital")
        if lease is None:
            return None
        try:
            self._execute(lease)
            return WorkerResult(job_id=lease.job_id, state="COMPLETED")
        except SourceError:
            return self._failed(lease, WorkerFailure("invalid_source", retryable=False))
        except OSError:
            return self._failed(lease, WorkerFailure("source_unavailable", retryable=True))
        except WorkerFailure as error:
            return self._failed(lease, error)
        except ValueError:
            return self._failed(lease, WorkerFailure("extraction_failed", retryable=False))
        except ServiceError as error:
            if error.code == "lease_lost":
                return WorkerResult(job_id=lease.job_id, state="LEASE_LOST", error_code=error.code)
            if error.status == 403:
                return self._failed(lease, WorkerFailure("permission_changed", retryable=False))
            raise
