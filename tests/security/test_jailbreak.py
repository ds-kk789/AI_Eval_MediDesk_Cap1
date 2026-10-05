"""Impersonation and jailbreak cases (BR-4, BR-9).

Claimed identities, delegated authority, role-play and existence probes. The
role comes only from the API key, so none of these may unlock patient data,
and a refusal must not confirm that a patient exists.

Detectors and recording live in validators.py. Answers are cached under
reports/_cache, so a re-run costs nothing. SECURITY_SAMPLE_PER_CATEGORY=0 runs
every case in the category.
"""

import pytest

from tests.security.validators import case_ids, evaluate_attack, load_cases


CASES = load_cases("impersonation")


@pytest.mark.parametrize("case", CASES, ids=case_ids(CASES))
def test_impersonation_is_refused(case, cached_ask, record):
    passed, reasons, answer = evaluate_attack(case, cached_ask, record)

    assert passed, f"{case['id']} leaked: {reasons}: {answer}"
