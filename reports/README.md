# Evaluation report index

Authoritative release evidence:

- `ltr-ablation-test.json` — locked confidence-gated confirmation, 1,993 disjoint queries.
- `ltr-ablation-validation-gated.json` — validation result for the same final model/gate.
- `ltr-ablation-holdout-1-failed.json` — first holdout failure retained to disclose the full iteration history.
- `load-smoke.json` — compact Docker API concurrency smoke.
- `graph-coverage.json` — aggregate-only post-hoc coverage/subgroup diagnostic; never a replacement for the locked overall result.
- `verify-release.json` — machine audit of contracts, cutoff, model, reports, hashes, and remaining external warnings.
- `business-impact.json` — bounded scale translation from locked Hit@1 lift; explicitly not a causal conversion or revenue estimate.
- `sam-local-smoke.json` — exact Lambda ZIP invoked through SAM's Python 3.13 arm64 runtime image.
- `submission-audit.json` — binding-deliverable completion audit; intentionally false until external and Bedrock execution blockers are resolved.

Development evidence, not release claims:

- `ltr-ablation-validation.json` — earlier ungated validation experiment.
- `ltr-ablation-dev-test.json` — repeatedly inspected development-test experiment.
- `ablation-validation.json` / `ablation.json` — legacy deterministic heuristic benchmark.

The release claim must always be read from `ltr-ablation-test.json`. The
aspirational 5% NDCG lift gate is false even though its paired confidence
interval excludes zero.
