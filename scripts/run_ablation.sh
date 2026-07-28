#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f artifacts/demo-index.json ]]; then
  python3 scripts/build_demo_index.py
fi

python3 scripts/build_benchmark_fixture.py

python3 scripts/benchmark.py --index artifacts/benchmark-index.json --split validation --report reports/ablation-validation.json
python3 scripts/benchmark.py --index artifacts/benchmark-index.json --split test --report reports/ablation.json
