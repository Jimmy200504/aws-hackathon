.PHONY: setup demo test coverage sam-smoke aws-smoke video index benchmark ltr-ablation quality package opensearch-local-up opensearch-local-down full-index-local full-demo-local query-artifacts graph-full-plan graph-full-build graph-review graph-review-score verify-inputs clean-search-log

PYTHON ?= .venv/bin/python
AWS_REGION ?= us-east-1
BEDROCK_QUERY_MODEL_ID ?= global.anthropic.claude-haiku-4-5-20251001-v1:0

setup:
	./scripts/setup_local.sh

# Confirm every developer builds from byte-identical inputs. The deterministic
# pipeline only reproduces identical artifacts when these match.
verify-inputs:
	$(PYTHON) scripts/verify_dataset_inputs.py

# Remove SEO spam and URL queries from the organizer search log. Required
# before quality/index builds, which read userSearchLog_cleaned.csv.
clean-search-log:
	$(PYTHON) scripts/clean_search_log.py

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

coverage:
	$(PYTHON) scripts/report_graph_coverage.py

sam-smoke:
	$(PYTHON) scripts/run_sam_local_smoke.py

aws-smoke:
	$(PYTHON) scripts/run_aws_production_smoke.py

video:
	$(PYTHON) scripts/render_demo_video.py

index:
	$(PYTHON) scripts/build_demo_index.py

benchmark:
	./scripts/run_ablation.sh

# Reproduces artifacts/models/ltr-graph-final.ubj, the model `make coverage`
# evaluates by default. Overwrites the committed model with a freshly trained
# one; only run this when deliberately updating it.
ltr-ablation:
	./scripts/run_ltr_ablation.sh

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
