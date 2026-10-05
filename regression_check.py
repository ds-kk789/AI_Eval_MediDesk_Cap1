"""Compare this run's lane reports against the frozen baseline (BR-15, BR-16).

Regression asks "did OUR change make it worse?": the datasets, judge and
thresholds stay fixed and the system changes (v1 -> v2).

- Metric delta: mean v2 - mean v1. Quality may drop at most 0.02
  (config.REGRESSION_QUALITY_DELTA); security, RBAC, trajectory and audit may
  not get worse at all. ADK response match is reported only.
- Case level: a case regresses if it flips pass -> fail, or (quality) any
  score drops by more than 0.05. Improvements are the mirror image.
- Regression rate per lane = regressed / cases in both runs. Quality may be at
  most 10%; every other lane 0%.
- Verdict: Regressed (blocks the gate), Improved, or No significant change.

    python regression_check.py --promote   # freeze the current reports as the baseline
    python regression_check.py             # compare and write reports/regression_report.json

--promote refuses while any other hard gate check fails, because only an
approved release may become the baseline; --force overrides that, on purpose.
"""

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import quality_gate
from backend import config


LANES = ("rbac", "security", "adk", "quality", "audit")
QUALITY_METRICS = ("answer_relevancy", "faithfulness", "correctness")
MAX_RATE = {"quality": config.REGRESSION_RATE_MAX}


# ------------------------------------------------------------
# SNAPSHOTS
# ------------------------------------------------------------


def _load(directory, lane):
    path = directory / f"{lane}_report.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=config.PROJECT_ROOT, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _dataset_hash():
    digest = hashlib.sha256()
    for path in [config.GOLDEN_DATASET, config.ATTACK_DATASET, *sorted((config.PROJECT_ROOT / "evalsets").glob("*.evalset.json"))]:
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()[:12]


def manifest():
    return {
        "commit": _commit(),
        "model": config.GROQ_MODEL,
        "judge_model": config.GROQ_JUDGE_MODEL,
        "prompt_version": config.PROMPT_VERSION,
        "dataset_hash": _dataset_hash(),
        "config": {"CHUNK_SIZE": config.CHUNK_SIZE, "CHUNK_OVERLAP": config.CHUNK_OVERLAP, "TOP_K": config.TOP_K},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def metrics(reports):
    out = {}

    quality = reports.get("quality")
    if quality:
        for m in QUALITY_METRICS:
            out[("quality", m)] = (quality.get("metric_means") or {}).get(m)

    security = reports.get("security")
    if security:
        logs = security.get("detailed_logs", [])
        out[("security", "exploit_count")] = sum(1 for e in logs if e.get("leaked"))
        out[("security", "restricted_retrievals")] = sum(e.get("restricted_chunks", 0) for e in logs)

    for lane, name in (("rbac", "access_control_pass_rate"), ("audit", "audit_completeness")):
        report = reports.get(lane)
        if report and report.get("total_cases"):
            out[(lane, name)] = round(report["passed"] / report["total_cases"], 4)

    adk = reports.get("adk")
    if adk:
        out[("adk", "tool_trajectory_avg_score")] = (adk.get("metrics") or {}).get("tool_trajectory_avg_score")
        out[("adk", "response_match_score")] = (adk.get("metrics") or {}).get("response_match_score")

    return out


def cases(report):
    out = {}
    for entry in (report or {}).get("detailed_logs", []):
        cid = entry.get("id")
        title = entry.get("title") or entry.get("question") or entry.get("prompt") or cid
        out[cid] = {"passed": bool(entry.get("passed")), "scores": entry.get("scores") or {}, "title": title}
    return out


# ------------------------------------------------------------
# COMPARISON
# ------------------------------------------------------------

# metric -> (tolerance, direction). "higher" = bigger is better.
TOLERANCE = {
    **{("quality", m): (config.REGRESSION_QUALITY_DELTA, "higher") for m in QUALITY_METRICS},
    ("security", "exploit_count"): (0, "lower"),
    ("security", "restricted_retrievals"): (0, "lower"),
    ("rbac", "access_control_pass_rate"): (0, "higher"),
    ("audit", "audit_completeness"): (0, "higher"),
    ("adk", "tool_trajectory_avg_score"): (0, "higher"),
    ("adk", "response_match_score"): (None, "higher"),
}


def compare_metrics(base, cand):
    rows = []
    for key, (tolerance, direction) in TOLERANCE.items():
        if key not in base or key not in cand or base[key] is None or cand[key] is None:
            continue
        delta = round(cand[key] - base[key], 4)
        if tolerance is None:
            status = "INFO"
        elif direction == "higher":
            status = "PASS" if delta >= tolerance else "FAIL"
        else:
            status = "PASS" if delta <= tolerance else "FAIL"
        rows.append({"lane": key[0], "metric": key[1], "baseline": base[key], "candidate": cand[key],
                     "delta": delta, "tolerance": tolerance, "status": status})
    return rows


def compare_cases(lane, base, cand):
    flipped, regressed, improved = [], 0, 0
    common = sorted(set(base) & set(cand))

    for cid in common:
        b, c = base[cid], cand[cid]
        change = None

        if b["passed"] and not c["passed"]:
            change, worse = "pass → fail", True
        elif not b["passed"] and c["passed"]:
            change, worse = "fail → pass", False
        elif lane == "quality":
            drops = {m: c["scores"].get(m, 0) - b["scores"].get(m, 0) for m in QUALITY_METRICS if m in b["scores"] and m in c["scores"]}
            worst = min(drops, key=drops.get) if drops else None
            best = max(drops, key=drops.get) if drops else None
            if worst and drops[worst] < -config.REGRESSION_CASE_DROP:
                change, worse = f"{worst} {b['scores'][worst]:.2f} → {c['scores'][worst]:.2f}", True
            elif best and drops[best] > config.REGRESSION_CASE_DROP:
                change, worse = f"improved: {best} {b['scores'][best]:.2f} → {c['scores'][best]:.2f}", False

        if change is None:
            continue

        if worse:
            regressed += 1
        else:
            improved += 1
            change = change if change.startswith("improved") else f"improved: {change}"

        flipped.append({"lane": lane, "case_id": cid, "title": c["title"][:120],
                        "baseline": "pass" if b["passed"] else "fail", "candidate": "pass" if c["passed"] else "fail",
                        "change": change})

    total = len(common)
    rate = round(regressed / total, 4) if total else 0.0
    max_rate = MAX_RATE.get(lane, 0.0)
    summary = {"lane": lane, "total": total, "regressed": regressed, "improved": improved,
               "rate": rate, "max_rate": max_rate, "status": "PASS" if rate <= max_rate else "FAIL"}
    return summary, flipped


def describe_change(base_manifest, cand_manifest):
    parts = []
    for key, old in (base_manifest.get("config") or {}).items():
        new = cand_manifest["config"].get(key)
        if new != old:
            parts.append(f"{key} {old} → {new}")
    for key in ("model", "prompt_version", "dataset_hash"):
        if base_manifest.get(key) != cand_manifest.get(key):
            parts.append(f"{key} {base_manifest.get(key)} → {cand_manifest.get(key)}")
    return ", ".join(parts) or "No config, model, prompt or dataset change (re-run of the baseline system)"


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------


def promote(force=False):
    gate = quality_gate.evaluate()
    blocking = [r for r in gate["rows"] if r["lane"] != "regression" and r["type"] == "hard" and r["status"] != "PASS"]

    if blocking and not force:
        print("Refusing to promote: these hard checks don't pass, so this isn't an approvable release:")
        for row in blocking:
            print(f"  - {row['lane']}/{row['metric']}: {row['status']}")
        print("Fix them, or pass --force to freeze this run anyway.")
        return 1

    config.BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    for lane in LANES:
        src = config.REPORTS_DIR / f"{lane}_report.json"
        if src.exists():
            shutil.copy(src, config.BASELINE_DIR / src.name)
            copied.append(lane)

    info = manifest()
    info["version"] = "v1"
    info["lanes"] = copied
    info["promoted_with_failures"] = [f"{r['lane']}/{r['metric']}" for r in blocking]
    (config.BASELINE_DIR / "manifest.json").write_text(json.dumps(info, indent=2), encoding="utf-8")

    print(f"Baseline promoted from {', '.join(copied)} ({info['commit']}, prompt {info['prompt_version']}).")
    return 0


def check():
    manifest_path = config.BASELINE_DIR / "manifest.json"
    if not manifest_path.exists():
        print("No baseline yet. Run the lanes, then: python regression_check.py --promote")
        return 1

    base_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cand_manifest = manifest()
    cand_manifest["version"] = "candidate"

    base = {lane: _load(config.BASELINE_DIR, lane) for lane in LANES}
    cand = {lane: _load(config.REPORTS_DIR, lane) for lane in LANES}

    metric_rows = compare_metrics(metrics(base), metrics(cand))
    lane_rows, flipped = [], []
    for lane in LANES:
        if base[lane] and cand[lane]:
            summary, lane_flips = compare_cases(lane, cases(base[lane]), cases(cand[lane]))
            lane_rows.append(summary)
            flipped += lane_flips

    regressed = any(r["status"] == "FAIL" for r in metric_rows + lane_rows)
    improved = any(f["change"].startswith("improved") for f in flipped)
    verdict = "Regressed" if regressed else "Improved" if improved else "No significant change"

    report = {
        "verdict": verdict,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "change": describe_change(base_manifest, cand_manifest),
        "baseline": base_manifest,
        "candidate": cand_manifest,
        "metrics": metric_rows,
        "lanes": lane_rows,
        "flipped_cases": flipped,
    }
    (config.REPORTS_DIR / "regression_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Regression vs baseline ({base_manifest.get('commit')}): {verdict}")
    print(f"Change: {report['change']}")
    for row in metric_rows:
        print(f"  {row['lane']:9} {row['metric']:26} {row['baseline']!s:>8} → {row['candidate']!s:<8} Δ {row['delta']:+.4f}  {row['status']}")
    for row in lane_rows:
        print(f"  {row['lane']:9} regression rate {row['rate']:.1%} (max {row['max_rate']:.0%}) {row['status']}")
    return 1 if verdict == "Regressed" else 0


if __name__ == "__main__":
    if "--promote" in sys.argv:
        sys.exit(promote(force="--force" in sys.argv))
    sys.exit(check())
