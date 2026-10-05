# Playbook

Run order for the full end-to-end evaluation. Every model answer and judge
verdict is cached under `reports/_cache/` and `reports/_judge_cache/`, so a
re-run only pays for what changed (delete a cache file to force a fresh
call). Groq's free tier allows about 200k tokens per key per day; when
`GROQ_API_KEY` runs out, every call site switches to `GROQ_API_KEY_2`.

Sampling: by default the model lanes run a sample (`SECURITY_SAMPLE_PER_CATEGORY=2`,
`QUALITY_SAMPLE_SIZE=3`, `ADK_SAMPLE_SIZE=2`). Set them to `0` for every case.
Calls are spaced `LLM_PACE_SECONDS=30` apart to stay under Groq's per-minute
limit.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill `GROQ_API_KEY` (and `GROQ_API_KEY_2` for failover) in `.env`.

## 1. Build the index and start the API

```bash
python -m backend.rag_pipeline
uvicorn backend.main:app --port 8003
```

The index also rebuilds itself on startup whenever `knowledge.txt` or the
chunk settings change.

To check the backend without spending LLM budget (fake model; results show
in the dashboard's Backend smoke tab):

```bash
python smoke_backend.py
```

The test lanes start uvicorn themselves if nothing is
listening on 8003.

## 2. Run v1 and freeze the baseline

| Step | Command | Model calls (full set) |
|---|---|---|
| RBAC + audit | `pytest tests/rbac tests/audit` | 3 (the 200 cells) |
| Security | `pytest tests/security` | ~15–30 |
| Quality | `pytest tests/quality` | 8 answers + ~70 judge |
| Agent | `python run_adk_eval.py` | ~7–14 |
| Freeze | `python regression_check.py --promote` | 0 |
| Compare | `python regression_check.py` | 0 |

`--promote` refuses while another hard gate check fails (only an approved
run may become the baseline); `--force` overrides on purpose.

## 3. Make the v2 change and check for regressions

In `backend/config.py` set `CHUNK_SIZE = 300` and `TOP_K = 2` (the index
rebuilds on next start), rerun the lanes, then:

```bash
python regression_check.py
python quality_gate.py
```

## 4. Drift

```bash
python make_drift_windows.py
python drift_check.py
```

## 5. Report

```bash
python build_report.py
```

Writes `reports/evaluation_report.html`, the evaluation dashboard: release
verdict, a severity-ranked "Needs attention" list, a tab per lane with charts
and a case drawer, and business-rule coverage. It reads the same reports as
`quality_gate.py`, so the two never disagree. `smoke_backend.py` rebuilds it
too. The "Sample preview" toggle shows `reports/sample/` (hand-made, labelled
on every view) so the layout can be judged before the lanes run.

## Budget order

1. RBAC, retrieval boundary, audit, drift: no or almost no LLM calls. Drift
   runs fully locally; the answer-based drift signals (refusal rate, answer
   length, labelled correctness) are listed as skipped in its report.
2. Security (all 15 attacks; it's the critical lane).
3. v1 quality (cached), then the agent lane.
4. The v2 rerun for the regression demo costs the same again.
