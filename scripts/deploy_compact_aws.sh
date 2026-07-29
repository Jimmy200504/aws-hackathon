#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

STACK_NAME="${SKILLWEAVE_STACK_NAME:-skillweave-demo}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
STAGE_NAME="${SKILLWEAVE_STAGE_NAME:-prod}"
RESERVED_CONCURRENCY="${SKILLWEAVE_RESERVED_CONCURRENCY:-0}"

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
  --parameter-overrides \
    "StageName=$STAGE_NAME" \
    "ReservedConcurrency=$RESERVED_CONCURRENCY"

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
python3 scripts/update_release_urls.py --aws-url "$DEMO_URL"
python3 scripts/verify_release.py

echo
echo "SkillWeave compact demo deployed: $DEMO_URL"
