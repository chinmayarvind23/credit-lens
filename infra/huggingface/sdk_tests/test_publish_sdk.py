"""Actual optional HF exception contracts are tested without contacting the Hub."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from huggingface_hub.errors import RepositoryNotFoundError

from scripts.publish_static_preview import http_status
from tests.test_static_preview import test_publish_lifecycle as exercise_publish


def test_actual_sdk_missing_space_creates_only_static(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK's non-HTTPStatusError 404 triggers only the approved Static create call."""
    with TemporaryDirectory(prefix="creditlens-hf-sdk-") as directory:
        exercise_publish(Path(directory), monkeypatch, False, True, RepositoryNotFoundError)


def test_actual_sdk_status_is_preserved() -> None:
    """Failure audit classification handles SDK response-bearing errors."""
    import httpx

    response = httpx.Response(403, request=httpx.Request("GET", "https://huggingface.co"))
    assert http_status(RepositoryNotFoundError("Denied", response=response)) == 403
