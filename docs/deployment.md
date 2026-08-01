# AWS Demo Deployment Runbook

這份 runbook 包含兩種模式：

- Compact demo：API Gateway + Lambda + 12,000 筆 embedded artifact，適合 UI 與合約 smoke。
- Full-corpus judge：OpenSearch 搜尋全部 1,218,635 筆職缺，再由同一 Lambda LTR 重排。

若評分會以完整職缺集合檢驗，只有第二種模式符合搜尋範圍要求。完整
Neptune/SageMaker production 架構另見 `aws-architecture.md`。

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
- `artifacts/models/ltr-quality-final.trees.json`

不會把 3.8 GB 原始 CSV 上傳。

Portable model 是 frozen XGBoost 的 40 棵樹，不含 XGBoost runtime dependency。
`reports/portable-ltr-parity.json` 在 40,218 rows 上驗證純 Python 與原生
XGBoost 的 centered score 最大誤差為 `1.08e-7`。

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

## Full-corpus judge deployment

Compact Lambda 內仍保留 12,000 筆作服務降級，不再把它當成正式評測全集。
完整流程為：

```text
1,218,635-row 職缺.csv
  → index_full_opensearch.py
  → OpenSearch Serverless skillweave-jobs-v1
  → query Top 200
  → Lambda graph/LTR rerank
  → API Top 20
```

先確認既有 compact stack 存在，並準備具 OpenSearch Serverless 與
CloudFormation 權限的 IAM role。部署會建立計費資源，必須顯式 opt-in：

```bash
uv pip install --python .venv/bin/python -r requirements-production.lock
export AWS_REGION=us-east-1
export SKILLWEAVE_ENABLE_PAID_FULL_INDEX=yes
export SKILLWEAVE_INGESTION_PRINCIPAL_ARN=arn:aws:iam::ACCOUNT:role/ROLE
./scripts/deploy_full_search_aws.sh
```

腳本依序：

1. 解析現有 Lambda runtime role。
2. 建立最低 OCU=0 的 NextGen collection group、security policies 與 SEARCH collection。
3. 將 `職缺.csv` 每一列匯入 OpenSearch；有沒有技能命中都必須匯入。
4. 等待 index refresh，使用 `_count` 驗證文件總數。
5. 更新 Lambda 的 endpoint、index 與最小讀取 IAM 權限。
6. 重跑 health/search smoke。

OpenSearch Serverless endpoint 可從公網到達，但文件 API 仍要求 SigV4 與 data
access policy。正式企業環境應改用 VPC endpoint；黑客松版本採這個設定是為了讓
本機匯入器與 Lambda 共用同一 collection。

NextGen group 在所有 collection 閒置 10 分鐘後可將 indexing/search workers
降至 0 OCU；第一次喚醒可能增加 10～30 秒延遲。評審期間應使用有界 keep-warm，
評審結束後停止，不能讓第一個 judge request 落入 12,000-job fallback。

驗收：

```bash
curl -sS "$DEMO_URL/api/v1/meta"
curl -sS -X POST "$DEMO_URL/api/v1/jobs/search" \
  -H "content-type: application/json" \
  -d '{"query":"行政助理","top_k":20}'
```

`/api/v1/meta` 必須回傳：

```json
{"search_scope":"full_corpus_opensearch"}
```

搜尋回應的 `meta.candidate_source` 必須是 `opensearch_full_corpus`，且
`meta.degraded_components` 必須為空。若看到 `embedded_12000_fallback`，
代表 API 合約仍可用，但不具全量評測資格。

## Local full-corpus OpenSearch

AWS OpenSearch Serverless 本身不能在本機執行；本機使用單節點 open-source
OpenSearch，驗證相同的 mapping、bulk、CJK 搜尋與 rerank API。它不模擬 IAM、
OCU、自動擴縮或 AWS data access policy。

Docker Desktop 至少分配 4 GB 記憶體；對 121 萬筆完整 corpus，建議分配更多
記憶體與足夠磁碟空間。啟動服務：

```bash
make opensearch-local-up
curl http://127.0.0.1:9200
```

第一次建立全量索引：

```bash
make full-index-local
```

這會讀完 `data/dataset/職缺.csv`，可能需要一段時間。完成時報告必須顯示：

```json
{
  "source_jobs_indexed": 1218635,
  "verified_index_count": 1218635,
  "all_source_jobs_are_search_targets": true
}
```

啟動全量本機 API：

```bash
make full-demo-local
```

驗證：

```bash
curl -sS http://127.0.0.1:8080/api/v1/meta
curl -sS -X POST http://127.0.0.1:8080/api/v1/jobs/search \
  -H "content-type: application/json" \
  -d '{"query":"行政助理","top_k":20}'
```

第一個回應應為 `search_scope=full_corpus_opensearch`；第二個回應應為
`candidate_source=opensearch_full_corpus`。停止 container 但保留 index volume：

```bash
make opensearch-local-down
```

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
2. Deterministic exact extraction + evidence/negation validator（零 LLM/embedding request）。
3. OpenSearch hybrid index。
4. Neptune graph import/traversal。
5. SageMaker Unbiased LambdaMART endpoint。
6. Fargate orchestrator 替換 Lambda scan。
7. 保留相同 API Gateway route 與 response contract。

## Rollback

不要刪 stack 才回滾。先把 Lambda alias 指回上一個經驗證版本，或重新部署上一個 release package。資料／index／model manifest 必須一起回退。
