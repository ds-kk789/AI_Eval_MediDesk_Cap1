# Sample lane reports

Hand-made reports in the exact format each eval lane must write. They are
**not a real run**. They let `build_report.py` preview the evaluation
dashboard ("Sample preview" toggle) before the lanes exist, and they are the
spec the lanes are built against. `backend_smoke_report.json` is a copy of a
real `smoke_backend.py` run.

The sample tells one story so the triage view has something to rank: v2
(`CHUNK_SIZE 300`, `TOP_K 2`) leaks the system prompt on TC-PI-02, hallucinates
Saturday walk-ins on G-4, so it regresses against v1, while the current
drift window shows telehealth questions the knowledge base doesn't cover.

| File | Gate reads | Per-case fields (`detailed_logs[*]`) |
|---|---|---|
| `rbac_report.json` | `passed / total_cases` | `id, kind (status/retrieval), key, method, endpoint, expected_status, actual_status, probes, violations, passed, rules` |
| `security_report.json` | `leaked`, `restricted_chunks` | `id, category, role, prompt, canary, answer, decision, tools_called, leaked, leak_reasons, restricted_chunks, passed, rules` |
| `adk_report.json` | `metrics.*`, `patient_tool_leaks` | `id, eval_set_id, eval_id, question, expected_tools, actual_tools, trajectory_score, response_match, patient_tool_called, passed, rules` |
| `quality_report.json` | `metric_means.*` | `id, role, question, expected_answer, answer, scores{answer_relevancy, faithfulness, correctness}, reasons{...}, passed, rules` |
| `audit_report.json` | `passed / total_cases` | `id, title, passed, rules` |
| `regression_report.json` | `verdict` | top level: `change, baseline{}, candidate{}, metrics[], lanes[], flipped_cases[]` |
| `drift_report.json` | `alerts` (warning only) | top level: `windows, topic_mix, signals[], alerts[], recommendation` |

`rules` is a string such as `"BR-4, BR-9"` or `"BR-1–3"`; the dashboard maps
every case to its business rules from it.
