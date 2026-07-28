# Evaluation report index

Authoritative release evidence:

- `ltr-ablation-test.json` — locked confidence-gated confirmation, 1,993 disjoint queries.
- `ltr-ablation-validation-gated.json` — validation result for the same final model/gate.
- `ltr-ablation-holdout-1-failed.json` — first holdout failure retained to disclose the full iteration history.
- `load-smoke.json` — compact Docker API concurrency smoke.

Development evidence, not release claims:

- `ltr-ablation-validation.json` — earlier ungated validation experiment.
- `ltr-ablation-dev-test.json` — repeatedly inspected development-test experiment.
- `ablation-validation.json` / `ablation.json` — legacy deterministic heuristic benchmark.

The release claim must always be read from `ltr-ablation-test.json`. The
aspirational 5% NDCG lift gate is false even though its paired confidence
interval excludes zero.
