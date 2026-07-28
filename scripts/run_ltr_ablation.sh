#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python3 scripts/build_benchmark_fixture.py \
  --test-sample-bucket-start 400 \
  --test-sample-basis-points 1000 \
  --max-test-per-day 2000
python3 pipeline/build_ltr_pairs.py

.venv/bin/python pipeline/train_ltr.py \
  --train artifacts/ltr/train.jsonl \
  --validation artifacts/ltr/validation.jsonl \
  --feature-set behavior_graph \
  --n-estimators 20 \
  --max-depth 2 \
  --learning-rate 0.1 \
  --early-stopping-rounds 0 \
  --output artifacts/models/ltr-graph-final.ubj

.venv/bin/python pipeline/evaluate_ltr.py \
  --graph-model artifacts/models/ltr-graph-final.ubj \
  --pairs artifacts/ltr/validation.jsonl \
  --split validation \
  --confidence-gate behavior_job_edge \
  --output reports/ltr-ablation-validation-gated.json

.venv/bin/python pipeline/evaluate_ltr.py \
  --graph-model artifacts/models/ltr-graph-final.ubj \
  --pairs artifacts/ltr/test.jsonl \
  --split test \
  --confidence-gate behavior_job_edge \
  --output reports/ltr-ablation-test.json
