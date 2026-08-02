# SkillWeave

使用 OpenSearch 進行全量職缺搜尋、技能圖譜與 LambdaMART 重排，讓求職者更快找到真正相關的工作。

**Live demo:** https://m97uj2vc55.execute-api.us-east-1.amazonaws.com/prod/

目前本機與 AWS production 均以 `deterministic-v1-rules-v2-latest` serving；
完整 2026-06-01～2026-06-07 資料集的 1,218,635 筆職缺都在 skill graph 中。
`evaluation-cutoff` artifact 僅保留作無 leakage 的離線評測與回滾證據。

## AWS production 架構

以下是公開 Live demo 的完整流程，不是 `make demo` 的本機流程：

```text
Web UI / API Gateway
  → Lambda
      → Amazon Bedrock：Query normalization
      → OpenSearch：1,218,635 筆職缺候選
      → Neptune Analytics：Skill Graph
      → LambdaMART：Top 20 重排與推薦證據
```

## 本機啟動

Requirement：Python 3.11+、AWS CLI，以及可呼叫 Amazon Bedrock 的 AWS 帳號。

```bash
make setup
aws login
aws sts get-caller-identity
make demo
```

開啟 <http://127.0.0.1:8080>

```text
Web UI
  → 本機 Python server
      → Amazon Bedrock：Query normalization
      → Embedded 12,000-job index
      → Embedded Skill Graph
      → Portable LambdaMART
```

## 測試

```bash
make test       # 全部單元／整合測試
make sam-smoke  # Lambda runtime smoke
make release    # 完整 release gate
```

## 部署到 AWS

```bash
aws login
bash scripts/deploy_lambda_code.sh
```

成功時會輸出公開 URL。首次建立 stack、compact stack、完整 OpenSearch、rollback
與資源清理見 [AWS 部署 runbook](docs/deployment.md)。

## 檢驗 AWS 上線狀態

```bash
.venv/bin/python scripts/verify_app_deployment.py \
  --url https://m97uj2vc55.execute-api.us-east-1.amazonaws.com/prod/ \
  --require-full-corpus --require-neptune \
  --expected-graph-version deterministic-v1-rules-v2-latest
```

## API

主要端點：

- `GET /health`
- `GET /api/v1/meta`
- `POST /api/v1/jobs/search`
- `POST /api/v1/graph/trace`

可參考 [OpenAPI](docs/openapi.yaml)。

## 評測指標

目前部署的 `ltr-quality-remote-salary-intent`，在 2026-06-07 holdout 的 1,991 個
查詢上量測（`reports/ltr-quality-remote-salary-intent-confirmation.json`）：

| 指標 | Graph OFF | Graph ON | 相對改善 |
|---|---:|---:|---:|
| NDCG@10 | 0.4489 | 0.4724 | **+5.24%** |
| MRR | 0.4301 | 0.4637 | **+7.80%** |
| Hit@1 | 0.2732 | 0.3059 | **+11.95%** |

NDCG@10 的 paired bootstrap CI95 為 `[+0.0129, +0.0340]`，不含 0。

這是 Graph ON 對 Graph OFF 的比較，不是對某個外部基準的比較，也不等同於實際轉換率。
Query 正規化的 intent 特徵已納入排序，但**未宣稱帶來整體提升**：與不含這些特徵、
其餘完全相同的模型相比，配對檢定為 `+0.0014`、CI95 `[-0.0026, +0.0054]`，跨過 0。
可測得的效益集中在查詢文字指名地點的情境，而該情境在此 fixture 只佔 5.2%。

可參考 [評估報告索引](reports/README.md)。

## 目錄

```text
app/        API、搜尋與線上 normalization
web/        前端
pipeline/   Graph 與 LTR pipeline
infra/      AWS SAM／OpenSearch／Neptune templates
scripts/    部署、驗證與資料工具
tests/      測試
docs/       runbooks 與設計文件
```

## 文件

- [AWS 部署與 rollback](docs/deployment.md)
- [AWS 架構](docs/aws-architecture.md)
- [Skill Graph schema](docs/graph-schema.md)
