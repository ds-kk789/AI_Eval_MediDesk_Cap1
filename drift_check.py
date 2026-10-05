"""Compare drift windows with PSI, KL and JS divergence (BR-17).

Reads data/drift/{reference,control,current}.jsonl (make_drift_windows.py)
and writes reports/drift_report.json. Alerts only: drift never blocks a
release, because the system didn't change, the world did.

Every signal here is computed locally (MiniLM embeddings + the FAISS index),
so the check costs no API calls:

- Input drift   topic mix (each query's topic = the section of its nearest
                chunk, or "Not covered" when top-1 similarity < 0.40), query
                length, clinician share. PSI and JS.
- Output drift  retrieval-side outputs: top-1 retrieval similarity and how often
                the role filter had to drop the nearest chunk. PSI.
- Concept drift out-of-coverage rate (top-1 similarity < 0.40), alert on a
                rise of 10 points or more.

Signals that need generated answers (refusal rate, answer length, correctness
on a labelled current sample) are listed under `skipped`: they would cost
about 20+ model calls per window.

PSI = sum((q - p) ln(q / p)), KL(P||Q) = sum(p ln(p / q)), JS = average KL to
the midpoint, log base 2 (0..1). Empty buckets get epsilon 1e-6.

    python drift_check.py
"""

import json
import math
from datetime import datetime, timezone

import numpy as np

from backend import config
from backend.rag_pipeline import RAGPipeline


EPS = 1e-6
WINDOWS = ("reference", "control", "current")

TOPIC_OF_SECTION = {
    "OPENING HOURS": "Opening hours",
    "INSURANCE AND PANEL PLANS": "Insurance",
    "CANCELLATION AND NO-SHOW POLICY": "Cancellations",
    "VACCINATIONS": "Vaccinations",
}
TOPICS = ["Opening hours", "Insurance", "Cancellations", "Vaccinations", "Clinical lookups", "Other policy", "Not covered"]

LENGTH_BINS = [(0, 6, "≤6 words"), (7, 10, "7–10"), (11, 15, "11–15"), (16, 25, "16–25"), (26, 10_000, "26+")]
SIM_EDGES = [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 1.01]


# ------------------------------------------------------------
# DIVERGENCES
# ------------------------------------------------------------


def dist(counts):
    total = sum(counts) or 1
    return [max(c / total, EPS) for c in counts]


def psi(p, q):
    return sum((qi - pi) * math.log(qi / pi) for pi, qi in zip(p, q))


def kl(p, q):
    return sum(pi * math.log(pi / qi) for pi, qi in zip(p, q))


def js(p, q):
    m = [(a + b) / 2 for a, b in zip(p, q)]
    return (kl(p, m) + kl(q, m)) / 2 / math.log(2)


def psi_status(value):
    if value >= config.DRIFT_PSI_ALERT:
        return "alert"
    return "moderate" if value >= 0.1 else "stable"


# ------------------------------------------------------------
# FEATURES
# ------------------------------------------------------------


def load(name):
    path = config.DRIFT_DIR / f"{name}.jsonl"
    if not path.exists():
        raise SystemExit(f"{path} not found. Run: python make_drift_windows.py")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def features(rag, rows):
    vectors = np.array(rag.embedding_model.embed_documents([r["query"] for r in rows]), dtype="float32")
    scores, indices = rag.index.search(vectors, 1)

    out = []
    for row, vec, score, idx in zip(rows, vectors, scores[:, 0], indices[:, 0]):
        record = rag.records[idx]
        similarity = float(score)

        if similarity < config.DRIFT_COVERAGE_SIMILARITY:
            topic = "Not covered"
        elif record["visibility"] == config.CLINICAL:
            topic = "Clinical lookups"
        else:
            topic = TOPIC_OF_SECTION.get(record["section"], "Other policy")

        allowed = config.ROLE_VISIBILITY.get(row["role"], {config.PUBLIC})
        out.append({
            "query": row["query"],
            "role": row["role"],
            "topic": topic,
            "words": len(row["query"].split()),
            "similarity": similarity,
            "filtered_nearest": record["visibility"] not in allowed,
            "vector": vec,
        })
    return out


def histogram(values, edges):
    counts = [0] * (len(edges) - 1)
    for v in values:
        for i in range(len(edges) - 1):
            if edges[i] <= v < edges[i + 1]:
                counts[i] += 1
                break
    return counts


def length_counts(feats):
    return [sum(1 for f in feats if lo <= f["words"] <= hi) for lo, hi, _ in LENGTH_BINS]


def topic_counts(feats):
    return [sum(1 for f in feats if f["topic"] == t) for t in TOPICS]


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------


def main():
    rag = RAGPipeline()
    rag.load_index()

    feats = {w: features(rag, load(w)) for w in WINDOWS}
    ref = feats["reference"]

    def compare(name, method, counts_fn, kind, threshold=None):
        p = dist(counts_fn(ref))
        values = {w: dist(counts_fn(feats[w])) for w in ("control", "current")}
        fn = psi if method == "PSI" else js
        control, current = round(fn(p, values["control"]), 3), round(fn(p, values["current"]), 3)
        thr = threshold if threshold is not None else (config.DRIFT_PSI_ALERT if method == "PSI" else config.DRIFT_JS_ALERT)
        status = (lambda v: psi_status(v)) if method == "PSI" else (lambda v: "alert" if v >= thr else "stable")
        return {"type": kind, "feature": name, "method": method, "control": control, "current": current,
                "threshold": thr, "status": status(current), "control_status": status(control)}

    signals = [
        compare("Topic mix", "PSI", topic_counts, "input"),
        compare("Topic mix", "JS", topic_counts, "input"),
        compare("Query length", "PSI", length_counts, "input"),
        compare("Clinician share of queries", "PSI", lambda fs: [sum(f["role"] == "clinician" for f in fs), sum(f["role"] != "clinician" for f in fs)], "input"),
        compare("Top-1 retrieval similarity", "PSI", lambda fs: histogram([f["similarity"] for f in fs], SIM_EDGES), "output"),
        compare("Nearest chunk dropped by role filter", "PSI", lambda fs: [sum(f["filtered_nearest"] for f in fs), sum(not f["filtered_nearest"] for f in fs)], "output"),
    ]

    def coverage(fs):
        return round(sum(f["topic"] == "Not covered" for f in fs) / len(fs), 3)

    ref_cov = coverage(ref)
    for_status = lambda rate: "alert" if rate - ref_cov >= 0.10 else "stable"  # noqa: E731
    signals.append({
        "type": "concept", "feature": f"Out-of-coverage rate (top-1 < {config.DRIFT_COVERAGE_SIMILARITY:.2f})",
        "method": "Rate change", "reference": ref_cov, "control": coverage(feats["control"]),
        "current": coverage(feats["current"]), "threshold": 0.10,
        "status": for_status(coverage(feats["current"])), "control_status": for_status(coverage(feats["control"])),
    })

    alerts = [
        f"{s['type'].capitalize()} drift: {s['feature']} {s['method']} {s['current']} (alert at {s['threshold']})"
        for s in signals if s["status"] == "alert"
    ]

    mix = {w: [round(100 * c / len(feats[w])) for c in topic_counts(feats[w])] for w in WINDOWS}

    def centroid_distance(a, b):
        ca, cb = np.mean([f["vector"] for f in a], axis=0), np.mean([f["vector"] for f in b], axis=0)
        return round(float(1 - np.dot(ca, cb) / (np.linalg.norm(ca) * np.linalg.norm(cb))), 3)

    uncovered = [f["query"] for f in feats["current"] if f["topic"] == "Not covered"]
    changes = sorted(zip(TOPICS, mix["reference"], mix["current"]), key=lambda t: t[2] - t[1], reverse=True)
    grown = [f"{t} {r}% → {c}%" for t, r, c in changes if c - r >= 5]
    recommendation = (
        (f"{', '.join(grown)}. " if grown else "")
        + (f"{coverage(feats['current']):.0%} of current queries match nothing in knowledge.txt "
           f"(e.g. \"{uncovered[0]}\"): add those topics to the knowledge base, then rerun the regression check."
           if uncovered else "Review the current window's new queries and add representative ones to the golden set.")
    ) if alerts else "No alert: the current window matches the reference distribution."

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "windows": {w: len(feats[w]) for w in WINDOWS},
        "topic_mix": {"topics": TOPICS, "unit": "% of queries", **mix},
        "signals": signals,
        "alerts": alerts,
        "recommendation": recommendation,
        "kl_reference_current": round(kl(dist(topic_counts(ref)), dist(topic_counts(feats["current"]))), 3),
        "embedding_centroid_distance": {"control": centroid_distance(ref, feats["control"]), "current": centroid_distance(ref, feats["current"])},
        "uncovered_examples": uncovered[:8],
        "limitations": [
            "The coverage check uses nearest-chunk similarity, so it undercounts: an uncovered question that shares "
            "clinic words (e.g. a long telehealth question opening with 'first time at this clinic') can still score "
            f"above {config.DRIFT_COVERAGE_SIMILARITY:.2f}. Treat the out-of-coverage rate as a floor.",
        ],
        "skipped": [
            {"feature": "Refusal rate", "type": "output", "why": "needs generated answers (~20 model calls per window)"},
            {"feature": "Answer length", "type": "output", "why": "needs generated answers"},
            {"feature": "Correctness on 10 labelled current queries", "type": "concept", "why": "needs answers plus judge calls; dropped first under the API budget"},
        ],
    }

    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.REPORTS_DIR / "drift_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Drift (alert only, never blocks):")
    for s in signals:
        print(f"  {s['type']:8} {s['feature']:40} {s['method']:11} control {s['control']!s:>6} ({s['control_status']})  current {s['current']!s:>6} ({s['status']})")
    print(f"Topic mix % (reference / control / current): {list(zip(TOPICS, mix['reference'], mix['control'], mix['current']))}")
    print(f"{len(alerts)} alert(s). {recommendation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
