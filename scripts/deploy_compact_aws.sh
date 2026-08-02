#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

STACK_NAME="${SKILLWEAVE_STACK_NAME:-skillweave-demo}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
STAGE_NAME="${SKILLWEAVE_STAGE_NAME:-prod}"
RESERVED_CONCURRENCY="${SKILLWEAVE_RESERVED_CONCURRENCY:-0}"
OPENSEARCH_ENDPOINT_VALUE="${OPENSEARCH_ENDPOINT:-}"
OPENSEARCH_COLLECTION_ARN_VALUE="${OPENSEARCH_COLLECTION_ARN:-}"
OPENSEARCH_INDEX_VALUE="${OPENSEARCH_INDEX:-skillweave-jobs-v1}"
OPENSEARCH_DOCUMENT_COUNT_VALUE="${OPENSEARCH_DOCUMENT_COUNT:-0}"
OPENSEARCH_SERVICE_VALUE="${OPENSEARCH_SERVICE:-none}"
BEDROCK_QUERY_MODEL_ID_VALUE="${BEDROCK_QUERY_MODEL_ID:-us.anthropic.claude-sonnet-4-6}"
NEPTUNE_GRAPH_ID_VALUE="${NEPTUNE_GRAPH_ID:-}"
NEPTUNE_GRAPH_REGION_VALUE="${NEPTUNE_GRAPH_REGION:-$AWS_REGION_NAME}"
GRAPH_VERSION_VALUE="${GRAPH_VERSION:-evaluation-cutoff-embedded}"
SKILL_ALIAS_INDEX_VALUE="${SKILL_ALIAS_INDEX:-skillweave-skill-alias-v1}"

if [[ -n "$OPENSEARCH_ENDPOINT_VALUE" || -n "$OPENSEARCH_COLLECTION_ARN_VALUE" ]]; then
  if [[ -z "$OPENSEARCH_ENDPOINT_VALUE" || -z "$OPENSEARCH_COLLECTION_ARN_VALUE" ]]; then
    echo "OPENSEARCH_ENDPOINT and OPENSEARCH_COLLECTION_ARN must be supplied together" >&2
    exit 1
  fi
fi

PARAMETER_OVERRIDES=(
  "StageName=$STAGE_NAME"
  "ReservedConcurrency=$RESERVED_CONCURRENCY"
  "OpenSearchIndex=$OPENSEARCH_INDEX_VALUE"
  "OpenSearchDocumentCount=$OPENSEARCH_DOCUMENT_COUNT_VALUE"
  "OpenSearchService=$OPENSEARCH_SERVICE_VALUE"
  "BedrockQueryModelId=$BEDROCK_QUERY_MODEL_ID_VALUE"
  "NeptuneGraphId=$NEPTUNE_GRAPH_ID_VALUE"
  "NeptuneGraphRegion=$NEPTUNE_GRAPH_REGION_VALUE"
  "GraphVersion=$GRAPH_VERSION_VALUE"
  "SkillAliasIndex=$SKILL_ALIAS_INDEX_VALUE"
)
if [[ -n "$OPENSEARCH_ENDPOINT_VALUE" ]]; then
  PARAMETER_OVERRIDES+=(
    "OpenSearchEndpoint=$OPENSEARCH_ENDPOINT_VALUE"
    "OpenSearchCollectionArn=$OPENSEARCH_COLLECTION_ARN_VALUE"
  )
fi

./scripts/release_gate.sh
sam validate --lint --template-file infra/template.yaml
sam build --template-file infra/template.yaml
sam deploy \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION_NAME" \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  --parameter-overrides "${PARAMETER_OVERRIDES[@]}"

DEMO_URL="$(
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='DemoUrl'].OutputValue | [0]" \
    --output text
)"
if [[ "$DEMO_URL" != https://* ]]; then
  echo "Deployment completed but DemoUrl was not an HTTPS URL: $DEMO_URL" >&2
  exit 1
fi

curl --fail --silent --show-error "${DEMO_URL%/}/health"
curl --fail --silent --show-error \
  --request POST "${DEMO_URL%/}/api/v1/jobs/search" \
  --header "content-type: application/json" \
  --data '{"query":"後端工程師 Node.js","location_code":["100100"],"top_k":10}'
VERIFY_ARGS=(--url "$DEMO_URL")
if [[ -n "$OPENSEARCH_ENDPOINT_VALUE" ]]; then
  VERIFY_ARGS+=(--require-full-corpus)
fi
if [[ -n "$NEPTUNE_GRAPH_ID_VALUE" ]]; then
  VERIFY_ARGS+=(--require-neptune --expected-graph-version "$GRAPH_VERSION_VALUE")
fi
.venv/bin/python scripts/verify_app_deployment.py "${VERIFY_ARGS[@]}"
python3 scripts/update_release_urls.py --aws-url "$DEMO_URL"
python3 scripts/verify_release.py

echo
echo "SkillWeave compact demo deployed: $DEMO_URL"
