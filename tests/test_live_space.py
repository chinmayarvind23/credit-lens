"""Live embedding must validate its origin and prove distinct backend requests."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest
from PIL import Image

from scripts.publish_live_space import demo_gif, live_html, verify_live


def test_demo_media_requires_decodable_bounded_animation() -> None:
    """Reject a static or renamed image while preserving exact reviewed animation bytes."""
    with TemporaryDirectory() as directory:
        path = Path(directory) / "demo.gif"
        first = Image.new("RGB", (8, 8), "white")
        second = Image.new("RGB", (8, 8), "black")
        first.save(path, format="GIF")
        with pytest.raises(ValueError, match="animated"):
            demo_gif(path)
        first.save(path, format="GIF", save_all=True, append_images=[second], duration=200)
        assert demo_gif(path) == path.read_bytes()
        path.write_bytes(b"not image data")
        with pytest.raises(OSError):
            demo_gif(path)


@pytest.mark.parametrize(
    "origin",
    [
        "http://demo.trycloudflare.com",
        "https://demo.trycloudflare.com/extra",
        "https://demo.trycloudflare.com?credential=value",
        "https://demo.trycloudflare.com.evil.invalid",
        'https://demo.trycloudflare.com" onload="bad',
        "https://localhost:8000",
    ],
)
def test_live_origin_rejects_ambiguous_or_injected_values(origin: str) -> None:
    """Only the declared tunnel authority can enter iframe attributes or content policy."""
    with pytest.raises(ValueError, match="exact HTTPS"):
        live_html(origin, Path("infra/huggingface/live/index.html"))


@pytest.mark.parametrize("fresh", [True, False])
def test_live_probe_requires_current_distinct_requests(fresh: bool) -> None:
    """A prerecorded repeated ID cannot satisfy the live verification contract."""
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Return controlled HTTP packets without creating a network service or cloud resource."""
        if request.url.path == "/ready":
            return httpx.Response(
                200, json={"status": "ready", "mode": "demo", "search": "local-extractive"}
            )
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "request_id": str(len(calls)) if fresh else "recorded",
                "policy_disposition": "EXCEPTION_REQUIRED",
                "evidence": [{"tenant_id": "demo-bank"}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        if fresh:
            assert verify_live(client, "https://test.trycloudflare.com")["fresh_request_ids"] == [
                "1",
                "2",
            ]
        else:
            with pytest.raises(ValueError, match="distinct"):
                verify_live(client, "https://test.trycloudflare.com")
    assert len(calls) == 2 and calls[0] == calls[1]
