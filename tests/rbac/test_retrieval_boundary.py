"""The retrieval boundary, asserted without any model calls (BR-1, BR-5, BR-7).

Ported from AI_Eval_Module_4_3_Steps/tests/rbac/test_retrieval_boundary.py.
It calls the RAG pipeline directly, so it costs nothing and covers EVERY
question in the repo (golden set + all 15 attacks), while the lanes that need
a model only run a sample. Whether restricted text can enter a prompt is a
property of retrieval; only whether the model then repeats it needs a model.
"""

import json

import pytest

from backend import config
from backend.rag_pipeline import RAGPipeline
from tests.security.validators import load_cases


LANE = "rbac"


@pytest.fixture(scope="module")
def rag():
    pipeline = RAGPipeline()
    pipeline.load_index()
    return pipeline


def _probes():
    golden = json.loads(config.GOLDEN_DATASET.read_text(encoding="utf-8"))
    return [g["question"] for g in golden] + [c["prompt"] for c in load_cases(sampled=False)]


PROBES = _probes()


def _record(record, n, key, title, probes, violations, rules):
    record(
        LANE,
        {
            "id": f"RBAC-R{n}",
            "kind": "retrieval",
            "key": key,
            "title": title,
            "probes": probes,
            "violations": len(violations),
            "violation_detail": violations[:10],
            "passed": not violations,
            "rules": rules,
        },
    )


def test_index_has_both_visibilities(rag):
    """Guard against a vacuous pass: if nothing is clinical, nothing can leak."""
    assert rag.chunks_with_visibility(config.PUBLIC)
    assert rag.chunks_with_visibility(config.CLINICAL)


def test_front_desk_never_retrieves_clinical(rag, record):
    violations = [
        {"question": q[:100], "chunk": r["id"]}
        for q in PROBES
        for r in rag.search(q, role=config.FRONT_DESK)
        if r["visibility"] != config.PUBLIC
    ]
    _record(record, 1, "front_desk", "Front-desk search never returns a clinical chunk", len(PROBES), violations, "BR-5")
    assert not violations, violations


def test_policy_scope_stays_public_for_clinician(rag, record):
    """The policy tool searches PUBLIC only, so it can't sweep records (BR-7)."""
    violations = [
        {"question": q[:100], "chunk": r["id"]}
        for q in PROBES
        for r in rag.search(q, role=config.CLINICIAN, scope={config.PUBLIC})
        if r["visibility"] != config.PUBLIC
    ]
    _record(record, 2, "clinician", "Clinician policy search stays PUBLIC (no record sweep)", len(PROBES), violations, "BR-7")
    assert not violations, violations


def test_clinician_lookup_reaches_each_record(rag, record):
    """The mirror image: clinicians must actually reach the records."""
    missed = [pid for pid in rag.patients if not rag.lookup_patient(pid, role=config.CLINICIAN)]
    _record(record, 3, "clinician", "Clinician lookup reaches every patient record", len(rag.patients), missed, "BR-1")
    assert not missed, missed


def test_front_desk_lookup_returns_nothing(rag, record):
    leaked = [pid for pid in rag.patients if rag.lookup_patient(pid, role=config.FRONT_DESK)]
    _record(record, 4, "front_desk", "Front-desk patient lookup returns nothing", len(rag.patients), leaked, "BR-5")
    assert not leaked, leaked


def test_unknown_role_fails_closed(rag, record):
    violations = [
        {"question": q[:100], "chunk": r["id"]}
        for q in PROBES
        for r in rag.search(q, role="not-a-real-role")
        if r["visibility"] != config.PUBLIC
    ]
    _record(record, 5, "unknown role", "An unknown role sees PUBLIC only", len(PROBES), violations, "BR-1")
    assert not violations, violations
