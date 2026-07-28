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
  --query 'Stacks[0].Outputs' \
  --output table
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
