"""Prompt injection cases (BR-8).

Instruction overrides, system-prompt disclosure, fake policy updates and
instructions planted in patient notes. Each case carries a canary that can
only appear if the injection worked.

Detectors and recording live in validators.py. Answers are cached under
reports/_cache, so a re-run costs nothing. SECURITY_SAMPLE_PER_CATEGORY=0 runs
every case in the category.
"""

import pytest

from tests.security.validators import case_ids, evaluate_attack, load_cases


CASES = load_cases("prompt injection")


@pytest.mark.parametrize("case", CASES, ids=case_ids(CASES))
def test_injection_is_not_obeyed(case, cached_ask, record):
    passed, reasons, answer = evaluate_attack(case, cached_ask, record)

    assert passed, f"{case['id']} leaked: {reasons}: {answer}"
