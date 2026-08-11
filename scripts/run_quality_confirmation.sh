#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
DATA_DIR="${DATA_DIR:-data/dataset}"
WORK_DIR="${WORK_DIR:-artifacts/quality}"
GRAPH_WORK_ROOT="${GRAPH_WORK_ROOT:-artifacts/skill-graph-full-v2}"
GRAPH_RUN_ID="${GRAPH_RUN_ID:-deterministic-v2-rules-v3-full}"
GRAPH_VERSION="${GRAPH_VERSION:-deterministic-v2-rules-v3}"
MODEL="artifacts/models/ltr-quality-final.ubj"
GRAPH_MANIFEST="$GRAPH_WORK_ROOT/release/runs/$GRAPH_RUN_ID/evaluation-cutoff/manifest.json"
GRAPH_NODES="$GRAPH_WORK_ROOT/resolved/evaluation-cutoff/nodes.jsonl"
GRAPH_RESOLVED_JOBS="$GRAPH_WORK_ROOT/resolved/evaluation-cutoff/jobs.jsonl"
GRAPH_JOB_EDGES="$GRAPH_WORK_ROOT/resolved/evaluation-cutoff/job-skill-edges.jsonl"
GRAPH_RELATION_EDGES="$GRAPH_WORK_ROOT/relations/evaluation-cutoff/relation-edges.jsonl"

mkdir -p "$WORK_DIR/primary/ltr-overlay" "$WORK_DIR/replication/ltr-overlay"

# This script evaluates the already-committed model; it never retrains it.
# Retraining is a deliberate, separate action (see the error below and
# README) because it is not guaranteed to reproduce or beat the shipped
# model's performance even with identical hyperparameters and a fixed seed —
# measured on this pipeline, a from-scratch retrain scored measurably below
# the committed model on the replication bucket. Silently overwriting a
# verified model with a worse one on every `make quality` run is worse than
# just failing loudly here.
if [[ ! -f "$MODEL" ]]; then
  echo "$MODEL is missing." >&2
  echo "This script only evaluates an existing model; it does not train one." >&2
  echo "Train it explicitly first (see README '重現 benchmark' -> '3. 重新訓練模型'" >&2
  echo "for the exact overlay-based train_ltr.py invocation and its caveats)," >&2
  echo "review the resulting metrics, then re-run this script." >&2
  exit 1
fi

# The confirmation/replication numbers this script produces are only
# meaningful bound to the full statistical Skill Graph (RELATED_TO edges from
# the whole corpus), not the ontology's manually reviewed hint weights alone.
# Build it once and reuse it across both buckets rather than silently
# training on a weaker signal.
if [[ ! -f "$GRAPH_MANIFEST" ]]; then
  echo "0/6 Build the full deterministic Skill Graph (no manifest at $GRAPH_MANIFEST)"
  "$PYTHON" scripts/run_full_graph_build.py \
    --work-root "$GRAPH_WORK_ROOT" \
    --run-id "$GRAPH_RUN_ID" \
    --graph-version "$GRAPH_VERSION" \
    --cutoff '2026-06-05 23:59:59.999'
fi

echo "1/6 Build the frozen training, validation, and primary confirmation fixture"
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

echo "2/6 Bind the full statistical RELATED_TO graph and materialize grouped LTR rows"
"$PYTHON" scripts/build_v2_ranking_overlay.py \
  --base-index "$WORK_DIR/primary/benchmark-index.json" \
  --qrels "$WORK_DIR/primary/temporal-eval.json" \
  --graph-manifest "$GRAPH_MANIFEST" \
  --nodes "$GRAPH_NODES" \
  --resolved-jobs "$GRAPH_RESOLVED_JOBS" \
  --job-edges "$GRAPH_JOB_EDGES" \
  --relation-edges "$GRAPH_RELATION_EDGES" \
  --reviewed-ontology config/skill_ontology.seed.json \
  --output "$WORK_DIR/primary/overlay-index.json"
"$PYTHON" pipeline/build_ltr_pairs.py \
  --index "$WORK_DIR/primary/overlay-index.json" \
  --qrels "$WORK_DIR/primary/temporal-eval.json" \
  --output-dir "$WORK_DIR/primary/ltr-overlay"

echo "3/6 Evaluate the committed model against the untouched primary confirmation"
"$PYTHON" pipeline/evaluate_ltr.py \
  --graph-model "$MODEL" \
  --pairs "$WORK_DIR/primary/ltr-overlay/test.jsonl" \
  --qrels "$WORK_DIR/primary/temporal-eval.json" \
  --graph-binding-manifest "$WORK_DIR/primary/overlay-index.manifest.json" \
  --output reports/ltr-quality-confirmation.json \
  --split test \
  --confidence-gate none

echo "Export and verify dependency-free Lambda inference"
"$PYTHON" scripts/export_portable_ltr.py \
  --model "$MODEL"
"$PYTHON" scripts/verify_portable_ltr.py \
  --model "$MODEL" \
  --pairs "$WORK_DIR/primary/ltr-overlay/test.jsonl"
python3 scripts/enrich_demo_behavior.py \
  --benchmark-index "$WORK_DIR/primary/benchmark-index.json"

echo "4/6 Attribute feature-family contributions on the locked primary result"
"$PYTHON" scripts/report_quality_ablation.py \
  --model "$MODEL" \
  --pairs "$WORK_DIR/primary/ltr-overlay/test.jsonl" \
  --output reports/ltr-quality-component-ablation.json

echo "5/6 Build a second disjoint confirmation bucket"
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

echo "6/6 Bind the same full statistical RELATED_TO graph for the replication bucket"
"$PYTHON" scripts/build_v2_ranking_overlay.py \
  --base-index "$WORK_DIR/replication/benchmark-index.json" \
  --qrels "$WORK_DIR/replication/temporal-eval.json" \
  --graph-manifest "$GRAPH_MANIFEST" \
  --nodes "$GRAPH_NODES" \
  --resolved-jobs "$GRAPH_RESOLVED_JOBS" \
  --job-edges "$GRAPH_JOB_EDGES" \
  --relation-edges "$GRAPH_RELATION_EDGES" \
  --reviewed-ontology config/skill_ontology.seed.json \
  --output "$WORK_DIR/replication/overlay-index.json"
"$PYTHON" pipeline/build_ltr_pairs.py \
  --index "$WORK_DIR/replication/overlay-index.json" \
  --qrels "$WORK_DIR/replication/temporal-eval.json" \
  --output-dir "$WORK_DIR/replication/ltr-overlay"

echo "Evaluate the committed model against the replication bucket"
"$PYTHON" pipeline/evaluate_ltr.py \
  --graph-model "$MODEL" \
  --pairs "$WORK_DIR/replication/ltr-overlay/test.jsonl" \
  --qrels "$WORK_DIR/replication/temporal-eval.json" \
  --graph-binding-manifest "$WORK_DIR/replication/overlay-index.manifest.json" \
  --output reports/ltr-quality-replication.json \
  --split test \
  --confidence-gate none

"$PYTHON" scripts/verify_quality_release.py
