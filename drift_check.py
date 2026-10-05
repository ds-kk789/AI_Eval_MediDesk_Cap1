"""Compare drift windows with PSI, KL and JS divergence.

Port from: new

TODO:
- Input drift: topic mix (nearest policy topic by MiniLM embedding), query length
- Output drift: top-1 retrieval similarity, chunks returned, refusal rate, answer length
- Concept drift: share of queries with top-1 similarity < 0.4; correctness on 10 labelled queries
- Smooth empty buckets with epsilon 1e-6; JS uses log base 2
- Write reports/drift_report.json; alert only, never fails the gate
"""
