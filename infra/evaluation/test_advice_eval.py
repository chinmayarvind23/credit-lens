"""Exercise real GEval wiring with declared protocol doubles, not model-quality claims."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Privacy initialization must precede the SDK import.
# ruff: noqa: I001
from advice_judge import AdviceJudge
from advice_rubric import STEPS, validate_advice_score
from local_judge import LocalJudge

from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, SingleTurnParams


class AdviceEvalTests(unittest.TestCase):
    """Pin the library's single-call strict-mode contract and reject lossy score conversions."""

    def test_fixed_steps_use_one_typed_call(self) -> None:
        """Explicit steps must bypass generated criteria and preserve the raw typed result."""
        with tempfile.TemporaryDirectory() as directory:
            judge = AdviceJudge("fixture:local", "0" * 64, Path(directory) / "calls.jsonl")

            def reply(prompt: str, schema: type):
                """Check actual library prompt/context wiring with a protocol-only response."""
                self.assertEqual(schema.__name__, "ReasonScore")
                self.assertIn("The signed schedule is missing.", prompt)
                self.assertIn("Request the signed schedule.", prompt)
                return schema.model_validate({"score": 1, "reason": "Requests the missing source."})

            try:
                with patch.object(LocalJudge, "generate", side_effect=reply) as generate:
                    metric = GEval(
                        name="Advice",
                        model=judge,
                        evaluation_steps=list(STEPS),
                        evaluation_params=[
                            SingleTurnParams.INPUT,
                            SingleTurnParams.ACTUAL_OUTPUT,
                            SingleTurnParams.EXPECTED_OUTPUT,
                            SingleTurnParams.CONTEXT,
                        ],
                        strict_mode=True,
                        async_mode=False,
                    )
                    score = metric.measure(
                        LLMTestCase(
                            input="Field: recommended_next_actions. What is missing?",
                            actual_output="Request the signed schedule.",
                            expected_output="Request the missing signed source before review.",
                            context=["The signed schedule is missing."],
                        ),
                        _show_indicator=False,
                    )
                    self.assertEqual(generate.call_count, 1)
                    validate_advice_score(score, metric.reason, judge.outputs)
                    self.assertEqual(score, 1)
            finally:
                judge.close()

    def test_fractional_boolean_and_mismatched_outputs_fail(self) -> None:
        """GEval integer conversion cannot make a fractional score valid or conceal a mismatch."""
        for original, reported in ((0.9, 0), (True, 1), (2, 2), (1, 0), (float("nan"), 0)):
            outputs = [{"schema": "ReasonScore", "output": {"score": original, "reason": "Reason"}}]
            with self.subTest(original=original), self.assertRaises(ValueError):
                validate_advice_score(reported, "Reason", outputs)
        with self.assertRaises(ValueError):
            validate_advice_score(1, "Reason", [])


if __name__ == "__main__":
    unittest.main()
