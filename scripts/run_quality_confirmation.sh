#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
DATA_DIR="${DATA_DIR:-data/dataset}"
WORK_DIR="${WORK_DIR:-artifacts/quality}"
MODEL="artifacts/models/ltr-quality-final.ubj"

mkdir -p "$WORK_DIR/primary/ltr" "$WORK_DIR/replication/ltr"

echo "1/7 Build the frozen training, validation, and primary confirmation fixture"
"$PYTHON" scripts/build_benchmark_fixture.py \
  --data-dir "$DATA_DIR" \
  --qrels-output "$WORK_DIR/primary/temporal-eval.json" \
  --index-output "$WORK_DIR/primary/benchmark-index.json" \
  --train-sample-basis-points 1000 \
  --eval-sample-basis-points 1000 \
  --test-sample-bucket-start 2400 \
  --test-sample-basis-points 1000 \
  --max-train-per-day 15000 \
  --max-eval-per-day 2000 \
  --max-test-per-day 2000

echo "2/7 Materialize grouped LTR rows"
"$PYTHON" pipeline/build_ltr_pairs.py \
  --index "$WORK_DIR/primary/benchmark-index.json" \
  --qrels "$WORK_DIR/primary/temporal-eval.json" \
  --output-dir "$WORK_DIR/primary/ltr"

echo "3/7 Train the frozen unbiased LambdaMART configuration"
"$PYTHON" pipeline/train_ltr.py \
  --train "$WORK_DIR/primary/ltr/train.jsonl" \
  --train-extra "$WORK_DIR/primary/ltr/validation.jsonl" \
  --validation "$WORK_DIR/primary/ltr/validation.jsonl" \
  --output "$MODEL" \
  --feature-set quality_minimal \
  --n-estimators 40 \
  --max-depth 4 \
  --min-child-weight 12 \
  --learning-rate 0.05 \
  --early-stopping-rounds 0

echo "4/7 Evaluate the untouched primary confirmation"
"$PYTHON" pipeline/evaluate_ltr.py \
  --graph-model "$MODEL" \
  --pairs "$WORK_DIR/primary/ltr/test.jsonl" \
  --qrels "$WORK_DIR/primary/temporal-eval.json" \
  --output reports/ltr-quality-confirmation.json \
  --split test \
  --confidence-gate none

echo "Export and verify dependency-free Lambda inference"
"$PYTHON" scripts/export_portable_ltr.py \
  --model "$MODEL"
"$PYTHON" scripts/verify_portable_ltr.py \
  --model "$MODEL" \
  --pairs "$WORK_DIR/primary/ltr/test.jsonl"
python3 scripts/enrich_demo_behavior.py \
  --benchmark-index "$WORK_DIR/primary/benchmark-index.json"

echo "5/7 Attribute feature-family contributions on the locked primary result"
"$PYTHON" scripts/report_quality_ablation.py \
  --model "$MODEL" \
  --pairs "$WORK_DIR/primary/ltr/test.jsonl" \
  --output reports/ltr-quality-component-ablation.json

echo "6/7 Build a second disjoint confirmation bucket"
"$PYTHON" scripts/build_benchmark_fixture.py \
  --data-dir "$DATA_DIR" \
  --qrels-output "$WORK_DIR/replication/temporal-eval.json" \
  --index-output "$WORK_DIR/replication/benchmark-index.json" \
  --train-sample-basis-points 1000 \
  --eval-sample-basis-points 1000 \
  --test-sample-bucket-start 3400 \
  --test-sample-basis-points 1000 \
  --max-train-per-day 15000 \
  --max-eval-per-day 2000 \
  --max-test-per-day 2000
"$PYTHON" pipeline/build_ltr_pairs.py \
  --index "$WORK_DIR/replication/benchmark-index.json" \
  --qrels "$WORK_DIR/replication/temporal-eval.json" \
  --output-dir "$WORK_DIR/replication/ltr"

echo "7/7 Replicate with the exact same frozen model"
"$PYTHON" pipeline/evaluate_ltr.py \
  --graph-model "$MODEL" \
  --pairs "$WORK_DIR/replication/ltr/test.jsonl" \
  --qrels "$WORK_DIR/replication/temporal-eval.json" \
  --output reports/ltr-quality-replication.json \
  --split test \
  --confidence-gate none

"$PYTHON" scripts/verify_quality_release.py
