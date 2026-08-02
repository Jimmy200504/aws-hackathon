.PHONY: setup demo test verify release audit coverage business sam-smoke aws-smoke video submission external-preflight index benchmark quality package opensearch-local-up opensearch-local-down full-index-local full-demo-local query-artifacts graph-full-plan graph-full-build graph-review graph-review-score

PYTHON ?= .venv/bin/python
AWS_REGION ?= us-east-1
BEDROCK_QUERY_MODEL_ID ?= global.anthropic.claude-haiku-4-5-20251001-v1:0

setup:
	./scripts/setup_local.sh

# Build inputs for query normalization and the behavior-aware index.
query-artifacts:
	$(PYTHON) scripts/build_query_vocab.py
	$(PYTHON) scripts/build_top_queries.py
	$(PYTHON) scripts/build_job_behavior.py

demo:
	AWS_REGION="$(AWS_REGION)" BEDROCK_QUERY_MODEL_ID="$(BEDROCK_QUERY_MODEL_ID)" \
		$(PYTHON) -m app.server --port 8080 --require-bedrock-query-normalization

test:
	$(PYTHON) -m py_compile app/*.py pipeline/*.py scripts/*.py tests/*.py
	$(PYTHON) -m unittest discover -s tests -v

verify:
	$(PYTHON) scripts/verify_release.py

release:
	./scripts/release_gate.sh

audit:
	$(PYTHON) scripts/audit_submission.py

coverage:
	$(PYTHON) scripts/report_graph_coverage.py

business:
	$(PYTHON) scripts/report_business_impact.py

sam-smoke:
	$(PYTHON) scripts/run_sam_local_smoke.py

aws-smoke:
	$(PYTHON) scripts/run_aws_production_smoke.py

video:
	$(PYTHON) scripts/render_demo_video.py

submission:
	$(PYTHON) scripts/build_submission_packet.py

external-preflight:
	$(PYTHON) scripts/external_release_preflight.py

index:
	$(PYTHON) scripts/build_demo_index.py

benchmark:
	./scripts/run_ablation.sh

quality:
	./scripts/run_quality_confirmation.sh

package:
	$(PYTHON) scripts/package_lambda.py

opensearch-local-up:
	docker compose -f infra/docker-compose.opensearch.yaml up -d

opensearch-local-down:
	docker compose -f infra/docker-compose.opensearch.yaml down

full-index-local:
	$(PYTHON) scripts/index_full_opensearch.py --endpoint http://127.0.0.1:9200

full-demo-local:
	OPENSEARCH_ENDPOINT=http://127.0.0.1:9200 $(PYTHON) -m app.server --port 8080

graph-full-plan:
	$(PYTHON) scripts/run_full_graph_build.py --dry-run

graph-full-build:
	$(PYTHON) scripts/run_full_graph_build.py

graph-review:
	$(PYTHON) scripts/build_graph_review_packet.py

graph-review-score:
	$(PYTHON) scripts/score_graph_review.py
