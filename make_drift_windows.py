"""Build the reference, control and current drift windows from templates.

Port from: new

TODO:
- Templates in data/drift/templates.json, seeded from golden_dataset.json topics
- reference: 100 queries, mix 40% hours / 30% insurance / 20% cancellation / 10% vaccines
- control: another 100 from the same mix (different seed offset)
- current: 100 with injected drift (dengue, telehealth, longer queries, more clinician lookups)
- Fixed seed config.DRIFT_SEED; no LLM calls
"""
