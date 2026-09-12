"""Protect the existing public demo recording during an app-only browser release."""

import unittest
from hashlib import sha256

import httpx

from scripts.publish_browser_space import retained_demo
from scripts.publish_static_preview import verify_remote


class RecordingTests(unittest.TestCase):
    """Use controlled HTTP bytes to check immutable retention and post-publication verification."""

    def test_retains_only_reviewed_gif(self) -> None:
        """The current immutable recording is hashed without treating it as an upload asset."""
        content = b"GIF89a-synthetic-test"
        urls = []

        def handle(request: httpx.Request) -> httpx.Response:
            """Record the immutable revision URL requested by the publisher."""
            urls.append(str(request.url))
            return httpx.Response(200, content=content)

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            self.assertEqual(retained_demo(client, "owner/demo", "abc", set()), {})
            self.assertEqual(urls, [])
            expected = retained_demo(client, "owner/demo", "abc", {"demo.gif"})
            self.assertEqual(expected, {"demo.gif": sha256(content).hexdigest()})
            self.assertEqual(
                urls, ["https://huggingface.co/spaces/owner/demo/resolve/abc/demo.gif"]
            )

    def test_rejects_invalid_recording(self) -> None:
        """An HTTP error or non-GIF response must stop before any publication mutation."""
        for status, content in ((404, b"missing"), (200, b"<html>error</html>")):
            with httpx.Client(
                transport=httpx.MockTransport(
                    lambda request, status=status, content=content: httpx.Response(
                        status, content=content
                    )
                )
            ) as client:
                with self.assertRaises((ValueError, httpx.HTTPStatusError)):
                    retained_demo(client, "owner/demo", "abc", {"demo.gif"})

    def test_post_publish_detects_changed_recording(self) -> None:
        """Keeping the filename alone is insufficient; remote immutable bytes must match."""

        class Api:
            """Only inventory is reached before the deliberately corrupted hash fails."""

            def list_repo_files(self, *args, **kwargs):
                """Simulate a retained filename whose content changed unexpectedly."""
                return ["demo.gif"]

        with httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"GIF89a-changed")
            )
        ) as client:
            with self.assertRaisesRegex(ValueError, "Published bytes differ"):
                verify_remote(Api(), client, "owner/demo", "abc", {"demo.gif": "wrong"}, set())
