# Evaluation report index

Authoritative release evidence:

- `ltr-quality-confirmation.json` — primary untouched confirmation: 1,991
  queries, NDCG@10 +5.72%, paired CI entirely positive.
- `ltr-quality-replication.json` — second untouched, disjoint confirmation:
  1,992 queries, NDCG@10 +5.07%, paired CI entirely positive.
- `ltr-quality-component-ablation.json` — same-model cumulative feature-family
  attribution on the locked primary confirmation.
- `ltr-quality-company-holdout.json` — rejected post-hoc company-only candidate
  on the second holdout; retained to disclose the complete iteration.
- `ltr-ablation-test.json` — historical RC6 confidence-gated confirmation,
  1,993 disjoint queries and +1.34%; superseded by the larger quality model.
- `ltr-ablation-validation-gated.json` — validation result for the same final model/gate.
- `ltr-ablation-holdout-1-failed.json` — first holdout failure retained to disclose the full iteration history.
- `load-smoke.json` — compact Docker API concurrency smoke.
- `graph-coverage.json` — aggregate-only post-hoc coverage/subgroup diagnostic; never a replacement for the locked overall result.
- `verify-release.json` — machine audit of contracts, cutoff, model, reports, hashes, and registered external deliverables.
- `business-impact.json` — bounded scale translation from locked Hit@1 lift; explicitly not a causal conversion or revenue estimate.
- `sam-local-smoke.json` — exact Lambda ZIP invoked through SAM's Python 3.13 arm64 runtime image.
- `portable-ltr-parity.json` — 40,218-row native XGBoost versus dependency-free
  Lambda tree-inference parity check.
- `bedrock-pilot.json` — aggregate-only real Bedrock structured-extraction
  evidence: 200 train-only records, validated publication/quarantine, tokens,
  and bounded cost.
- `aws-production-smoke.json` — public HTTPS UI/API/trace checks plus bounded AWS concurrency and latency evidence.
- `demo-video.json` — five-minute Full HD MP4, audio/subtitle streams, scene count, size, and immutable hash verification.
- `submission-audit.json` — binding-deliverable completion audit; intentionally false until external and Bedrock execution blockers are resolved.

Development evidence, not release claims:

- `llm-contribution-exploration.json` — timchen's exploratory attribution of the
  generative-AI contribution: Bedrock node family +1.00% and Bedrock query
  normalization -0.06%, both with paired intervals crossing zero. 499 queries on
  a window already consumed by the locked confirmation, trained on 4.3% of the
  release training data, so it supports no claim. Read together with
  [`docs/evaluation-limits.md`](../docs/evaluation-limits.md).
- `ltr-tuning-*.json` — local validation-only model-selection traces; ignored
  from release commits.
- `ltr-ablation-validation.json` — earlier ungated validation experiment.
- `ltr-ablation-dev-test.json` — repeatedly inspected development-test experiment.
- `ablation-validation.json` / `ablation.json` — legacy deterministic heuristic benchmark.

The current release claim must be read from `ltr-quality-confirmation.json`
and independently checked against `ltr-quality-replication.json`. Both clear
the 5% NDCG gate, use the same frozen model, and have paired confidence
intervals that exclude zero. RC6 reports remain as historical evidence and
must not be mixed with the current quality release.
