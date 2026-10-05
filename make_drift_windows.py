"""Build the reference, control and current drift windows from templates.

Drift asks "did our USERS change?": the system stays fixed and the data
changes. The golden set is only 8 questions, too few for PSI (about 5-10 per
bucket are needed), so it seeds templates (data/drift/templates.json) that
generate 100-query windows with a fixed seed. No LLM calls.

- reference: a normal week. Front desk 80% (hours 40%, insurance 30%,
  cancellation 20%, vaccines 10% of front-desk traffic), clinician lookups 20%,
  10% long queries.
- control: another normal week from the same mix (different seed). PSI against
  reference must stay < 0.1, or the thresholds are too jumpy.
- current: the week under review, with injected drift: dengue and telehealth
  questions (neither is in knowledge.txt), more vaccine questions, more
  clinician lookups (25%), 40% long queries.

    python make_drift_windows.py
"""

import json
import random

from backend import config


TEMPLATES = config.DRIFT_DIR / "templates.json"

MIX = {
    "reference": ({"hours": 32, "insurance": 24, "cancellation": 16, "vaccines": 8, "clinical": 20}, 0.10, 0),
    "control": ({"hours": 32, "insurance": 24, "cancellation": 16, "vaccines": 8, "clinical": 20}, 0.10, 1),
    "current": ({"hours": 12, "insurance": 11, "cancellation": 7, "vaccines": 17, "clinical": 25, "dengue": 10, "telehealth": 18}, 0.40, 2),
}


def _fill(template, slots, rng):
    out = template
    for key, values in slots.items():
        while "{" + key + "}" in out:
            out = out.replace("{" + key + "}", rng.choice(values), 1)
    return out


def build(name):
    spec = json.loads(TEMPLATES.read_text(encoding="utf-8"))
    mix, long_share, seed_offset = MIX[name]
    rng = random.Random(config.DRIFT_SEED + seed_offset)

    topics = [topic for topic, count in mix.items() for _ in range(count)]
    rng.shuffle(topics)

    rows = []
    for n, topic in enumerate(topics, start=1):
        entry = spec["topics"][topic]
        query = _fill(rng.choice(entry["templates"]), spec["slots"], rng)
        is_long = rng.random() < long_share
        if is_long:
            query = rng.choice(spec["long_prefixes"]) + query[0].lower() + query[1:]
        rows.append({"id": f"{name[:3]}-{n:03d}", "query": query, "role": entry["role"], "template_topic": topic, "long": is_long})

    return rows


def main():
    config.DRIFT_DIR.mkdir(parents=True, exist_ok=True)
    for name in MIX:
        rows = build(name)
        path = config.DRIFT_DIR / f"{name}.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        print(f"{name:9} {len(rows)} queries -> {path.relative_to(config.PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
