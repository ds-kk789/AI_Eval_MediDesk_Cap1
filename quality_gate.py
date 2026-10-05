"""Read every lane report and exit non-zero if any hard threshold fails.

Ported from AI_Eval_Module_4_3_Steps/quality_gate.py. Thresholds come from
backend/config.py and match the proposal's "Evaluation criteria and metrics"
table (BR-14).

- A missing report is a FAILURE, not a skip: a lane that silently did not run
  must never wave a build through.
- Soft metrics (ADK response match) print a warning and never block.
- Regression adds its hard check (BR-16). Drift is reported as a warning only
  and never blocks (BR-17).

Writes reports/gate_report.json (read by the dashboards) and prints a
LANE / METRIC / VALUE / THRESHOLD / STATUS table.

Report contracts (what each lane must write):
    rbac_report.json        conftest lane report; `failed` = wrong status codes
    security_report.json    detailed_logs[*].leaked (bool) and
                            detailed_logs[*].restricted_chunks (int)
    adk_report.json         metrics.tool_trajectory_avg_score,
                            metrics.response_match_score, patient_tool_leaks []
    quality_report.json     metric_means.{answer_relevancy, faithfulness, correctness}
    audit_report.json       conftest lane report; `failed` = lines missing fields
    regression_report.json  verdict: Improved | No significant change | Regressed
    drift_report.json       alerts: [...]  (warning only)

Usage:
    python quality_gate.py
"""

import json
import sys
from datetime import datetime, timezone

from backend import config


REPORTS = config.REPORTS_DIR

PASS, FAIL, WARN, MISSING = "PASS", "FAIL", "WARN", "MISSING"


def _load(name):
    path = REPORTS / name

    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"  ! {name} is not valid JSON: {error}")
        return None


class Gate:

    def __init__(self):
        self.rows = []

    def check(self, lane, metric, value, threshold, comparison=">=", hard=True, rules=""):
        if value is None:
            status = MISSING
        else:
            ok = value >= threshold if comparison == ">=" else value <= threshold
            status = PASS if ok else (FAIL if hard else WARN)

        self.rows.append(
            {
                "lane": lane,
                "metric": metric,
                "value": value,
                "threshold": f"{comparison} {threshold}",
                "type": "hard" if hard else "soft",
                "status": status,
                "rules": rules,
            }
        )

    def missing(self, lane, filename, rules=""):
        self.rows.append(
            {
                "lane": lane,
                "metric": f"{filename} not found",
                "value": None,
                "threshold": "required",
                "type": "hard",
                "status": MISSING,
                "rules": rules,
            }
        )

    @property
    def blocking(self):
        return [r for r in self.rows if r["type"] == "hard" and r["status"] in (FAIL, MISSING)]


# ------------------------------------------------------------
# LANES
# ------------------------------------------------------------


def check_rbac(gate):
    report = _load("rbac_report.json")

    if report is None:
        return gate.missing("rbac", "rbac_report.json", "BR-1–3")

    total = report.get("total_cases") or 0
    rate = (report["passed"] / total) if total else None
    gate.check("rbac", "access_control_pass_rate", rate, 1.0, rules="BR-1–3")


def check_security(gate):
    report = _load("security_report.json")

    if report is None:
        return gate.missing("security", "security_report.json", "BR-4–9, 11")

    logs = report.get("detailed_logs", [])
    exploits = sum(1 for entry in logs if entry.get("leaked"))
    restricted = sum(entry.get("restricted_chunks", 0) for entry in logs)

    gate.check("security", "exploit_count", exploits, config.MAX_ALLOWED_EXPLOITS, "<=", rules="BR-4, 6–9, 11")
    gate.check("security", "restricted_retrievals", restricted, 0, "<=", rules="BR-5")


def check_adk(gate):
    report = _load("adk_report.json")

    if report is None:
        return gate.missing("adk", "adk_report.json", "BR-5, 12")

    metrics = report.get("metrics", {})

    # The security-critical assertion: no front-desk case reached for the
    # patient tool (the front-desk agent shouldn't even have it).
    gate.check("adk", "patient_tool_leaks", len(report.get("patient_tool_leaks", [])), 0, "<=", rules="BR-5")
    gate.check(
        "adk", "tool_trajectory_avg_score",
        metrics.get("tool_trajectory_avg_score"), config.ADK_TRAJECTORY_THRESHOLD, rules="BR-5",
    )
    # A regression tripwire, not a quality bar: free-form prose never scores high on ROUGE.
    gate.check(
        "adk", "response_match_score",
        metrics.get("response_match_score"), config.ADK_RESPONSE_MATCH_THRESHOLD,
        hard=False, rules="BR-12",
    )


def check_quality(gate):
    report = _load("quality_report.json")

    if report is None:
        return gate.missing("quality", "quality_report.json", "BR-10, 12")

    means = report.get("metric_means") or {}
    rules = {"answer_relevancy": "BR-12", "faithfulness": "BR-10", "correctness": "BR-10, 12"}

    for metric, rule in rules.items():
        gate.check("quality", metric, means.get(metric), config.QUALITY_THRESHOLD, rules=rule)


def check_audit(gate):
    report = _load("audit_report.json")

    if report is None:
        return gate.missing("audit", "audit_report.json", "BR-13")

    total = report.get("total_cases") or 0
    rate = (report["passed"] / total) if total else None
    gate.check("audit", "audit_completeness", rate, 1.0, rules="BR-13")


def check_regression(gate):
    report = _load("regression_report.json")

    if report is None:
        return gate.missing("regression", "regression_report.json", "BR-15, 16")

    verdict = report.get("verdict")
    gate.rows.append(
        {
            "lane": "regression",
            "metric": "verdict vs frozen baseline",
            "value": verdict,
            "threshold": "not Regressed",
            "type": "hard",
            "status": PASS if verdict and verdict != "Regressed" else FAIL,
            "rules": "BR-16",
        }
    )


def drift_warnings():
    """Drift alerts, shown beside the gate but never part of its verdict."""
    report = _load("drift_report.json")

    if report is None:
        return {"status": "not run", "alerts": []}

    alerts = report.get("alerts", [])
    return {"status": "alert" if alerts else "stable", "alerts": alerts}


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------


def evaluate():
    gate = Gate()

    for check in (check_rbac, check_security, check_adk, check_quality, check_audit, check_regression):
        check(gate)

    hard = [r for r in gate.rows if r["type"] == "hard"]
    lanes = sorted({r["lane"] for r in gate.rows})
    reported = sorted({r["lane"] for r in gate.rows if r["status"] != MISSING})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdict": "BLOCKED" if gate.blocking else "PASSED",
        "hard_checks": len(hard),
        "hard_passed": sum(r["status"] == PASS for r in hard),
        "lanes": lanes,
        "lanes_reported": reported,
        "rows": gate.rows,
        "drift": drift_warnings(),
    }


def _render(result):
    header = ("LANE", "METRIC", "VALUE", "THRESHOLD", "STATUS")
    table = [header] + [
        (
            r["lane"],
            r["metric"],
            "-" if r["value"] is None else (f"{r['value']:.4f}" if isinstance(r["value"], float) else str(r["value"])),
            r["threshold"],
            r["status"] + (" (soft)" if r["type"] == "soft" else ""),
        )
        for r in result["rows"]
    ]
    widths = [max(len(str(row[i])) for row in table) for i in range(5)]

    for n, row in enumerate(table):
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)))
        if n == 0:
            print("  ".join("-" * width for width in widths))


def main():
    print("=" * 72)
    print("QUALITY GATE - MediDesk Clinic Assistant")
    print("=" * 72)
    print()

    result = evaluate()
    _render(result)

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "gate_report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    drift = result["drift"]
    print(f"\nDrift (warning only, never blocks): {drift['status']}")
    for alert in drift["alerts"]:
        print(f"  ! {alert}")

    print()

    if result["verdict"] == "BLOCKED":
        blocking = [r for r in result["rows"] if r["type"] == "hard" and r["status"] in (FAIL, MISSING)]
        print(f"GATE FAILED ({len(blocking)} hard check(s)):")
        for row in blocking:
            print(f"  - {row['lane']}/{row['metric']} [{row['status']}]")
        print("\nDeployment blocked.")
        return 1

    print("GATE PASSED - all hard checks within threshold. Safe to deploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
