"""PII exfiltration cases (BR-6, BR-7).

Direct requests, translation and summary wrappers, ID enumeration and bulk
listing (refused even for clinicians). Both the answer and the retrieved
sources are checked.

Detectors and recording live in validators.py. Answers are cached under
reports/_cache, so a re-run costs nothing. SECURITY_SAMPLE_PER_CATEGORY=0 runs
every case in the category.
"""

import pytest

from tests.security.validators import case_ids, evaluate_attack, load_cases


CASES = load_cases("pii exfiltration")


@pytest.mark.parametrize("case", CASES, ids=case_ids(CASES))
def test_patient_data_is_not_exfiltrated(case, cached_ask, record):
    passed, reasons, answer = evaluate_attack(case, cached_ask, record)

    assert passed, f"{case['id']} leaked: {reasons}: {answer}"
