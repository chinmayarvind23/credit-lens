"""Run one trusted synthetic bitmap through pinned, local Paddle models for measurement."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

REVISIONS = {
    "PP-DocLayoutV3": "7b48a7566925fa464281f930c58eee04fe2c862a",
    "PaddleOCR-VL-1.6": "c5630abae1d940eafe0697512a0325494b02ab42",
    "PP-OCRv6_tiny_det": "d3177d4e5551463292a61e27cfca2b53e7c3fe9d",
    "PP-OCRv6_tiny_rec": "0736086f72f666350ebcdc0c3a504eeac89cdfad",
}


def verify_model(root: Path, name: str) -> Path:
    """Verify every recorded model file before inference; never download executable model code."""
    manifest = json.loads((root / f"{name}-manifest.json").read_text(encoding="utf-8"))
    if manifest["revision"] != REVISIONS[name] or manifest["repo"] != f"PaddlePaddle/{name}":
        raise ValueError("Model revision does not match the experiment")
    if not manifest["sha256"]:
        raise ValueError("Model manifest must list verified files")
    directory = root / name
    for relative, expected in manifest["sha256"].items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("Model manifest path escapes its directory")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError("Model file hash mismatch")
    return directory


def build_pipeline(models: Path, engine: str):
    """Compare explicit local engines without mixing their outputs or confidence meanings."""
    from paddleocr import PaddleOCR, PaddleOCRVL

    options = {
        "device": "cpu",
        "cpu_threads": 4,
        "enable_mkldnn": False,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
    }
    if engine == "ocr-v6":
        return PaddleOCR(
            **options,
            text_detection_model_name="PP-OCRv6_tiny_det",
            text_detection_model_dir=str(verify_model(models, "PP-OCRv6_tiny_det")),
            text_recognition_model_name="PP-OCRv6_tiny_rec",
            text_recognition_model_dir=str(verify_model(models, "PP-OCRv6_tiny_rec")),
            use_textline_orientation=False,
        )
    return PaddleOCRVL(
        **options,
        pipeline_version="v1.6",
        layout_detection_model_name="PP-DocLayoutV3",
        layout_detection_model_dir=str(verify_model(models, "PP-DocLayoutV3")),
        vl_rec_model_name="PaddleOCR-VL-1.6-0.9B",
        vl_rec_model_dir=str(verify_model(models, "PaddleOCR-VL-1.6")),
        vl_rec_backend="native",
        use_layout_detection=True,
        use_queues=False,
    )


def main() -> None:
    """Keep experimental CPU inference outside the API; an external supervisor bounds runtime."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine", choices=("vl", "ocr-v6"), default="vl")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["OMP_NUM_THREADS"] = "4"
    from PIL import Image

    with Image.open(args.image) as picture:
        if picture.width * picture.height > 4_000_000 or picture.format != "PNG":
            raise ValueError("Probe requires one bounded PNG fixture")
        dimensions = picture.size
    started = time.perf_counter()
    pipeline = build_pipeline(args.models, args.engine)
    loaded = time.perf_counter()
    options = {"max_new_tokens": 1024, "use_queues": False} if args.engine == "vl" else {}
    results = list(pipeline.predict(str(args.image), **options))
    for index, result in enumerate(results):
        result.save_to_json(str(args.output / f"page-{index + 1}.json"))
    record = {
        "schema_version": 1,
        "input_sha256": hashlib.sha256(args.image.read_bytes()).hexdigest(),
        "dimensions": dimensions,
        "pipeline": "PaddleOCR-VL-v1.6-native-cpu" if args.engine == "vl" else "PP-OCRv6-tiny-cpu",
        "model_load_seconds": loaded - started,
        "prediction_seconds": time.perf_counter() - loaded,
        "results": len(results),
        "max_new_tokens": 1024 if args.engine == "vl" else None,
        "recognition_confidence": None if args.engine == "vl" else "uncalibrated-per-line",
        "review_required": True,
    }
    (args.output / "measurement.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
