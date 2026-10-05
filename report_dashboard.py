"""Render reports/backend_smoke_report.html as an analytics dashboard.

Input: the smoke report dict (smoke_backend.py) and the gate result
(quality_gate.evaluate()). Output: one self-contained HTML page. Charts are
inline SVG drawn here; the only script is the hover/focus tooltip, and every
value it shows is also printed on the page or in a "Show data" table.

Colours follow the dataviz reference palette: categorical slots 1-2 (blue,
orange; validated light and dark) for public vs clinical chunks, a single
blue for magnitude, and the reserved status colours, always with an icon and
a word, for pass / fail / missing.
"""

import html

# Business rules from docs/MediDesk_Use_Case_Proposal.md: short name and the
# lane that enforces each one in the eval harness.
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

STATUS = {
    "PASS": ("good", "✓", "Pass"),
    "FAIL": ("critical", "✕", "Fail"),
    "MISSING": ("critical", "!", "Not run"),
    "WARN": ("warning", "▲", "Warn"),
}

e = html.escape


def _pill(status):
    tone, icon, word = STATUS[status]
    return f'<span class="pill {tone}"><span aria-hidden="true">{icon}</span> {word}</span>'


def _tip(title, value):
    return f'tabindex="0" data-tip-title="{e(title)}" data-tip-value="{e(value)}"'


# ------------------------------------------------------------
# CHARTS (inline SVG)
# ------------------------------------------------------------

def _bar(x, y, width, height, cls, round_end=True):
    """A bar anchored at x with only its data end rounded (4px)."""
    if width <= 0:
        return ""
    if not round_end or width < 6:
        return f'<rect class="{cls}" x="{x:.1f}" y="{y}" width="{width:.1f}" height="{height}"/>'
    return (
        f'<rect class="{cls}" x="{x:.1f}" y="{y}" width="{width:.1f}" height="{height}" rx="4"/>'
        f'<rect class="{cls}" x="{x:.1f}" y="{y}" width="{min(4, width):.1f}" height="{height}"/>'
    )


def _hbars(rows, unit, label_w=118, width=520, bar_h=18, gap=14):
    """Single-series horizontal bars: rows = [(label, value)]."""
    top = 6
    max_v = max([v for _, v in rows] + [1])
    plot_w = width - label_w - 44
    height = top + len(rows) * (bar_h + gap)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{e(unit)}">']

    for i, (label, value) in enumerate(rows):
        y = top + i * (bar_h + gap)
        w = plot_w * value / max_v
        out.append(f'<text class="axis-label" x="{label_w - 10}" y="{y + bar_h - 5}" text-anchor="end">{e(label)}</text>')
        out.append(f'<g class="hit" {_tip(label, f"{value} {unit}")}>')
        out.append(f'<rect class="hit-area" x="{label_w}" y="{y - 4}" width="{plot_w + 40}" height="{bar_h + 8}"/>')
        out.append(_bar(label_w, y, w, bar_h, "s1"))
        out.append("</g>")
        out.append(f'<text class="value-label" x="{label_w + w + 6:.1f}" y="{y + bar_h - 5}">{value}</text>')

    out.append(f'<line class="baseline" x1="{label_w}" y1="0" x2="{label_w}" y2="{height}"/>')
    out.append("</svg>")
    return "".join(out)


def _stacked(rows, series, label_w=118, width=520, bar_h=22, gap=18):
    """rows = [(label, {series_key: value})]; series = [(key, name, css)]."""
    top = 6
    max_total = max([sum(v.values()) for _, v in rows] + [1])
    plot_w = width - label_w - 70
    height = top + len(rows) * (bar_h + gap)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Chunks retrieved by role and visibility">']

    for i, (label, values) in enumerate(rows):
        y = top + i * (bar_h + gap)
        x = label_w
        total = sum(values.values())
        nonzero = [k for k, _, _ in series if values.get(k)]
        out.append(f'<text class="axis-label" x="{label_w - 10}" y="{y + bar_h - 7}" text-anchor="end">{e(label)}</text>')

        for key, name, css in series:
            value = values.get(key, 0)
            w = plot_w * value / max_total
            last = key == nonzero[-1] if nonzero else False
            seg_w = w - (0 if last else 2)  # 2px surface gap between fills
            out.append(f'<g class="hit" {_tip(f"{label} · {name}", f"{value} chunks")}>')
            out.append(_bar(x, y, seg_w, bar_h, css, round_end=last))
            out.append("</g>")
            x += w

        clinical = values.get("clinical", 0)
        note = f"{total}  ({clinical} clinical)"
        out.append(f'<text class="value-label" x="{x + 6:.1f}" y="{y + bar_h - 7}">{e(note)}</text>')

    out.append(f'<line class="baseline" x1="{label_w}" y1="0" x2="{label_w}" y2="{height}"/>')
    out.append("</svg>")
    return "".join(out)


def _area_meters(areas):
    """Passed / total per test area, status-coloured with the count as text."""
    out = []
    for area, (passed, total) in areas.items():
        pct = 100 * passed / total if total else 0
        tone = "good" if passed == total else "critical"
        out.append(
            f'<div class="meter-row" {_tip(area, f"{passed} of {total} checks passed")}>'
            f'<span class="meter-label">{e(area)}</span>'
            f'<span class="meter"><span class="meter-fill {tone}" style="width:{pct:.0f}%"></span></span>'
            f'<span class="meter-value">{passed}/{total}</span></div>'
        )
    return "".join(out)


def _data_table(headers, rows):
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in row) + "</tr>" for row in rows)
    return f'<details class="data"><summary>Show data</summary><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></details>'


# ------------------------------------------------------------
# PAGE
# ------------------------------------------------------------

def render(smoke, gate):
    checks = smoke["detailed_logs"]
    traffic = smoke["traffic"]
    covered = set(smoke["rules_covered"])

    areas = {}
    for c in checks:
        passed, total = areas.get(c["area"], (0, 0))
        areas[c["area"]] = (passed + c["passed"], total + 1)

    front_desk_clinical = traffic["chunks_by_role"].get("front_desk", {}).get("clinical", 0)
    denied = traffic["by_decision"].get("Denied", 0)
    smoke_ok = smoke["passed"] == smoke["total_cases"]
    blocked = gate["verdict"] == "BLOCKED"
    missing = [r["lane"] for r in gate["rows"] if r["status"] == "MISSING"]

    # --- KPI row -----------------------------------------------------
    kpis = [
        ("Smoke checks passed", f'{smoke["passed"]}/{smoke["total_cases"]}',
         "all server-side controls held" if smoke_ok else f'{smoke["failed"]} failing', "good" if smoke_ok else "critical"),
        ("Clinical chunks served to front desk", str(front_desk_clinical),
         "target 0 (BR-5)", "good" if front_desk_clinical == 0 else "critical"),
        ("Business rules exercised", f"{len(covered)}/{len(RULES)}",
         "the rest are enforced by gate lanes", None),
        ("Release gate hard checks", f'{gate["hard_passed"]}/{gate["hard_checks"]}',
         f'{len(gate["lanes_reported"])} of {len(gate["lanes"])} lanes reported', "critical" if blocked else "good"),
        ("Requests in smoke run", str(traffic["requests"]),
         f"{denied} denied", None),
    ]
    kpi_html = "".join(
        f'<div class="tile"><div class="k">{e(k)}</div><div class="v">{e(v)}</div>'
        f'<div class="d">{"" if not tone else _status_icon(tone)}{e(d)}</div></div>'
        for k, v, d, tone in kpis
    )

    # --- Gate ----------------------------------------------------------
    if blocked:
        if len(missing) == len(gate["lanes"]):
            gate_line = (f"No eval lane has reported yet ({len(missing)} of {len(gate['lanes'])} missing). "
                         "A missing report counts as a failure, so release is blocked until the lanes run.")
        else:
            failing = [f'{r["lane"]}/{r["metric"]}' for r in gate["rows"] if r["type"] == "hard" and r["status"] in ("FAIL", "MISSING")]
            gate_line = f"{len(failing)} hard check(s) failing: " + ", ".join(failing) + "."
    else:
        gate_line = "Every hard threshold passed. Safe to deploy."

    lane_chips = "".join(
        f'<span class="lane-chip {"ok" if lane in gate["lanes_reported"] else "off"}">'
        f'<span aria-hidden="true">{"✓" if lane in gate["lanes_reported"] else "○"}</span> {e(lane)}</span>'
        for lane in gate["lanes"]
    )

    def _fmt(value):
        if value is None:
            return "—"
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    gate_rows = "".join(
        f'<tr><td class="strong">{e(r["lane"])}</td><td>{_pill(r["status"])}</td><td>{e(r["metric"])}</td>'
        f'<td class="num">{e(_fmt(r["value"]))}</td><td class="num">{e(r["threshold"])}</td>'
        f'<td>{e(r["type"])}</td><td class="muted">{e(r["rules"])}</td></tr>'
        for r in gate["rows"]
    )
    drift = gate["drift"]
    drift_line = {
        "not run": "Drift: not run yet. Drift raises alerts only and never blocks a release (BR-17).",
        "stable": "Drift: stable. Drift raises alerts only and never blocks a release (BR-17).",
    }.get(drift["status"], f"Drift: {len(drift['alerts'])} alert(s). Alerts trigger a re-evaluation, never a block (BR-17).")

    # --- Charts ---------------------------------------------------------
    role_names = {"front_desk": "Front desk", "clinician": "Clinician"}
    chunk_rows = [
        (role_names[role], traffic["chunks_by_role"].get(role, {"public": 0, "clinical": 0}))
        for role in ("front_desk", "clinician")
    ]
    chunks_svg = _stacked(chunk_rows, [("public", "Public policy", "s1"), ("clinical", "Clinical record", "s2")])
    chunks_table = _data_table(
        ["Role", "Public chunks", "Clinical chunks"],
        [(label, v.get("public", 0), v.get("clinical", 0)) for label, v in chunk_rows],
    )

    decision_order = ["Allowed", "Denied", "No match", "Referred", "Invalid", "Error"]
    decision_rows = [(d, traffic["by_decision"][d]) for d in decision_order if traffic["by_decision"].get(d)]
    decision_svg = _hbars(decision_rows, "requests")
    decision_table = _data_table(["Decision", "Requests"], decision_rows)

    # --- Rule coverage -------------------------------------------------
    rule_tiles = "".join(
        f'<div class="rule {"covered" if rid in covered else "lane"}" {_tip(rid + " · " + name, ("Exercised by the smoke run" if rid in covered else "Enforced by the " + lane + " lane"))}>'
        f'<span class="rid"><span aria-hidden="true">{"✓" if rid in covered else "○"}</span> {rid}</span>'
        f'<span class="rname">{e(name)}</span>'
        f'<span class="rlane">{"Smoke + " + e(lane) if rid in covered else e(lane)}</span></div>'
        for rid, (name, lane) in RULES.items()
    )

    # --- Check list ----------------------------------------------------
    check_rows = []
    previous = None
    for c in checks:
        area = c["area"] if c["area"] != previous else ""
        previous = c["area"]
        check_rows.append(
            f'<tr><td class="strong">{e(area)}</td><td>{e(c["check"])}</td><td class="muted">{e(c["rule"])}</td>'
            f'<td>{_pill("PASS" if c["passed"] else "FAIL")}</td><td class="ev">{e(c["evidence"])}</td></tr>'
        )

    verdict_tone = "critical" if blocked else "good"
    verdict_word = "Release blocked" if blocked else "Release gate passed"

    return (
        TEMPLATE
        .replace("{{when}}", e(smoke["generated_at"]))
        .replace("{{verdict_tone}}", verdict_tone)
        .replace("{{verdict_icon}}", "✕" if blocked else "✓")
        .replace("{{verdict_word}}", verdict_word)
        .replace("{{kpis}}", kpi_html)
        .replace("{{gate_line}}", e(gate_line))
        .replace("{{lane_chips}}", lane_chips)
        .replace("{{gate_rows}}", gate_rows)
        .replace("{{drift_line}}", e(drift_line))
        .replace("{{chunks_svg}}", chunks_svg)
        .replace("{{chunks_table}}", chunks_table)
        .replace("{{fd_clinical}}", str(front_desk_clinical))
        .replace("{{decision_svg}}", decision_svg)
        .replace("{{decision_table}}", decision_table)
        .replace("{{area_meters}}", _area_meters(areas))
        .replace("{{rule_tiles}}", rule_tiles)
        .replace("{{covered}}", str(len(covered)))
        .replace("{{check_rows}}", "".join(check_rows))
    )


def _status_icon(tone):
    icon = {"good": "✓", "critical": "✕", "warning": "▲"}[tone]
    return f'<span class="ico {tone}" aria-hidden="true">{icon}</span>'


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MediDesk Backend Dashboard</title>
<style>
:root {
  color-scheme: light;
  --bg:#f4f4f1; --surface:#fcfcfb; --raised:#ffffff; --line:#e4e3de; --grid:#ecebe7;
  --text:#0b0b0b; --text-2:#52514e; --muted:#7a7974;
  --s1:#2a78d6; --s2:#eb6834;
  --good:#0ca30c; --good-ink:#006300; --good-bg:#e5f4e5;
  --critical:#d03b3b; --critical-ink:#a32020; --critical-bg:#fbe7e7;
  --warning:#fab219; --warning-ink:#7a5200; --warning-bg:#fdf3dc;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --bg:#121211; --surface:#1a1a19; --raised:#212120; --line:#33332f; --grid:#2a2a27;
    --text:#ffffff; --text-2:#c3c2b7; --muted:#9a9990;
    --s1:#3987e5; --s2:#d95926;
    --good-ink:#5fd35f; --good-bg:#16301a;
    --critical-ink:#f08a8a; --critical-bg:#3a1b1b;
    --warning-ink:#fab219; --warning-bg:#3a2e12;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg:#121211; --surface:#1a1a19; --raised:#212120; --line:#33332f; --grid:#2a2a27;
  --text:#ffffff; --text-2:#c3c2b7; --muted:#9a9990;
  --s1:#3987e5; --s2:#d95926;
  --good-ink:#5fd35f; --good-bg:#16301a;
  --critical-ink:#f08a8a; --critical-bg:#3a1b1b;
  --warning-ink:#fab219; --warning-bg:#3a2e12;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1180px; margin:0 auto; padding:28px 16px 56px; display:flex; flex-direction:column; gap:20px; }
header { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap; }
h1 { font-size:22px; margin:0; letter-spacing:-.01em; }
h2 { font-size:15px; margin:0; }
.sub { color:var(--text-2); margin:4px 0 0; }
.panel { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:18px 20px; min-width:0; }
.panel-h { display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:12px; }
.panel-h p { margin:2px 0 0; color:var(--text-2); font-size:13px; }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; }
.tile { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
.tile .k { color:var(--text-2); font-size:12.5px; }
.tile .v { font-size:30px; font-weight:650; font-variant-numeric:tabular-nums; margin:4px 0 2px; letter-spacing:-.02em; }
.tile .d { color:var(--muted); font-size:12.5px; display:flex; align-items:center; gap:6px; }
.ico { font-weight:700; }
.ico.good { color:var(--good-ink); } .ico.critical { color:var(--critical-ink); } .ico.warning { color:var(--warning-ink); }
.verdict { display:flex; align-items:center; gap:10px; padding:10px 14px; border-radius:10px; font-weight:600; }
.verdict.good { background:var(--good-bg); color:var(--good-ink); }
.verdict.critical { background:var(--critical-bg); color:var(--critical-ink); }
.verdict small { display:block; font-weight:400; color:var(--text-2); }
.gate-line { margin:0 0 12px; color:var(--text-2); }
.lanes { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }
.lane-chip { border:1px solid var(--line); border-radius:99px; padding:2px 10px; font-size:12.5px; }
.lane-chip.ok { color:var(--good-ink); background:var(--good-bg); border-color:transparent; }
.lane-chip.off { color:var(--muted); border-style:dashed; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr)); gap:20px; }
.tablewrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-size:11.5px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); font-weight:600; }
tbody tr:last-child td { border-bottom:0; }
td.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
td.strong { font-weight:600; white-space:nowrap; }
td.muted { color:var(--muted); white-space:nowrap; }
td.ev { color:var(--text-2); font:12px/1.45 ui-monospace,Consolas,monospace; word-break:break-word; max-width:340px; }
.pill { display:inline-flex; gap:5px; align-items:center; padding:1px 9px; border-radius:99px; font-size:12px; font-weight:600; white-space:nowrap; }
.pill.good { color:var(--good-ink); background:var(--good-bg); }
.pill.critical { color:var(--critical-ink); background:var(--critical-bg); }
.pill.warning { color:var(--warning-ink); background:var(--warning-bg); }
.note { color:var(--muted); font-size:12.5px; margin:12px 0 0; }
svg { width:100%; height:auto; display:block; overflow:visible; }
svg .s1 { fill:var(--s1); } svg .s2 { fill:var(--s2); }
svg .axis-label { fill:var(--text-2); font-size:12.5px; }
svg .value-label { fill:var(--text); font-size:12.5px; font-variant-numeric:tabular-nums; }
svg .baseline { stroke:var(--line); stroke-width:1; }
svg .hit-area { fill:transparent; }
svg .hit { cursor:default; outline:none; }
svg .hit:hover rect:not(.hit-area), svg .hit:focus rect:not(.hit-area) { opacity:.82; }
.legend { display:flex; gap:14px; font-size:12.5px; color:var(--text-2); }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; vertical-align:-1px; }
.callout { margin:12px 0 0; padding:8px 12px; border-radius:8px; background:var(--good-bg); color:var(--good-ink); font-size:13px; font-weight:600; }
.meter-row { display:grid; grid-template-columns:120px 1fr 44px; align-items:center; gap:12px; padding:6px 0; outline:none; }
.meter-label { color:var(--text-2); font-size:13px; }
.meter { height:10px; background:var(--grid); border-radius:99px; overflow:hidden; }
.meter-fill { display:block; height:100%; border-radius:99px; }
.meter-fill.good { background:var(--good); } .meter-fill.critical { background:var(--critical); }
.meter-value { font-variant-numeric:tabular-nums; text-align:right; font-size:13px; }
.rules { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:8px; }
.rule { border-radius:10px; padding:9px 11px; display:flex; flex-direction:column; gap:2px; outline:none; border:1px solid var(--line); }
.rule.covered { background:var(--good-bg); border-color:transparent; }
.rule.lane { border-style:dashed; }
.rule .rid { font-weight:650; font-size:12.5px; }
.rule.covered .rid { color:var(--good-ink); }
.rule.lane .rid { color:var(--muted); }
.rule .rname { font-size:12.5px; }
.rule .rlane { font-size:11.5px; color:var(--muted); }
.rule:focus-visible, .meter-row:focus-visible, svg .hit:focus-visible { outline:2px solid var(--s1); outline-offset:2px; }
details.data { margin-top:10px; font-size:12.5px; color:var(--text-2); }
details.data summary { cursor:pointer; }
details.data table { margin-top:6px; }
#tip { position:fixed; pointer-events:none; z-index:10; background:var(--raised); border:1px solid var(--line); border-radius:8px; padding:7px 10px; box-shadow:0 6px 20px rgba(0,0,0,.14); font-size:12.5px; display:none; max-width:260px; }
#tip b { display:block; font-size:14px; font-variant-numeric:tabular-nums; }
#tip span { color:var(--text-2); }
@media (max-width:560px) { .tile .v { font-size:26px; } .meter-row { grid-template-columns:96px 1fr 40px; } }
</style></head>
<body>
<main>
  <header>
    <div>
      <h1>MediDesk · Backend &amp; release gate</h1>
      <p class="sub">Smoke run {{when}} · scripted fake model, no LLM calls</p>
    </div>
    <div class="verdict {{verdict_tone}}"><span aria-hidden="true">{{verdict_icon}}</span>
      <div>{{verdict_word}}<small>python quality_gate.py</small></div></div>
  </header>

  <section class="kpis" aria-label="Headline numbers">{{kpis}}</section>

  <section class="panel">
    <div class="panel-h"><div><h2>Release gate</h2><p>Every hard threshold must pass before a release (BR-14). A lane with no report counts as a failure.</p></div></div>
    <p class="gate-line">{{gate_line}}</p>
    <div class="lanes" aria-label="Lanes reported">{{lane_chips}}</div>
    <div class="tablewrap"><table>
      <thead><tr><th>Lane</th><th>Status</th><th>Metric</th><th>Value</th><th>Threshold</th><th>Type</th><th>Rules</th></tr></thead>
      <tbody>{{gate_rows}}</tbody></table></div>
    <p class="note">{{drift_line}}</p>
  </section>

  <div class="grid2">
    <section class="panel">
      <div class="panel-h"><div><h2>Chunks retrieved, by caller role</h2><p>Every chunk that reached the model during the smoke run, from the audit log.</p></div>
        <div class="legend"><span><i style="background:var(--s1)"></i>Public policy</span><span><i style="background:var(--s2)"></i>Clinical record</span></div></div>
      {{chunks_svg}}
      <p class="callout"><span aria-hidden="true">✓</span> Front desk received {{fd_clinical}} clinical chunks, including when the model searched for a patient.</p>
      {{chunks_table}}
    </section>
    <section class="panel">
      <div class="panel-h"><div><h2>Requests by decision</h2><p>All audited requests in the run, including 401, 403 and 422 rejections.</p></div></div>
      {{decision_svg}}
      {{decision_table}}
    </section>
  </div>

  <div class="grid2">
    <section class="panel">
      <div class="panel-h"><div><h2>Smoke checks by area</h2><p>Passed out of total, per part of the backend.</p></div></div>
      {{area_meters}}
    </section>
    <section class="panel">
      <div class="panel-h"><div><h2>Business-rule coverage</h2><p>{{covered}} of 17 rules exercised by this run; the rest are enforced by the eval lanes.</p></div>
        <div class="legend"><span><span class="ico good">✓</span> Smoke run</span><span>○ Lane only</span></div></div>
      <div class="rules">{{rule_tiles}}</div>
    </section>
  </div>

  <section class="panel">
    <div class="panel-h"><div><h2>All smoke checks</h2><p>The real API, ADK runners, tools, FAISS index and audit log run in process; only the model is scripted, including calls a misbehaving model might make.</p></div></div>
    <div class="tablewrap"><table>
      <thead><tr><th>Area</th><th>Check</th><th>Rule</th><th>Result</th><th>Evidence</th></tr></thead>
      <tbody>{{check_rows}}</tbody></table></div>
    <p class="note">This page is a developer check of the backend. The live model is graded by the security, ADK and quality lanes, which feed the release gate above.</p>
  </section>
</main>
<div id="tip" role="status" aria-live="polite"><b></b><span></span></div>
<script>
(function () {
  var tip = document.getElementById("tip");
  var value = tip.querySelector("b"), title = tip.querySelector("span");
  function show(el, x, y) {
    value.textContent = el.getAttribute("data-tip-value");
    title.textContent = el.getAttribute("data-tip-title");
    tip.style.display = "block";
    var w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(x + 14, window.innerWidth - w - 8) + "px";
    tip.style.top = Math.max(8, y - h - 12) + "px";
  }
  function hide() { tip.style.display = "none"; }
  document.querySelectorAll("[data-tip-title]").forEach(function (el) {
    el.addEventListener("pointermove", function (ev) { show(el, ev.clientX, ev.clientY); });
    el.addEventListener("pointerleave", hide);
    el.addEventListener("focus", function () { var r = el.getBoundingClientRect(); show(el, r.left + r.width / 2, r.top); });
    el.addEventListener("blur", hide);
  });
})();
</script>
</body></html>
"""
