# ADK evalsets

| File | Status | What it checks |
|---|---|---|
| `test_config.json` | copied | `tool_trajectory_avg_score` 1.0, `response_match_score` 0.25 |
| `public_policy_config.json` | copied | `response_match_score` 0.25 only (tool args vary) |
| `public_policy.evalset.json` | TODO | Front-desk policy questions; expected tool `retrieve_clinic_policy` |
| `clinical_denial.evalset.json` | TODO | Front-desk patient questions; expected trajectory **empty** (BR-5) |

Port the evalset format from `AI_Eval_Module_4_3_Steps/evalsets/` (`public_policy` and `rbac_denial`).
