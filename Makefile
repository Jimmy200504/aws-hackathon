.PHONY: demo test verify release coverage index benchmark package

demo:
	python3 -m app.server --port 8080

test:
	python3 -m py_compile app/*.py scripts/*.py tests/*.py
	python3 -m unittest discover -s tests -v

verify:
	python3 scripts/verify_release.py

release:
	./scripts/release_gate.sh

coverage:
	.venv/bin/python scripts/report_graph_coverage.py

index:
	python3 scripts/build_demo_index.py

benchmark:
	./scripts/run_ablation.sh

package:
	python3 scripts/package_lambda.py
