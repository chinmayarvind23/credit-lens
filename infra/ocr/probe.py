"""Run one trusted synthetic bitmap through pinned, local Paddle models for measurement."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path


def verify_model(root: Path, name: str) -> Path:
    """Verify every recorded model file before inference; never download executable model code."""
    manifest = json.loads((root / f"{name}-manifest.json").read_text(encoding="utf-8"))
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


def main() -> None:
    """Keep experimental CPU inference outside the API; an external supervisor bounds runtime."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    layout = verify_model(args.models, "PP-DocLayoutV3")
    recognition = verify_model(args.models, "PaddleOCR-VL-1.6")
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["OMP_NUM_THREADS"] = "4"
    from paddleocr import PaddleOCRVL
    from PIL import Image

    with Image.open(args.image) as picture:
        if picture.width * picture.height > 4_000_000 or picture.format != "PNG":
            raise ValueError("Probe requires one bounded PNG fixture")
        dimensions = picture.size
    started = time.perf_counter()
    pipeline = PaddleOCRVL(
        pipeline_version="v1.6",
        device="cpu",
        cpu_threads=4,
        enable_mkldnn=False,
        layout_detection_model_name="PP-DocLayoutV3",
        layout_detection_model_dir=str(layout),
        vl_rec_model_name="PaddleOCR-VL-1.6-0.9B",
        vl_rec_model_dir=str(recognition),
        vl_rec_backend="native",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_layout_detection=True,
        use_queues=False,
    )
    loaded = time.perf_counter()
    results = pipeline.predict(str(args.image), max_new_tokens=1024, use_queues=False)
    for index, result in enumerate(results):
        result.save_to_json(str(args.output / f"page-{index + 1}.json"))
    record = {
        "schema_version": 1,
        "input_sha256": hashlib.sha256(args.image.read_bytes()).hexdigest(),
        "dimensions": dimensions,
        "pipeline": "PaddleOCR-VL-v1.6-native-cpu",
        "model_load_seconds": loaded - started,
        "prediction_seconds": time.perf_counter() - loaded,
        "results": len(results),
        "max_new_tokens": 1024,
        "recognition_confidence": None,
        "review_required": True,
    }
    (args.output / "measurement.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
