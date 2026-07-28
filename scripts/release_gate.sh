#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m py_compile app/*.py pipeline/*.py scripts/*.py tests/*.py
python3 -m unittest discover -s tests -v
python3 scripts/report_business_impact.py
python3 scripts/package_lambda.py
python3 -m zipfile -t dist/skillweave-lambda.zip
python3 scripts/audit_submission.py
python3 scripts/verify_release.py
