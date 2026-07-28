# AWS Demo Deployment Runbook

這份 runbook 部署「可評審的 compact demo」：API Gateway HTTP API + Lambda + 12,000 筆真實職缺 artifact。完整 production OpenSearch/Neptune/SageMaker 架構另見 `aws-architecture.md`。

## 前置

- AWS account 與可建立 CloudFormation、Lambda、API Gateway、CloudWatch 的 role
- AWS CLI v2
- AWS SAM CLI
- region 支援 `python3.13` Lambda runtime

## Package

```bash
python3 scripts/build_demo_index.py
python3 scripts/package_lambda.py
unzip -t dist/skillweave-lambda.zip
```

Package 只包含：

- `app/`
- `web/`
- `artifacts/demo-index.json`

不會把 3.8 GB 原始 CSV 上傳。

## Validate and deploy

完整 release gate 與一鍵部署：

```bash
./scripts/release_gate.sh
./scripts/deploy_compact_aws.sh
```

第二個指令會 package、驗證、部署、讀取 CloudFormation `DemoUrl`、執行
external health/search smoke，最後把真實 AWS URL 寫入 `release-manifest.json`。
可用 `SKILLWEAVE_STACK_NAME`、`AWS_REGION`、`SKILLWEAVE_STAGE_NAME` 與
`SKILLWEAVE_RESERVED_CONCURRENCY` 覆寫預設值。

等價的逐步指令：

```bash
sam validate --lint --template-file infra/template.yaml
sam build --template-file infra/template.yaml
sam deploy \
  --stack-name skillweave-demo \
  --region ap-northeast-1 \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides StageName=prod ReservedConcurrency=10
```

取得網址：

```bash
aws cloudformation describe-stacks \
  --stack-name skillweave-demo \
  --region ap-northeast-1 \
  --query 'Stacks[0].Outputs' \
  --output table
```

GitHub 與影片完成後只接受 public HTTPS URL：

```bash
python3 scripts/update_release_urls.py \
  --github-url "https://github.com/ORG/REPO/releases/tag/TAG" \
  --demo-video-url "https://VIDEO_HOST/VIDEO_ID"
python3 scripts/verify_release.py
```

## External smoke

使用不帶 AWS session 的網路執行：

```bash
curl -fsS "$DEMO_URL/health"
curl -fsS -X POST "$DEMO_URL/api/v1/jobs/search" \
  -H 'content-type: application/json' \
  -d '{"query":"後端工程師 Node.js","location_code":["100100"]}'
```

驗證：

- URL 直接顯示 UI
- health 的 index version 符合 release
- API 回傳 20 筆
- rank 1～20 連續
- Top result 有合理職稱／地區
- response latency 被 CloudWatch access log 記錄

## Production migration

Compact Lambda 是交付安全網，不冒充完整 production architecture。升級順序：

1. S3 + Glue + Step Functions temporal manifest。
2. Bedrock batch structured extraction + evidence validator。
3. OpenSearch hybrid index。
4. Neptune graph import/traversal。
5. SageMaker Unbiased LambdaMART endpoint。
6. Fargate orchestrator 替換 Lambda scan。
7. 保留相同 API Gateway route 與 response contract。

## Rollback

不要刪 stack 才回滾。先把 Lambda alias 指回上一個經驗證版本，或重新部署上一個 release package。資料／index／model manifest 必須一起回退。
