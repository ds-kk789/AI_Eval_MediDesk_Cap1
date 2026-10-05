# Playbook

Planned run order once the stubs are implemented. LLM budget is about 30
calls a day, so every model answer and judge verdict is cached under
`reports/_cache/` and `reports/_judge_cache/`.

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

To check the backend without spending LLM budget (fake model, writes
`reports/backend_smoke_report.html`):

```bash
python smoke_backend.py
```

The test lanes start uvicorn themselves if nothing is
listening on 8003.

## 2. Run v1 and freeze the baseline

```bash
pytest tests/rbac tests/audit
pytest tests/security
pytest tests/quality
python run_adk_eval.py
python regression_check.py --promote
```

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

Opens as `reports/evaluation_report.html`.

## Budget order

1. RBAC, retrieval boundary, audit, input drift: no LLM calls.
2. v1 quality (cached), then v2 quality.
3. Security lane (sampled with `SECURITY_SAMPLE_PER_CATEGORY`).
4. Output and concept drift samples last. Drop the concept-drift labelled sample first if out of budget.
