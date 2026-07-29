# AWS Demo Deployment Runbook

這份 runbook 部署「可評審的 compact demo」：API Gateway HTTP API + Lambda + 12,000 筆真實職缺 artifact。完整 production OpenSearch/Neptune/SageMaker 架構另見 `aws-architecture.md`。

## 前置

- AWS account 與可建立 CloudFormation、Lambda、API Gateway、CloudWatch 的 role
- AWS CLI v2
- AWS SAM CLI
- region 支援 `python3.13` Lambda runtime

本機已驗證工具版本：AWS CLI `2.36.9`、SAM CLI `1.164.0`、GitHub CLI
`2.96.0`。工具已就緒，但 release manifest 不會把「已安裝」誤寫成「已登入／
已部署」。

先用唯讀 preflight 檢查 credentials、release tag、影片 hash、Git remote 與
敏感資料：

```bash
python3 scripts/external_release_preflight.py
# 登入後需要把未通過狀態視為錯誤時：
python3 scripts/external_release_preflight.py --require-ready
```

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

本機以正式 Lambda Python 3.13 arm64 emulation image 驗證 bundle：

```bash
python3 scripts/run_sam_local_smoke.py
```

結果保存為 `reports/sam-local-smoke.json`，並檢查 HTTP 200、Top 10、rank
連續、job ID 去重、index version 與 graph provenance。

## Validate and deploy

完整 release gate 與一鍵部署：

```bash
./scripts/release_gate.sh
./scripts/deploy_compact_aws.sh
```

第二個指令會 package、驗證、部署、讀取 CloudFormation `DemoUrl`、執行
external health/search smoke，最後把真實 AWS URL 寫入 `release-manifest.json`。
可用 `SKILLWEAVE_STACK_NAME`、`AWS_REGION`、`SKILLWEAVE_STAGE_NAME` 與
`SKILLWEAVE_RESERVED_CONCURRENCY` 覆寫預設值。Reserved concurrency 預設
為 `0`（不建立 function-level reservation，使用帳號共用 concurrency），以支援
新帳號的最低 quota；提高 account quota 後可設為 `10` 或更高。

等價的逐步指令：

```bash
sam validate --lint --template-file infra/template.yaml
sam build --template-file infra/template.yaml
sam deploy \
  --stack-name skillweave-demo \
  --region us-east-1 \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides StageName=prod ReservedConcurrency=0
```

取得網址：

```bash
aws cloudformation describe-stacks \
  --stack-name skillweave-demo \
  --region us-east-1 \
  --query 'Stacks[0].Outputs' \
  --output table
```

目前已驗證的 public judge URL：

`https://38r6a90fb3.execute-api.us-east-1.amazonaws.com/prod/`

GitHub 與影片完成後只接受 public HTTPS URL：

```bash
python3 scripts/update_release_urls.py \
  --github-url "https://github.com/ORG/REPO/releases/tag/TAG" \
  --demo-video-url "https://VIDEO_HOST/VIDEO_ID"
python3 scripts/verify_release.py
```

`update_release_urls.py` 會同步重建 submission audit 與它在 manifest 內的
SHA-256，避免 URL 已完成但 audit 仍顯示舊 blocker。

## Public GitHub release + video

確認原始競賽 CSV 與 PDF 沒有被 Git tracked 後，以選定的 `OWNER/REPO` 發布：

```bash
gh repo create OWNER/REPO --public --source=. --remote=origin --push
git push origin skillweave-2026.07.28-rc6
gh release create skillweave-2026.07.28-rc6 \
  dist/skillweave-demo-5min.mp4 \
  --repo OWNER/REPO \
  --title "SkillWeave RC6" \
  --notes "Verified AWS hackathon judge release."
```

GitHub release asset 同時作為公開影片 URL：

```bash
python3 scripts/update_release_urls.py \
  --github-url "https://github.com/OWNER/REPO/releases/tag/skillweave-2026.07.28-rc6" \
  --demo-video-url "https://github.com/OWNER/REPO/releases/download/skillweave-2026.07.28-rc6/skillweave-demo-5min.mp4"
```

接著執行 AWS deploy。三個 URL 都通過 clean-session smoke 後，將更新後的
manifest、audit 與 verifier report commit 並 push 到 `main`。

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

可重現的 bounded production smoke：

```bash
python3 scripts/run_aws_production_smoke.py --requests 30 --concurrency 5
```

它以無 AWS session 的 public HTTPS client 檢查 UI、relative assets、health、
metadata、graph on/off、trace provenance，以及 30 個 concurrency-5 搜尋請求；
結果與 latency 分位數保存於 `reports/aws-production-smoke.json`。

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
