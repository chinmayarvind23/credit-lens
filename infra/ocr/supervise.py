"""Bound a local Windows OCR experiment and terminate its complete owned process tree."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def stop_tree(process: subprocess.Popen) -> None:
    """Windows virtualenv launchers spawn children, so killing only the launcher leaks inference."""
    taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
    subprocess.run(  # noqa: S603 - absolute Windows tool and the exact child PID, never shell input.
        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
        capture_output=True,
        timeout=15,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    process.wait(timeout=15)


def main() -> None:
    """Record timeout/exit status separately from model output; no timeout can count as success."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--engine", choices=("vl", "ocr-v6"), default="vl")
    args = parser.parse_args()
    if os.name != "nt" or not 10 <= args.timeout <= 1800:
        raise ValueError("This supervisor requires Windows and a 10..1800 second limit")
    log_path = args.output.with_suffix(".log")
    record_path = args.output.with_suffix(".supervision.json")
    if args.output.exists() or log_path.exists() or record_path.exists():
        raise ValueError("Use a new experiment output path")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(args.python.resolve()),
        str(Path(__file__).with_name("probe.py")),
        "--models",
        str(args.models.resolve()),
        "--image",
        str(args.image.resolve()),
        "--output",
        str(args.output.resolve()),
        "--engine",
        args.engine,
    ]
    # Local inference needs OS paths, not cloud credentials or paid-provider endpoints.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "USERPROFILE"}
    }
    environment["PYTHONIOENCODING"] = "utf-8"
    started = time.monotonic()
    status = "EXITED"
    with log_path.open("xb") as log:
        process = subprocess.Popen(  # noqa: S603 - fixed probe and explicit local path arguments.
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            while process.poll() is None:
                if time.monotonic() - started > args.timeout:
                    status = "TIMED_OUT"
                    stop_tree(process)
                elif log_path.stat().st_size > 8_000_000:
                    status = "LOG_LIMIT"
                    stop_tree(process)
                else:
                    time.sleep(0.2)
        finally:
            if process.poll() is None:
                stop_tree(process)
    record = {
        "status": status,
        "exit_code": process.returncode,
        "elapsed_seconds": time.monotonic() - started,
        "timeout_seconds": args.timeout,
        "source_image": str(args.image.resolve()),
    }
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
