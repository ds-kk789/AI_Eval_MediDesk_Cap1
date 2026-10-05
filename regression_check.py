"""Compare v2 lane reports against the frozen v1 baseline.

Port from: new

TODO:
- Load reports/baseline/*.json and reports/*.json
- Per metric: delta = mean_v2 - mean_v1; quality tolerance -0.02, others 0
- Per case: regressed if pass -> fail or quality score drops > 0.05
- Regression rate = regressed / total; quality <= 10%, others 0%
- Write reports/regression_report.json; --promote copies current reports into baseline (BR-15)
"""
