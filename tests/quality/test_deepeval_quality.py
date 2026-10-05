"""Answer relevancy, faithfulness and correctness on golden_dataset.json (BR-10, BR-12).

Ported from AI_Eval_Module_4_3_Steps/tests/quality/test_deepeval_quality.py.
Each golden question goes through the live API as its own role (clinician
questions via /clinical/ask) and is scored by three DeepEval metrics, judged
by Groq (groq_judge.py):

- answer_relevancy: share of the answer's statements that address the question
- faithfulness:     share of the answer's claims supported by the retrieved context
- correctness:      G-Eval rubric comparing the answer with the golden answer

The hard >= 0.95 bar is applied to each metric's MEAN by quality_gate.py. The
per-case assertion here is only a collapse floor (0.5): judge scores move a
little between runs, and one judge opinion on one sentence shouldn't fail the
build on its own. Answers and verdicts are cached, so a re-run is free.
QUALITY_SAMPLE_SIZE=0 runs all 8 golden questions.
"""

import json

import pytest
from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from backend import config
from tests.quality.groq_judge import GroqJudge


LANE = "quality"

GOLDEN = config.sample(
    json.loads(config.GOLDEN_DATASET.read_text(encoding="utf-8")),
    config.QUALITY_SAMPLE_SIZE,
)

PER_CASE_FLOOR = 0.5

CORRECTNESS_STEPS = [
    "Identify every factual claim in 'actual output' about clinic hours, days, "
    "fees, notice periods, insurance plans, vaccinations, or a patient's "
    "medication, allergy or diagnosis.",
    "Compare each claim against 'expected output'. A claim CONTRADICTS if it "
    "states a different number, time, day, fee, plan name, drug, dose or "
    "allergy for the same thing.",
    "Heavily penalise contradictions. Each contradiction is a serious error.",
    "Do NOT penalise the actual output for being worded, ordered or formatted "
    "differently, or for adding correct detail the expected output omits.",
    "If the actual output omits a fact the question directly asked for, apply "
    "a moderate penalty.",
    "Score 1.0 when there are no contradictions and nothing the question "
    "asked for is missing.",
]


@pytest.fixture(scope="session")
def metrics():
    judge = GroqJudge()
    return {
        "answer_relevancy": AnswerRelevancyMetric(threshold=config.QUALITY_THRESHOLD, model=judge, async_mode=False),
        "faithfulness": FaithfulnessMetric(threshold=config.QUALITY_THRESHOLD, model=judge, async_mode=False),
        "correctness": GEval(
            name="Correctness",
            # Explicit steps: with free-form criteria the judge scored a
            # correct but more detailed answer 0.0 in the HR project.
            evaluation_steps=CORRECTNESS_STEPS,
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
                LLMTestCaseParams.EXPECTED_OUTPUT,
            ],
            threshold=config.QUALITY_THRESHOLD,
            model=judge,
            async_mode=False,
        ),
    }


@pytest.mark.parametrize("item", GOLDEN, ids=[f"G-{item['id']}" for item in GOLDEN])
def test_answer_quality(item, cached_ask, record, metrics):
    role = item["role"]
    path = "/clinical/ask" if role == config.CLINICIAN else "/ask"
    payload = cached_ask(f"QUAL-{item['id']:02d}", item["question"], role=role, path=path)

    test_case = LLMTestCase(
        input=item["question"],
        actual_output=payload["answer"],
        expected_output=item["expected_answer"],
        retrieval_context=payload["retrieved_context"] or [""],
    )

    scores, reasons = {}, {}

    for name, metric in metrics.items():
        metric.measure(test_case)
        scores[name] = round(metric.score, 4)
        reasons[name] = metric.reason

    record(
        LANE,
        {
            "id": f"G-{item['id']}",
            "role": role,
            "question": item["question"],
            "expected_answer": item["expected_answer"],
            "answer": payload["answer"],
            "decision": payload.get("decision"),
            "tools_called": payload.get("tools_called", []),
            "sources": [s["id"] for s in payload.get("sources", [])],
            "scores": scores,
            "reasons": reasons,
            "passed": all(v >= config.QUALITY_THRESHOLD for v in scores.values()),
            "rules": "BR-10, BR-12",
        },
    )

    lowest = min(scores.values())
    assert lowest >= PER_CASE_FLOOR, (
        f"G-{item['id']}: quality collapsed {scores}\nQ: {item['question']}\nA: {payload['answer']}"
    )
