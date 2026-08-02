#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${SKILLWEAVE_ENABLE_PAID_FULL_INDEX:-}" != "yes" ]]; then
  echo "Full OpenSearch deployment creates billable AWS resources." >&2
  echo "Re-run with SKILLWEAVE_ENABLE_PAID_FULL_INDEX=yes after reviewing the account and region." >&2
  exit 2
fi

DEMO_STACK_NAME="${SKILLWEAVE_STACK_NAME:-skillweave-demo}"
SEARCH_STACK_NAME="${SKILLWEAVE_SEARCH_STACK_NAME:-skillweave-full-search}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
COLLECTION_NAME="${SKILLWEAVE_COLLECTION_NAME:-skillweave-jobs}"
COLLECTION_GROUP_NAME="${SKILLWEAVE_COLLECTION_GROUP_NAME:-skillweave-search}"
INDEX_NAME="${OPENSEARCH_INDEX:-skillweave-jobs-v1}"
PYTHON="${PYTHON:-.venv/bin/python}"
INDEX_BATCH_SIZE="${SKILLWEAVE_INDEX_BATCH_SIZE:-2000}"
INDEX_WORKERS="${SKILLWEAVE_INDEX_WORKERS:-4}"
INDEX_SKIP_CREATE="${SKILLWEAVE_INDEX_SKIP_CREATE:-no}"
INDEX_START_RECORD="${SKILLWEAVE_INDEX_START_RECORD:-0}"
INDEX_EXPECTED_COUNT="${SKILLWEAVE_INDEX_EXPECTED_COUNT:-0}"
MAX_INDEXING_OCU="${SKILLWEAVE_MAX_INDEXING_OCU:-8}"
MAX_SEARCH_OCU="${SKILLWEAVE_MAX_SEARCH_OCU:-2}"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing Python environment: $PYTHON" >&2
  echo "Create it and install requirements-production.lock before deployment." >&2
  exit 1
fi

RUNTIME_ROLE_NAME="$(
  aws cloudformation describe-stack-resources \
    --stack-name "$DEMO_STACK_NAME" \
    --region "$AWS_REGION_NAME" \
    --query "StackResources[?ResourceType=='AWS::IAM::Role'].PhysicalResourceId | [0]" \
    --output text
)"
if [[ -z "$RUNTIME_ROLE_NAME" || "$RUNTIME_ROLE_NAME" == "None" ]]; then
  echo "Could not resolve the deployed Lambda execution role from $DEMO_STACK_NAME" >&2
  exit 1
fi
RUNTIME_PRINCIPAL_ARN="$(
  aws iam get-role \
    --role-name "$RUNTIME_ROLE_NAME" \
    --query "Role.Arn" \
    --output text
)"
INGESTION_PRINCIPAL_ARN="${SKILLWEAVE_INGESTION_PRINCIPAL_ARN:-$(
  aws sts get-caller-identity --query Arn --output text
)}"
if [[ "$INGESTION_PRINCIPAL_ARN" == arn:aws:sts::*:assumed-role/* ]]; then
  echo "Set SKILLWEAVE_INGESTION_PRINCIPAL_ARN to the underlying IAM role ARN, not an STS session ARN." >&2
  exit 1
fi

aws cloudformation deploy \
  --stack-name "$SEARCH_STACK_NAME" \
  --region "$AWS_REGION_NAME" \
  --template-file infra/opensearch-serverless.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    "CollectionName=$COLLECTION_NAME" \
    "CollectionGroupName=$COLLECTION_GROUP_NAME" \
    "MaxIndexingOcu=$MAX_INDEXING_OCU" \
    "MaxSearchOcu=$MAX_SEARCH_OCU" \
    "RuntimePrincipalArn=$RUNTIME_PRINCIPAL_ARN" \
    "IngestionPrincipalArn=$INGESTION_PRINCIPAL_ARN" \
  --no-fail-on-empty-changeset

COLLECTION_ENDPOINT="$(
  aws cloudformation describe-stacks \
    --stack-name "$SEARCH_STACK_NAME" \
    --region "$AWS_REGION_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='CollectionEndpoint'].OutputValue | [0]" \
    --output text
)"
COLLECTION_ARN="$(
  aws cloudformation describe-stacks \
    --stack-name "$SEARCH_STACK_NAME" \
    --region "$AWS_REGION_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='CollectionArn'].OutputValue | [0]" \
    --output text
)"
INGESTION_ROLE_ARN="$(
  aws cloudformation describe-stacks \
    --stack-name "$SEARCH_STACK_NAME" \
    --region "$AWS_REGION_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='IngestionRoleArn'].OutputValue | [0]" \
    --output text
)"
if [[ "$COLLECTION_ENDPOINT" != https://* || "$COLLECTION_ARN" != arn:aws:aoss:* || "$INGESTION_ROLE_ARN" != arn:aws:iam::*:role/* ]]; then
  echo "OpenSearch stack did not return a valid endpoint and ARN" >&2
  exit 1
fi

read -r AWS_ACCESS_KEY_ID_VALUE AWS_SECRET_ACCESS_KEY_VALUE AWS_SESSION_TOKEN_VALUE < <(
  aws sts assume-role \
    --role-arn "$INGESTION_ROLE_ARN" \
    --role-session-name skillweave-full-index \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text
)
INDEXER_ARGS=(
  --batch-size "$INDEX_BATCH_SIZE"
  --workers "$INDEX_WORKERS"
  --start-record "$INDEX_START_RECORD"
  --expected-count "$INDEX_EXPECTED_COUNT"
)
if [[ "$INDEX_SKIP_CREATE" == "yes" ]]; then
  INDEXER_ARGS+=(--skip-create)
fi
OPENSEARCH_ENDPOINT="$COLLECTION_ENDPOINT" \
OPENSEARCH_INDEX="$INDEX_NAME" \
AWS_REGION="$AWS_REGION_NAME" \
AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID_VALUE" \
AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY_VALUE" \
AWS_SESSION_TOKEN="$AWS_SESSION_TOKEN_VALUE" \
  "$PYTHON" scripts/index_full_opensearch.py "${INDEXER_ARGS[@]}"

OPENSEARCH_ENDPOINT="$COLLECTION_ENDPOINT" \
OPENSEARCH_COLLECTION_ARN="$COLLECTION_ARN" \
OPENSEARCH_INDEX="$INDEX_NAME" \
AWS_REGION="$AWS_REGION_NAME" \
  ./scripts/deploy_compact_aws.sh

"$PYTHON" scripts/run_aws_production_smoke.py --require-full-corpus
"$PYTHON" scripts/verify_release.py

echo
echo "Full-corpus search deployed."
echo "OpenSearch endpoint: $COLLECTION_ENDPOINT"
echo "Verified source index: $INDEX_NAME"
