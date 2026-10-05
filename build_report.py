"""Render reports/evaluation_report.html: the evaluation dashboard.

One page for an evaluation engineer to answer "can this ship, and if not,
what do I fix first?" in a few clicks:

- Overview: release verdict, a severity-ranked "Needs attention" list, lane
  health, every gate check and business-rule coverage.
- One tab per lane (security, RBAC, agent, quality, regression, drift, audit,
  backend smoke) with its charts and a filterable case table; any case opens
  a detail drawer (prompt, answer, scores, judge reasons, tools, sources).

Data comes from reports/*.json via quality_gate.evaluate(), so the page and
the CI gate can never disagree. If reports/sample/ exists, its hand-made
reports are embedded too, behind a "Sample preview" toggle that is labelled
on every view, so the layout can be judged before the lanes have run.

    python build_report.py

The page is self-contained (inline CSS, SVG and script, no network).
"""

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import quality_gate
from backend import config


OUTPUT = config.REPORTS_DIR / "evaluation_report.html"
TEMPLATE = Path(__file__).with_name("build_report_template.html")
SAMPLE_DIR = config.REPORTS_DIR / "sample"

LANE_FILES = {
    "rbac": "rbac_report.json",
    "security": "security_report.json",
    "adk": "adk_report.json",
    "quality": "quality_report.json",
    "audit": "audit_report.json",
    "regression": "regression_report.json",
    "drift": "drift_report.json",
    "smoke": "backend_smoke_report.json",
}

LANE_COMMANDS = {
    "rbac": "pytest tests/rbac",
    "security": "pytest tests/security",
    "adk": "python run_adk_eval.py",
    "quality": "pytest tests/quality",
    "audit": "pytest tests/audit",
    "regression": "python regression_check.py",
    "drift": "python drift_check.py",
    "smoke": "python smoke_backend.py",
}

# Business rules (docs/MediDesk_Use_Case_Proposal.md): short name, enforcing lane.
RULES = {
    "BR-1": ("Two roles, scoped data", "RBAC"),
    "BR-2": ("Valid API key or 401", "RBAC"),
    "BR-3": ("Front desk on clinical route → 403", "RBAC"),
    "BR-4": ("Role only from the API key", "Security"),
    "BR-5": ("No clinical retrieval for front desk", "Retrieval, ADK"),
    "BR-6": ("No patient data to front desk", "Security"),
    "BR-7": ("One named patient per query", "Security"),
    "BR-8": ("Document instructions are data", "Security"),
    "BR-9": ("Refusals never confirm a patient", "Security"),
    "BR-10": ("Answer only from documents", "Quality"),
    "BR-11": ("No medical advice", "Security"),
    "BR-12": ("Complete, specific answers", "Quality"),
    "BR-13": ("Every request audited", "Audit"),
    "BR-14": ("Release needs every hard check", "Gate"),
    "BR-15": ("Baseline frozen and versioned", "Regression"),
    "BR-16": ("Block on any regression", "Regression"),
    "BR-17": ("Drift alerts, never blocks", "Drift"),
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "blocking": 2, "warning": 3}

ACTIONS = {
    ("security", "exploit_count"): "Open the leaked cases and see which validator fired, then fix the prompt or the retrieval filter and rerun pytest tests/security.",
    ("security", "restricted_retrievals"): "A clinical chunk reached a front-desk prompt: check the tool scope in medidesk_agent/agent.py and config.ROLE_VISIBILITY.",
    ("rbac", "access_control_pass_rate"): "A wrong status code is an access-control bug: check backend/auth.py and the route guards in backend/main.py.",
    ("adk", "patient_tool_leaks"): "The front-desk agent reached the patient tool: check build_agent() in medidesk_agent/agent.py.",
    ("adk", "tool_trajectory_avg_score"): "Compare expected and actual tools in the failing cases; fix the agent instruction or the tool docstrings.",
    ("adk", "response_match_score"): "Soft check only: compare the wording with the evalset reference. It never blocks a release.",
    ("quality", "answer_relevancy"): "Open the failing cases: the answer drifts off the question. Tighten the 'answer only what was asked' rule.",
    ("quality", "faithfulness"): "Open the failing cases and compare the answer with its retrieved context: a claim isn't supported (often missing context from a low TOP_K).",
    ("quality", "correctness"): "Compare the answer with the golden answer in the failing cases; check the retrieved context first.",
    ("audit", "audit_completeness"): "Check the audit middleware in backend/main.py for the missing fields.",
    ("regression", "verdict vs frozen baseline"): "Open the flipped cases, then fix or revert the v2 change and rerun the lanes and regression_check.py.",
}

TAB_FOR_LANE = {"adk": "agent", "smoke": "backend"}


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

def parse_rules(text):
    """'BR-4–9, 11' -> ['BR-4', ..., 'BR-9', 'BR-11']."""
    ids = []
    for part in re.split(r"[,;]\s*", text or ""):
        match = re.search(r"(\d+)\s*[–-]\s*(\d+)$", part.replace("BR-", ""))
        if match:
            ids += [f"BR-{n}" for n in range(int(match.group(1)), int(match.group(2)) + 1)]
        elif re.search(r"\d+", part):
            ids.append(f"BR-{re.search(r'(\d+)', part).group(1)}")
    return [i for i in dict.fromkeys(ids) if i in RULES]


def _load_dir(directory):
    reports = {}
    for lane, name in LANE_FILES.items():
        path = directory / name
        if path.exists():
            try:
                reports[lane] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                reports[lane] = None
        else:
            reports[lane] = None
    return reports


def _case_status(lane, case):
    if lane == "quality":
        return "pass" if case.get("passed") else "fail"
    if lane == "adk" and case.get("trajectory_score") == 1.0 and not case.get("passed"):
        return "warn"  # only the soft response match missed
    return "pass" if case.get("passed") else "fail"


def _cases(lane, report):
    """Per-case entries with a uniform status and parsed rule ids."""
    if not report:
        return []
    out = []
    for case in report.get("detailed_logs", []):
        case = dict(case)
        case.setdefault("id", case.get("check") or case.get("test"))
        case["status"] = _case_status(lane, case)
        case["rule_ids"] = parse_rules(case.get("rules") or case.get("rule"))
        out.append(case)
    return out


# ------------------------------------------------------------
# ISSUES (the "Needs attention" list)
# ------------------------------------------------------------

def _failing_ids(lane, metric, cases, threshold):
    if lane == "quality":
        return [c["id"] for c in cases if (c.get("scores") or {}).get(metric, 1) < threshold]
    if lane == "adk" and metric == "response_match_score":
        return [c["id"] for c in cases if c.get("response_match") is not None and c["response_match"] < threshold]
    if lane == "adk" and metric == "patient_tool_leaks":
        return [c["id"] for c in cases if c.get("patient_tool_called")]
    if lane == "security" and metric == "restricted_retrievals":
        return [c["id"] for c in cases if c.get("restricted_chunks")]
    return [c["id"] for c in cases if c["status"] == "fail"]


def build_issues(gate, reports, cases):
    issues = []

    for row in gate["rows"]:
        lane, metric, status = row["lane"], row["metric"], row["status"]
        if status == "PASS":
            continue

        tab = TAB_FOR_LANE.get(lane, lane)
        rules = parse_rules(row["rules"])

        if status == "MISSING":
            issues.append({
                "severity": "blocking", "lane": lane, "tab": tab,
                "title": f"{lane.upper() if lane in ('rbac', 'adk') else lane.capitalize()} lane has not run",
                "detail": "A missing report counts as a failure, so the release stays blocked.",
                "action": f"Run {LANE_COMMANDS[lane]}, then python quality_gate.py.",
                "rules": rules, "cases": [],
            })
            continue

        threshold_value = float(re.sub(r"[^\d.]", "", row["threshold"]) or 0) if row["threshold"][:1] in "<>" else None
        failing = _failing_ids(lane, metric, cases.get(lane, []), threshold_value if threshold_value is not None else 0)
        value = row["value"]
        shown = f"{value:.2f}" if isinstance(value, float) else str(value)

        if lane == "regression":
            flipped = (reports.get("regression") or {}).get("flipped_cases", [])
            hard_lanes = {f["lane"] for f in flipped if f["change"].startswith("pass → fail")}
            severity = "critical" if hard_lanes & {"security", "rbac", "adk"} else "high"
            failing = [f"{f['lane']}:{f['case_id']}" for f in flipped if not f["change"].startswith("improved")]
            title = "v2 regressed against the frozen v1 baseline"
            detail = f"{len(failing)} case(s) got worse" + (f", including a {', '.join(sorted(hard_lanes))} pass → fail" if hard_lanes else "") + "."
        else:
            severity = (
                "warning" if status == "WARN"
                else "critical" if lane in ("security", "rbac") or metric == "patient_tool_leaks"
                else "high"
            )
            label = metric.replace("_", " ")
            if metric == "exploit_count":
                title = f"{value} attack(s) leaked data"
            elif metric == "restricted_retrievals":
                title = f"{value} clinical chunk(s) reached a front-desk prompt"
            else:
                title = f"{label.capitalize()} {shown} misses {row['threshold']}"
            detail = f"{len(failing)} case(s) involved." if failing else ""

        issues.append({
            "severity": severity, "lane": lane, "tab": tab, "metric": metric,
            "title": title, "detail": detail,
            "value": value, "threshold": row["threshold"],
            "action": ACTIONS.get((lane, metric), "Open the failing cases."),
            "rules": rules, "cases": failing,
        })

    drift = reports.get("drift")
    if drift and drift.get("alerts"):
        issues.append({
            "severity": "warning", "lane": "drift", "tab": "drift",
            "title": f"Drift alert on {len(drift['alerts'])} signal(s)",
            "detail": "Alerts trigger a re-evaluation; they never block a release (BR-17).",
            "action": drift.get("recommendation") or "Review the current window, then rerun the regression check.",
            "rules": ["BR-17"], "cases": [],
        })

    smoke = reports.get("smoke")
    if smoke and smoke.get("failed"):
        issues.append({
            "severity": "high", "lane": "smoke", "tab": "backend",
            "title": f"{smoke['failed']} backend smoke check(s) failing",
            "detail": "A server-side control isn't behaving as designed.",
            "action": "Open the failing checks, then rerun python smoke_backend.py.",
            "rules": [], "cases": [c["check"] for c in smoke["detailed_logs"] if not c["passed"]],
        })

    issues.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], i["lane"]))
    return issues


# ------------------------------------------------------------
# RULE COVERAGE
# ------------------------------------------------------------

STATUS_RANK = {"fail": 0, "missing": 1, "warn": 2, "pass": 3}


def build_rules(gate, cases, reports):
    checks = {rid: [] for rid in RULES}

    for row in gate["rows"]:
        status = {"PASS": "pass", "FAIL": "fail", "WARN": "warn", "MISSING": "missing"}[row["status"]]
        for rid in parse_rules(row["rules"]):
            checks[rid].append({"lane": row["lane"], "tab": TAB_FOR_LANE.get(row["lane"], row["lane"]),
                                "id": row["metric"], "title": f"Gate: {row['lane']} {row['metric']}", "status": status})

    for lane, lane_cases in cases.items():
        for case in lane_cases:
            for rid in case["rule_ids"]:
                checks[rid].append({"lane": lane, "tab": TAB_FOR_LANE.get(lane, lane), "id": case["id"],
                                    "title": case.get("title") or case.get("check") or case.get("question") or case.get("prompt") or case["id"],
                                    "status": case["status"]})

    # Rules enforced by the gate and drift themselves.
    checks["BR-14"].append({"lane": "gate", "tab": "overview", "id": "verdict", "title": f"Release gate: {gate['verdict']}",
                            "status": "pass" if gate["verdict"] == "PASSED" else "fail"})
    drift = reports.get("drift")
    checks["BR-17"].append({"lane": "drift", "tab": "drift", "id": "drift", "title": "Drift check ran and alerted only",
                            "status": "pass" if drift else "missing"})

    out = {}
    for rid, (name, lane) in RULES.items():
        items = checks[rid]
        status = min((c["status"] for c in items), key=STATUS_RANK.get) if items else "missing"
        out[rid] = {"name": name, "lane": lane, "status": status, "checks": items}
    return out


# ------------------------------------------------------------
# DATASET
# ------------------------------------------------------------

def _git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=config.PROJECT_ROOT, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build_dataset(directory, meta):
    reports = _load_dir(directory)
    gate = quality_gate.evaluate(directory)
    cases = {lane: _cases(lane, reports[lane]) for lane in ("rbac", "security", "adk", "quality", "audit")}
    if reports["smoke"]:
        cases["smoke"] = [
            dict(c, id=c["check"], status="pass" if c["passed"] else "fail", rule_ids=parse_rules(c.get("rule")))
            for c in reports["smoke"]["detailed_logs"]
        ]

    return {
        "meta": meta,
        "gate": gate,
        "reports": reports,
        "cases": cases,
        "issues": build_issues(gate, reports, cases),
        "rules": build_rules(gate, cases, reports),
        "commands": LANE_COMMANDS,
        "thresholds": {
            "quality": config.QUALITY_THRESHOLD,
            "adk_response_match": config.ADK_RESPONSE_MATCH_THRESHOLD,
            "adk_trajectory": config.ADK_TRAJECTORY_THRESHOLD,
            "psi": config.DRIFT_PSI_ALERT,
            "js": config.DRIFT_JS_ALERT,
            "regression_delta": config.REGRESSION_QUALITY_DELTA,
            "regression_rate": config.REGRESSION_RATE_MAX,
        },
    }


def build():
    datasets = {
        "latest": build_dataset(config.REPORTS_DIR, {
            "source": "latest", "label": "Latest run",
            "generated_at": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
            "commit": _git_commit(), "model": config.GROQ_MODEL, "prompt_version": config.PROMPT_VERSION,
        }),
    }

    meta_path = SAMPLE_DIR / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        datasets["sample"] = build_dataset(SAMPLE_DIR, {
            "source": "sample", "label": "Sample preview",
            "generated_at": meta.get("generated_at", "")[:16].replace("T", " ") + " UTC",
            "commit": meta.get("commit"), "model": meta.get("model"),
            "prompt_version": meta.get("prompt_version"), "note": meta.get("note"),
        })

    payload = json.dumps({"datasets": datasets, "rules": RULES}, ensure_ascii=False).replace("</", "<\\/")
    page = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(page, encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    print(f"Wrote {build()}")
