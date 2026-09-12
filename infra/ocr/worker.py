"""Trusted Windows operator OCR extraction with isolated PDF rendering and durable review output."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from creditlens.ingestion_worker import WorkerFailure
from creditlens.ocr import OcrDocument, normalize_vl
from infra.ocr.completion import classify
from infra.ocr.probe import verify_model
from infra.ocr.supervise import stop_tree


class NativeOcrExtractor:
    """Keep native OCR opt-in for trusted inputs; no SQL authority enters the child process."""

    def __init__(self, python: Path, models: Path, renderer: str, output: Path, *, timeout=900):
        """Only operator configuration selects executable, models, renderer and retained output."""
        if os.name != "nt" or not re.fullmatch(r"sha256:[a-f0-9]{64}", renderer):
            raise ValueError("Windows and a pinned local renderer image are required")
        if not 30 <= timeout <= 1800 or not python.is_file():
            raise ValueError("Invalid OCR interpreter or timeout")
        self.docker = shutil.which("docker")
        if self.docker is None:
            raise ValueError("Docker is required for PDF rendering")
        self.python, self.models = python.resolve(), models.resolve()
        self.renderer, self.output, self.timeout = renderer, output.resolve(), timeout

    def _run(self, command, directory, heartbeat, deadline):
        """Renew current grants while supervising the exact owned process tree and output bounds."""
        environment = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "USERPROFILE"}
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        with (directory / (uuid4().hex + ".log")).open("xb") as log:
            process = subprocess.Popen(  # noqa: S603 - trusted executables and generated local paths.
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                while process.poll() is None:
                    heartbeat()
                    if time.monotonic() > deadline:
                        raise WorkerFailure("worker_timeout", retryable=True)
                    if (
                        sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
                        > 64_000_000
                    ):
                        raise WorkerFailure("extraction_failed", retryable=False)
                    time.sleep(0.5)
                if process.returncode != 0:
                    raise WorkerFailure("extraction_failed", retryable=False)
            finally:
                if process.poll() is None:
                    stop_tree(process)

    def _render(self, root, heartbeat, deadline):
        """Only Poppler sees the PDF, with no network, privileges or writable source mount."""
        name = "creditlens-ocr-render-" + uuid4().hex
        command = [
            self.docker,
            "run",
            "--rm",
            "--name",
            name,
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
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
            "--mount",
            f"type=bind,source={root / 'input'},target=/input,readonly",
            "--mount",
            f"type=bind,source={root / 'rendered'},target=/output",
            self.renderer,
            "-r",
            "144",
            "-scale-to",
            "1754",
            "-png",
            "/input/source.pdf",
            "/output/page",
        ]
        try:
            self._run(command, root, heartbeat, min(deadline, time.monotonic() + 60))
        finally:
            subprocess.run(  # noqa: S603 - exact owned container, including renderer timeout cleanup.
                [self.docker, "rm", "--force", name], capture_output=True, timeout=15, check=False
            )

    def extract(self, data, source, heartbeat):
        """Require all physical pages and observed EOS, then normalize only into quarantine."""
        if source.parser != "ocr" or len(source.pages) > 8 or len(data) > 25_000_000:
            raise WorkerFailure("invalid_source", retryable=False)
        if hashlib.sha256(data).hexdigest() != source.source_sha256:
            raise WorkerFailure("invalid_source", retryable=False)
        root = self.output / uuid4().hex
        (root / "input").mkdir(parents=True)
        (root / "rendered").mkdir()
        (root / "input" / "source.pdf").write_bytes(data)
        deadline = time.monotonic() + self.timeout
        self._render(root, heartbeat, deadline)
        images = sorted(
            (root / "rendered").glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1])
        )
        if len(images) != len(source.pages):
            raise WorkerFailure("invalid_source", retryable=False)
        names = ("PP-DocLayoutV3", "PaddleOCR-VL-1.6")
        for name in names:
            verify_model(self.models, name)
        models_hash = hashlib.sha256(
            b"".join((self.models / f"{name}-manifest.json").read_bytes() for name in names)
        ).hexdigest()
        pages = []
        for number, (image, metadata) in enumerate(zip(images, source.pages, strict=True), start=1):
            output = root / f"recognition-{number}"
            command = [
                str(self.python),
                str(Path(__file__).with_name("probe.py")),
                "--models",
                str(self.models),
                "--image",
                str(image),
                "--output",
                str(output),
            ]
            self._run(command, root, heartbeat, deadline)
            measurement = json.loads((output / "measurement.json").read_text())
            sequences = json.loads((output / "completion.json").read_text())["sequences"]
            complete = bool(sequences) and all(
                classify(row["tokens"], limit=row["max_new_tokens"])["generation_complete"]
                for row in sequences
            )
            image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
            if (
                not complete
                or measurement["input_sha256"] != image_hash
                or measurement["results"] != 1
            ):
                raise WorkerFailure("extraction_failed", retryable=False)
            pages.append(
                normalize_vl(
                    (output / "page-1.json").read_bytes(),
                    metadata,
                    pdf_sha256=source.source_sha256,
                    image_sha256=image_hash,
                    models_sha256=models_hash,
                    generation_complete=True,
                )
            )
        artifact = OcrDocument(pages=tuple(pages))
        (root / "normalized-review.json").write_text(
            artifact.model_dump_json(indent=2), encoding="utf-8"
        )
        heartbeat()
        return artifact
