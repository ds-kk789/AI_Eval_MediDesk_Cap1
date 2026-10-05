"""Read every lane report and exit non-zero if any hard threshold fails.

Port from: AI_Eval_Module_4_3_Steps/quality_gate.py

TODO:
- Lanes: rbac, security, adk, quality, audit
- Add regression_report.json hard checks (BR-16)
- Print drift_report.json as a warning only; drift never blocks (BR-17)
"""
