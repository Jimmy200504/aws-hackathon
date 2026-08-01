.PHONY: demo test verify release audit coverage business sam-smoke aws-smoke video submission external-preflight index benchmark quality package opensearch-local-up opensearch-local-down full-index-local full-demo-local graph-full-plan graph-full-build graph-review graph-review-score

demo:
	python3 -m app.server --port 8080

test:
	python3 -m py_compile app/*.py pipeline/*.py scripts/*.py tests/*.py
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

aws-smoke:
	python3 scripts/run_aws_production_smoke.py

video:
	python3 scripts/render_demo_video.py

submission:
	python3 scripts/build_submission_packet.py

external-preflight:
	python3 scripts/external_release_preflight.py

index:
	python3 scripts/build_demo_index.py

benchmark:
	./scripts/run_ablation.sh

quality:
	./scripts/run_quality_confirmation.sh

package:
	python3 scripts/package_lambda.py

opensearch-local-up:
	docker compose -f infra/docker-compose.opensearch.yaml up -d

opensearch-local-down:
	docker compose -f infra/docker-compose.opensearch.yaml down

full-index-local:
	python3 scripts/index_full_opensearch.py --endpoint http://127.0.0.1:9200

full-demo-local:
	OPENSEARCH_ENDPOINT=http://127.0.0.1:9200 python3 -m app.server --port 8080

graph-full-plan:
	python3 scripts/run_full_graph_build.py --dry-run

graph-full-build:
	python3 scripts/run_full_graph_build.py

graph-review:
	python3 scripts/build_graph_review_packet.py

graph-review-score:
	python3 scripts/score_graph_review.py
