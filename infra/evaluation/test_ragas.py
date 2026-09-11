"""Use explicit protocol doubles to test real RAGAS wiring, not to claim judge quality."""

# Privacy initialization must precede the SDK import below.
# ruff: noqa: I001

import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx
from ragas_judge import LocalJudge, RagasJudge
from ragas_profiles import configure
from run_ragas import read_cases, validate_score

from ragas.metrics.collections import Faithfulness


class RagasTests(unittest.TestCase):
    """Guard extraction denominators, schema transport and startup privacy ordering."""

    def test_profile_does_not_change_stock_metric(self) -> None:
        """A domain experiment must not mutate the shared library defaults or another metric."""
        judge = RagasJudge(Mock(spec=LocalJudge))
        stock = Faithfulness(llm=judge)
        original = configure(stock, "stock")
        experiment = Faithfulness(llm=judge)
        changed = configure(experiment, "lending-v1")
        self.assertNotEqual(changed, original)
        self.assertEqual(configure(stock, "stock"), original)
        self.assertEqual(configure(Faithfulness(llm=judge), "stock"), original)
        self.assertEqual(
            stock.statement_generator_prompt.examples,
            experiment.statement_generator_prompt.examples,
        )
        with self.assertRaisesRegex(ValueError, "Unknown"):
            configure(experiment, "unversioned")

    def test_derivation_cases_preserve_atomic_answers(self) -> None:
        """Frozen extraction expectations cover the entire answer, including wrong claims."""
        _, cases = read_cases(Path(__file__).with_name("controls-derivation-v1.jsonl"))
        self.assertEqual(len(cases), 12)
        self.assertEqual(sum(c["expected_range"] == [1, 1] for c in cases), 3)
        for case in cases:
            self.assertEqual(case["expected_statements"], [case["actual_output"]])

    def test_library_metric_uses_local_schema_transport(self) -> None:
        """Exercise both unmodified RAGAS prompts through the real adapter and mocked HTTP only."""
        with tempfile.TemporaryDirectory() as directory:
            transport = LocalJudge("test:8b", "a" * 64, Path(directory) / "calls.jsonl", calls=2)
            transport.client.close()
            transport.client = httpx.Client(
                base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(self.respond)
            )
            judge = RagasJudge(transport)
            try:
                result = asyncio.run(
                    Faithfulness(llm=judge).ascore(
                        user_input="What are the facts?",
                        response="First fact. Second fact.",
                        retrieved_contexts=[
                            "First fact is supported; second fact is contradicted."
                        ],
                    )
                )
                self.assertEqual(result.value, 0.5)
                self.assertEqual(transport.remaining, 0)
                validate_score(result.value, judge.outputs)
                self.assertEqual(len(transport.journal.read_text().splitlines()), 2)
            finally:
                transport.close()

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Provide explicit two-claim judgments while checking the SDK's requested schema."""
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "test:8b", "digest": "a" * 64}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {"format": "gguf"}})
        self.assertEqual(request.url.path, "/api/chat")
        schema = json.loads(request.content)["format"]["title"]
        outputs = self.outputs()
        result = next(item["output"] for item in outputs if item["schema"] == schema)
        return httpx.Response(
            200,
            json={"done": True, "done_reason": "stop", "message": {"content": json.dumps(result)}},
        )

    def outputs(self) -> list[dict]:
        """Construct an intentionally mixed score independent of the application or local model."""
        return [
            {
                "schema": "StatementGeneratorOutput",
                "output": {"statements": ["First fact.", "Second fact."]},
            },
            {
                "schema": "NLIStatementOutput",
                "output": {
                    "statements": [
                        {"statement": "First fact.", "verdict": 1, "reason": "Source supports it."},
                        {
                            "statement": "Second fact.",
                            "verdict": 0,
                            "reason": "Source contradicts it.",
                        },
                    ]
                },
            },
        ]

    def test_malformed_judgments_cannot_pass(self) -> None:
        """Missing, extra, rewritten and nonbinary verdicts cannot inflate the retained score."""
        for mode in ("missing", "extra", "rewritten", "nonbinary", "empty", "nan", "score"):
            outputs = copy.deepcopy(self.outputs())
            verdicts = outputs[1]["output"]["statements"]
            score = 0.5
            if mode == "missing":
                verdicts.pop()
            elif mode == "extra":
                verdicts.append(verdicts[0])
            elif mode == "rewritten":
                verdicts[0]["statement"] = "Different claim."
            elif mode == "nonbinary":
                verdicts[0]["verdict"] = 2
            elif mode == "empty":
                outputs[0]["output"]["statements"] = []
            elif mode == "nan":
                score = float("nan")
            else:
                score = 1.0
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_score(score, outputs)
        with self.assertRaisesRegex(ValueError, "both RAGAS"):
            validate_score(float("nan"), self.outputs()[:1])

    def test_duplicate_input_ids_fail(self) -> None:
        """Repeated cases cannot appear to enlarge calibration evidence."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text('{"id":"same"}\n{"id":"same"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "uniquely"):
                read_cases(path)

    def test_privacy_and_socket_boundary_before_sdk(self) -> None:
        """Check Windows loop startup and privacy before the fresh process's first SDK import."""
        script = """
import argparse, asyncio, builtins, os, socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from run_ragas import run
original = builtins.__import__
class Verified(Exception): pass
def checked(name, *args, **kwargs):
    if name == "ragas.llms.base":
        assert os.environ["RAGAS_DO_NOT_TRACK"] == "true"
        assert os.environ["LANGSMITH_TRACING"] == "false"
        assert os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] == "YES"
        with socket.socket() as connection:
            try: connection.connect(("127.0.0.1", 9999))
            except PermissionError: pass
            else: raise AssertionError("alternate endpoint allowed")
        raise Verified()
    return original(name, *args, **kwargs)
builtins.__import__ = checked
loop = asyncio.new_event_loop()
try:
    args = argparse.Namespace(
        output=Path(sys.argv[2]), cases=Path(sys.argv[1]) / "controls.jsonl"
    )
    run(args, loop)
except Verified:
    print("privacy-and-boundary-before-sdk")
else:
    raise AssertionError("SDK import not reached")
finally:
    loop.close()
"""
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment.pop("RAGAS_DO_NOT_TRACK", None)
            environment["LANGSMITH_TRACING"] = "true"
            result = subprocess.run(  # noqa: S603 - literal test script, no shell
                [sys.executable, "-c", script, str(Path(__file__).parent), directory],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("privacy-and-boundary-before-sdk", result.stdout)


if __name__ == "__main__":
    unittest.main()
