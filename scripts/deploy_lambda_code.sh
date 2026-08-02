#!/usr/bin/env bash
# Update the judge Lambda's code and query-normalization settings in place.
#
# Deliberately not `sam deploy`. The deployed stack carries parameters this
# repo's template does not define (they were deployed from an unpushed branch),
# so a full stack update would silently drop them. Updating the function
# directly changes only what this repo owns, and merges environment variables
# rather than replacing the map -- `update-function-configuration --environment`
# overwrites every key, so an unmerged call would erase the OpenSearch wiring.
set -euo pipefail
cd "$(dirname "$0")/.."

FUNCTION_NAME="${SKILLWEAVE_FUNCTION_NAME:-}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
STACK_NAME="${SKILLWEAVE_STACK_NAME:-skillweave-demo}"
ALIAS_NAME="${SKILLWEAVE_ALIAS_NAME:-live}"
BUNDLE="dist/skillweave-lambda.zip"
EXPECTED_AWS_ACCOUNT_ID="${SKILLWEAVE_EXPECTED_AWS_ACCOUNT_ID:-851558740348}"

if [[ "$AWS_REGION_NAME" != "us-east-1" ]]; then
  echo "Refusing deployment outside us-east-1: $AWS_REGION_NAME" >&2
  exit 1
fi
CURRENT_AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$CURRENT_AWS_ACCOUNT_ID" != "$EXPECTED_AWS_ACCOUNT_ID" ]]; then
  echo "Refusing deployment to AWS account $CURRENT_AWS_ACCOUNT_ID; expected $EXPECTED_AWS_ACCOUNT_ID" >&2
  exit 1
fi

if [[ -z "$FUNCTION_NAME" ]]; then
  FUNCTION_NAME="$(aws cloudformation describe-stack-resources \
    --stack-name "$STACK_NAME" --region "$AWS_REGION_NAME" \
    --query "StackResources[?ResourceType=='AWS::Lambda::Function'].PhysicalResourceId | [0]" \
    --output text)"
fi
if [[ -z "$FUNCTION_NAME" || "$FUNCTION_NAME" == "None" ]]; then
  echo "Could not resolve the Lambda function name; set SKILLWEAVE_FUNCTION_NAME" >&2
  exit 1
fi

echo "Packaging..."
.venv/bin/python scripts/package_lambda.py
for required in \
  config/query-intent-vocab.json \
  config/query-intent-prompt.txt \
  web/index.html \
  web/app.js \
  web/styles.css \
  artifacts/models/ltr-quality-remote-salary.trees.json; do
  .venv/bin/python - "$required" <<'PY'
import sys, zipfile
name = sys.argv[1]
if name not in zipfile.ZipFile("dist/skillweave-lambda.zip").namelist():
    raise SystemExit(f"{name} missing from the bundle; deployment is incomplete")
PY
done

echo "Updating code on ${FUNCTION_NAME}..."
aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --region "$AWS_REGION_NAME" \
  --zip-file "fileb://$BUNDLE" >/dev/null
aws lambda wait function-updated \
  --function-name "$FUNCTION_NAME" --region "$AWS_REGION_NAME"

echo "Merging environment..."
CURRENT="$(aws lambda get-function-configuration \
  --function-name "$FUNCTION_NAME" --region "$AWS_REGION_NAME" \
  --query 'Environment.Variables' --output json)"
MERGED="$(BEDROCK_QUERY_MODEL_ID_VALUE="${BEDROCK_QUERY_MODEL_ID:-global.anthropic.claude-haiku-4-5-20251001-v1:0}" \
  BEDROCK_EMBEDDING_MODEL_ID_VALUE="${BEDROCK_EMBEDDING_MODEL_ID:-}" \
  .venv/bin/python - "$CURRENT" <<'PY'
import json, os, sys

current = json.loads(sys.argv[1]) or {}
current.update(
    {
        "BEDROCK_QUERY_MODEL_ID": os.environ["BEDROCK_QUERY_MODEL_ID_VALUE"],
        # A batch of ten structured intents needs far more than the 128 tokens a
        # single-answer budget allowed; the old value truncated every response.
        "BEDROCK_QUERY_MAX_TOKENS": "4000",
        "BEDROCK_QUERY_MAX_BATCH": "10",
        # One invocation serves one request, so there is no sibling query to
        # coalesce with and waiting would only add latency.
        "BEDROCK_QUERY_MAX_WAIT_SECONDS": "0.05",
        # Measured complex queries can take 4.6 seconds; keep the documented
        # six-second request budget instead of degrading them prematurely.
        "BEDROCK_QUERY_DEADLINE_SECONDS": "6.0",
        "QUERY_VOCAB_PATH": "/var/task/config/query-intent-vocab.json",
        "QUERY_INTENTS_PATH": "/var/task/config/query-intents.json",
        # This script does not run `sam deploy`, so template.yaml's value never
        # reaches the live function. The bundle ships exactly one ranking model
        # and the ranker silently disables reranking when the path is missing,
        # so pin the key here to keep code and configuration in lockstep.
        "LTR_MODEL_PATH": (
            "/var/task/artifacts/models/ltr-quality-remote-salary.trees.json"
        ),
    }
)
# Hybrid retrieval is opt-in and must survive a deploy that does not mention
# it: only write the key when the operator supplies a value, and let an
# explicit empty value turn the vector leg back off.
embedding_model = os.environ["BEDROCK_EMBEDDING_MODEL_ID_VALUE"].strip()
if embedding_model:
    current["BEDROCK_EMBEDDING_MODEL_ID"] = embedding_model
elif "BEDROCK_EMBEDDING_MODEL_ID" in os.environ:
    current.pop("BEDROCK_EMBEDDING_MODEL_ID", None)
print(json.dumps({"Variables": current}, ensure_ascii=False))
PY
)"
aws lambda update-function-configuration \
  --function-name "$FUNCTION_NAME" \
  --region "$AWS_REGION_NAME" \
  --environment "$MERGED" >/dev/null
aws lambda wait function-updated \
  --function-name "$FUNCTION_NAME" --region "$AWS_REGION_NAME"

# API Gateway invokes the stable alias, not $LATEST. Publish only after both
# code and environment have converged so the immutable version contains the
# complete release, then atomically move production traffic to it.
PUBLISHED_VERSION="$(aws lambda publish-version \
  --function-name "$FUNCTION_NAME" \
  --region "$AWS_REGION_NAME" \
  --query Version --output text)"
aws lambda update-alias \
  --function-name "$FUNCTION_NAME" \
  --name "$ALIAS_NAME" \
  --function-version "$PUBLISHED_VERSION" \
  --routing-config '{"AdditionalVersionWeights":{}}' \
  --region "$AWS_REGION_NAME" >/dev/null
echo "Alias ${ALIAS_NAME} now points to version ${PUBLISHED_VERSION}."

DEMO_URL="$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$AWS_REGION_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='DemoUrl'].OutputValue | [0]" \
  --output text)"
if [[ "$DEMO_URL" != https://* ]]; then
  echo "Deployment completed but DemoUrl was not an HTTPS URL: $DEMO_URL" >&2
  exit 1
fi

VERIFY_ARGS=(--url "$DEMO_URL")
if [[ "${SKILLWEAVE_REQUIRE_FULL_CORPUS:-yes}" == "yes" ]]; then
  VERIFY_ARGS+=(--require-full-corpus)
fi
if [[ "${SKILLWEAVE_REQUIRE_NEPTUNE:-yes}" == "yes" ]]; then
  VERIFY_ARGS+=(--require-neptune)
fi
EXPECTED_GRAPH_VERSION="${SKILLWEAVE_EXPECTED_GRAPH_VERSION-deterministic-v1-rules-v2-evaluation-cutoff}"
if [[ -n "$EXPECTED_GRAPH_VERSION" ]]; then
  VERIFY_ARGS+=(--expected-graph-version "$EXPECTED_GRAPH_VERSION")
fi

echo "Verifying exact frontend assets and backend contract on $DEMO_URL..."
.venv/bin/python scripts/verify_app_deployment.py "${VERIFY_ARGS[@]}"
echo "SkillWeave frontend and backend deployed: $DEMO_URL"
