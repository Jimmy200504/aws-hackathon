# SkillWeave technical steering

- Python 3.11+.
- Local demo must remain zero-dependency and runnable with `python3 -m app.server`.
- Production-only dependencies belong in `requirements-production.lock`.
- Rank output is deterministic for a given index/version.
- Graph feature changes require a validation ablation and a fresh holdout.
- Every artifact records dataset version, graph cutoff, index/model version, schema fingerprint, and seed.
- Tests must cover contract, leakage, alias boundaries, and rank invariants.
