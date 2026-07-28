.PHONY: demo test verify release audit coverage business sam-smoke index benchmark package

demo:
	python3 -m app.server --port 8080

test:
	python3 -m py_compile app/*.py scripts/*.py tests/*.py
	python3 -m unittest discover -s tests -v

verify:
	python3 scripts/verify_release.py

release:
	./scripts/release_gate.sh

audit:
	python3 scripts/audit_submission.py

coverage:
	.venv/bin/python scripts/report_graph_coverage.py

business:
	python3 scripts/report_business_impact.py

sam-smoke:
	python3 scripts/run_sam_local_smoke.py

index:
	python3 scripts/build_demo_index.py

benchmark:
	./scripts/run_ablation.sh

package:
	python3 scripts/package_lambda.py
