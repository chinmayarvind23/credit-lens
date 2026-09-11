"""Protocol doubles test the cost boundary and schema handling, never judge quality."""

import argparse
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from local_judge import LocalJudge
from pydantic import BaseModel
from run_deepeval import restrict_network, run


class Result(BaseModel):
    """Require a real schema instance rather than accepting arbitrary model prose."""

    verdict: str


class JudgeTests(unittest.TestCase):
    """Keep transport tests independent of Ollama and paid inference credentials."""

    def setUp(self) -> None:
        """Use a private journal and replace the loopback transport with explicit protocol data."""
        self.directory = tempfile.TemporaryDirectory()
        self.journal = Path(self.directory.name) / "calls.jsonl"
        self.judge = LocalJudge("test:8b", "a" * 64, self.journal, calls=2)
        self.judge.client.close()
        self.paths: list[str] = []
        self.digest = "a" * 64
        self.remote = False
        self.reason = "stop"
        self.judge.client = httpx.Client(
            base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(self.respond)
        )

    def tearDown(self) -> None:
        """Close clients and remove only this test's owned temporary directory."""
        self.judge.close()
        self.directory.cleanup()

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Model the documented local HTTP protocol, recording whether inference was reached."""
        self.paths.append(request.url.path)
        if request.url.path == "/api/tags":
            data = {"models": [{"name": "test:8b", "digest": self.digest}]}
        elif request.url.path == "/api/show":
            data = {"details": {"format": "gguf"}, "remote_host": "remote" if self.remote else ""}
        else:
            body = json.loads(request.content)
            self.assertFalse(body["stream"])
            self.assertEqual(body["options"]["temperature"], 0)
            data = {
                "done": True,
                "done_reason": self.reason,
                "message": {"content": '{"verdict":"yes"}'},
            }
        return httpx.Response(200, json=data)

    def test_schema_and_journal(self) -> None:
        """Completed responses are schema-validated and retained with their original prompt."""
        result = self.judge.generate("Judge this supplied source", Result)
        self.assertIsInstance(result, Result)
        self.assertEqual(result.verdict, "yes")
        self.assertEqual(len(self.journal.read_text().splitlines()), 1)
        self.assertEqual(self.paths.count("/api/tags"), 2)

    def test_changed_digest_prevents_inference(self) -> None:
        """A mutable tag cannot silently switch the model used for an evaluation."""
        self.digest = "b" * 64
        with self.assertRaisesRegex(ValueError, "digest"):
            self.judge.generate("question", Result)
        self.assertNotIn("/api/chat", self.paths)

    def test_remote_descriptor_prevents_inference(self) -> None:
        """Even a matching installed tag cannot route a judgment to a cloud host."""
        self.remote = True
        with self.assertRaisesRegex(ValueError, "Remote"):
            self.judge.generate("question", Result)
        self.assertNotIn("/api/chat", self.paths)

    def test_truncated_generation_fails(self) -> None:
        """A length-limited response is retained for diagnosis but cannot become a score."""
        self.reason = "length"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.judge.generate("question", Result)
        self.assertTrue(self.journal.exists())

    def test_input_and_call_budgets(self) -> None:
        """Limits reject excessive work before network access, including multibyte input."""
        with self.assertRaisesRegex(ValueError, "budget"):
            self.judge.generate("\u2603" * 11000, Result)
        self.judge.remaining = 0
        with self.assertRaisesRegex(ValueError, "budget"):
            self.judge.generate("question", Result)
        self.assertEqual(self.paths, [])

    def test_rejects_cloud_tag(self) -> None:
        """Explicit cloud tags fail before a client or any model call is created."""
        with self.assertRaisesRegex(ValueError, "local"):
            LocalJudge("model:cloud", "a" * 64, self.journal)

    def test_network_boundary(self) -> None:
        """The process hook permits the fixed endpoint and blocks remote or alternate services."""
        restrict_network("socket.connect", (None, ("127.0.0.1", 11434)))
        for address in (("example.com", 443), ("127.0.0.1", 9999), ("::1", 11434)):
            with self.subTest(address=address), self.assertRaises(PermissionError):
                restrict_network("socket.connect", (None, address))

    def test_journals_stay_outside_repo(self) -> None:
        """Reject repository output before installing hooks or creating any journal directory."""
        with self.assertRaisesRegex(ValueError, "outside"):
            run(argparse.Namespace(output=Path(__file__).parent / "private-output"))


if __name__ == "__main__":
    unittest.main()
