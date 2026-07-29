# SkillWeave

> 把職缺文字編成可查詢的技能關係，讓求職者看到的第一個結果更接近真正想找的工作。

SkillWeave 是針對「2026 雲湧智生：臺灣生成式 AI 應用黑客松」1111 人力銀行題目打造的可解釋職缺搜尋系統。它同時提供：

- 符合工作坊合約的 `POST /api/v1/jobs/search`
- 可互動的求職者 Live Demo
- Query → Skill → Job 的圖譜遍歷 trace
- 嚴格時間 cutoff 與 cold-start 降級路徑
- 可一鍵執行的「有圖譜 vs 無圖譜」雙重指標 ablation
- AWS production architecture 與 Bedrock 結構化萃取規格

目前狀態必須說清楚：public AWS judge demo、雙合約 API、train-only graph
pipeline 與 XGBoost Unbiased LambdaMART 都已可執行。凍結後的最終模型在
**1,991 筆完全未參與調參的 confirmation queries** 上得到
**NDCG@10 +5.72%、MRR +6.45%、Hit@1 +9.35%**；paired NDCG 95% CI 為
`[+0.01491, +0.03607]`。同一模型再套用第二個互斥的 1,992-query bucket，
NDCG 仍為 **+5.07%**、CI `[+0.01218, +0.03272]`。兩次都通過 `≥5%`
theme gate。repo 同時保留歷史失敗 holdout 與一個未滿 5% 的候選模型，不隱藏
負面實驗；完整結果見
[`reports/ltr-quality-confirmation.json`](reports/ltr-quality-confirmation.json)、
[`reports/ltr-quality-replication.json`](reports/ltr-quality-replication.json)
與 [`reports/README.md`](reports/README.md)。

GenAI 也不是紙上架構：真實 Amazon Bedrock Claude Haiku 4.5 已對 200 筆
train-only 職缺執行 strict structured extraction。Evidence validator 發布
180 筆、1,598 個 grounded mentions，隔離 20 筆並拒絕未達規則的內容；
實際 464,061 tokens、估算 US$1.06。這是有界 pilot，不冒充完整 corpus graph。
Aggregate-only 證據見
[`reports/bedrock-pilot.json`](reports/bedrock-pilot.json)。

Public demo：<https://38r6a90fb3.execute-api.us-east-1.amazonaws.com/prod/>。無 AWS
session 的 production smoke 已驗證 UI、assets、API、graph on/off 與 trace；
30-request／concurrency-5 結果為 30/30 HTTP 200、p95 4.40 秒，詳見
[`reports/aws-production-smoke.json`](reports/aws-production-smoke.json)。

## 一分鐘啟動

需求：Python 3.11+。本機 demo runtime 只使用 Python 標準函式庫。

```bash
python3 scripts/build_demo_index.py
python3 -m app.server --port 8080
```

開啟 <http://127.0.0.1:8080>。

快速驗證：

```bash
python3 -m unittest discover -s tests -v
curl -sS -X POST http://127.0.0.1:8080/api/v1/jobs/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"後端工程師 Node.js","location_code":["100100"]}'
```

## API contract

工作坊簡報與原命題文件使用了兩組欄位名稱。服務同時接受兩組，避免評測整合時因版本差異失分。

正式工作坊格式：

```json
{
  "query": "後端工程師",
  "location_code": ["100100"],
  "duty_code": ["140200"]
}
```

原命題相容格式：

```json
{
  "ks": "後端工程師",
  "c0": "100100",
  "d0": "140200"
}
```

回應保留工作坊必要欄位，另加相容的 `empStr` 與可解釋資訊：

```json
{
  "request_id": "req_...",
  "result": [
    {
      "job_id": "130280356",
      "rank": 1,
      "title": "NodeJS資深後端工程師",
      "matched_skills": ["Node.js", "後端工程師"],
      "graph_trace": []
    }
  ],
  "empStr": "130280356,..."
}
```

保證：

- `rank` 從 1 連續
- `job_id` 與 `rank` 不重複
- 預設回傳 Top 20，最多 Top 100
- 無結果回傳 `result: []`
- 空 query 回傳明確 `400`
- 不依賴 `talentNo` 或其他正式 payload 不提供的個資欄位

完整 machine-readable schema：[`docs/openapi.yaml`](docs/openapi.yaml)。

## 真實資料概況

本 repo 已串讀六份提供資料，而非使用虛構職缺：

| 資料 | 實際列數 | 用途 |
|---|---:|---|
| 職缺主檔 | 1,218,635 | JD、條件、分類、地點、薪資、圖譜來源 |
| 搜尋紀錄 | 6,139,952 | query、條件、既有曝光候選 |
| 職缺瀏覽 | 8,241,233 | 弱正向訊號 |
| 主動應徵 | 225,999 | 強正向訊號 |
| 城市對照 | — | `c0/location_code` 解碼 |
| 職務對照 | — | `d0/duty_code` 與相似職稱 |

重要品質發現：

- `talentNo=0` 佔搜尋約 22.1%，一律排除跨事件使用者串接。
- 251,258 筆 JD 的最後修改時間晚於本地 graph cutoff `2026-06-05 23:59:59.999`，不得用來建立 train graph。
- `電腦技能資料` 缺值 961,442 筆，`工作技能` 缺值 1,067,372 筆，不能只依賴結構化技能欄位。
- 資料沒有工作坊原命題曾提及的 `session_id`；本地 qrels 使用「搜尋後 30 分鐘」的單向事件視窗，並在 artifact metadata 揭露這項近似。
- 未互動不是負例；production trainer 使用曝光 propensity 做 IPS clipping，評估指標本身不做 IPS。

詳細 data card：[`docs/data-card.md`](docs/data-card.md)。

## 排序設計

```text
query + c0 + d0
      │
      ├─ 意圖正規化（中英別名、OOV fallback）
      ├─ OpenSearch hybrid retrieval（BM25 + vector）
      ├─ Neptune 1-hop traversal / aggregation
      └─ IPS-aware LambdaMART rerank
                       │
                    Top 20
```

本機 demo 以 12,000 筆真實職缺的精簡 artifact 模擬相同 feature contract；production 以 OpenSearch 取代逐筆掃描。

主要特徵族：

1. 文字：職稱 phrase、分類、JD、subword overlap。
2. 圖譜：canonical skill 直接命中、帶權 `RELATED_TO` 一跳聚合、edge confidence。
3. 條件：城市與職務代碼；目前採強 soft constraint，保留降級可能。
4. 行為：僅用 train window 的 view/apply aggregate。
5. 新鮮度與 cold-start flag。

離線 LTR 另使用嚴格 train-only 的 `Query → Skill/Job` 行為邊。每個 train-day row 只讀更早日期的 rolling snapshot，避免把自己的 label target-encode 回特徵；validation/test 才讀凍結的 06-01～06-05 graph。最終線上規則只有在候選群中存在歷史 `Query → Job` edge 時啟用 graph model，否則自動退回 graph-off ranking。

圖譜不是自由生成答案。每條 `REQUIRES` edge 必須保存原文 evidence span、模型版本、prompt version、confidence、source timestamp 與 validation flags；無證據的邊不進 production graph。

Graph schema、查詢與真實 trace：[`docs/graph-schema.md`](docs/graph-schema.md)。

## 時間切分與洩漏防護

本地開發切分：

| Split | 日期 | 可用方式 |
|---|---|---|
| Train / graph cutoff | 06-01～06-05 | 建圖、特徵、模型訓練 |
| Validation | 06-06、hash bucket `[0,200)` | 模型選擇 |
| Holdout 1 | 06-07、bucket `[200,400)` | 失敗實驗，完整保留 |
| Confirmation | 06-07、bucket `[400,1400)` | gate/model 鎖定後一次性確認 |

核心規則：

- `職缺最後修改時間 > train cutoff` 的職缺不可由目前 JD 建圖。若該職缺在 cutoff 前的應徵事件保存了 `empName`，benchmark 只可使用當時的職稱快照；否則走 `skills=[]` cold-start。
- view/apply label 只允許發生在 query 之後，避免把過去行為誤歸因給未來搜尋。
- train 行為圖採 strictly-earlier-day snapshots，杜絕 label 回灌。
- benchmark 以同一 `(query, location, duty, job)` 最大 grade 聚合：view=1、apply=2。
- 所有 artifact 都保存 `schema_fingerprint`、cutoff、seed、builder/model version。

## 一鍵 ablation

首次執行先建立獨立的 LTR environment：

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements-ltr.lock
```

macOS 若 XGBoost 回報缺少 OpenMP，另安裝 `libomp`。接著：

```bash
make quality
```

流程會：

1. 以固定 hash seed 建立 train、validation 與 disjoint confirmation buckets。
2. 只連結搜尋之後 30 分鐘內的 view/apply。
3. 聚合同一 `(query, condition, job)` 的最大 grade。
4. 建立 judged-candidate benchmark index。
5. 以 XGBoost Unbiased LambdaMART 訓練固定 seed 的 LTR。
6. 固定同一個模型，graph-off 時只把 graph feature family 歸零，避免模型容量混淆。
7. 凍結模型後，依序評估兩個互斥、未用於選模的 confirmation buckets。
8. 輸出 NDCG@10、MRR、Hit@1、Hit@10、Precision@10 與 paired bootstrap CI。

輸出：

- `reports/ltr-quality-confirmation.json`
- `reports/ltr-quality-replication.json`
- `reports/ltr-quality-component-ablation.json`
- `reports/verify-quality-release.json`

各報告的 release／development 定位見 [`reports/README.md`](reports/README.md)。

主要鎖定 confirmation 結果（1,991 queries）：

| 指標 | 無圖譜 | 有圖譜 | 相對變化 |
|---|---:|---:|---:|
| NDCG@10 | 0.4494 | 0.4751 | **+5.72%** |
| MRR | 0.4349 | 0.4629 | **+6.45%** |
| Hit@1 | 0.2793 | 0.3054 | **+9.35%** |
| Hit@10 | 0.8267 | 0.8609 | **+4.13%** |
| Precision@10 | 0.1636 | 0.1729 | **+5.71%** |

NDCG paired delta 為 `+0.02570`，95% CI `[+0.01491, +0.03607]`。第二個
完全互斥 confirmation 的 NDCG 相對 lift 為 `+5.07%`，paired CI
`[+0.01218, +0.03272]`。兩者使用同一個 frozen model；驗證器也會檢查
bucket 不重疊、模型簽章相同、query 數、5% gate 與 CI。

Position bias 狀態：

- Heuristic UI smoke benchmark：`position_bias_correction=false`
- 正式 LTR ablation：`position_bias_correction=true`（XGBoost `lambdarank_unbiased=true`、query-grouped training、top-k pair construction；SageMaker framework image target `3.0-5`）

## Amazon Bedrock 生成式 AI 模組

Bedrock 的必要工作不是替結果寫文案，而是把非結構化 JD 變成可驗證的結構：

```json
{
  "mentions": [
    {
      "surface": "React.js",
      "canonical_skill": "React",
      "type": "framework",
      "level": "required",
      "evidence": "熟悉 React.js 前端開發",
      "confidence": 0.97
    }
  ],
  "relations": [
    {
      "source": "React",
      "type": "RELATED_TO",
      "target": "JavaScript",
      "confidence": 0.91
    }
  ]
}
```

輸出須通過 JSON schema、evidence substring、allowlisted edge types、alias collision、跨語縮寫與 confidence gate。失敗模式與防護：[`docs/genai-safety.md`](docs/genai-safety.md)。

## AWS 部署

Production path：

- S3 Versioning / Object Lock：raw data、model、index manifest
- Glue + Athena：schema 與 provenance
- Step Functions：temporal gate → Bedrock batch extraction → validation → index/graph publish
- Amazon Bedrock：JD skill extraction、alias normalization、OOV query fallback
- Amazon OpenSearch Service：BM25 + vector hybrid candidates
- Amazon Neptune：weighted skill graph traversal
- SageMaker：IPS-aware LambdaMART endpoint
- ECS Fargate：online orchestration API
- API Gateway + WAF：public judge endpoint
- CloudWatch + X-Ray：latency、OOV、empty result、feature drift、trace

架構圖與 failure/fallback path：[`docs/aws-architecture.md`](docs/aws-architecture.md)。

Compact AWS judge/demo 的 SAM runbook：[`docs/deployment.md`](docs/deployment.md)；5 分鐘錄影腳本：[`docs/demo-script.md`](docs/demo-script.md)。

Compact judge path 已實際部署為 API Gateway HTTP API + Lambda Python 3.13
arm64 + CloudWatch，公開 URL 與 production smoke 均已登錄。這不代表上方完整
OpenSearch／Neptune／SageMaker production path 已部署；兩者不可混為一談。
部署與驗證步驟見 [`docs/deployment.md`](docs/deployment.md)。

## 版本與重現資訊

- Random seed：`1111`
- Dataset：`1111-2026-06-01_2026-06-07`
- Schema fingerprint：`105f60c88cdef8a3`
- Demo index：`demo-2026.06.05-v1`
- Benchmark index：`benchmark-2026.06.05-v1`
- Graph schema：`skillgraph-v1`
- Bootstrap graph builder：`reviewed-bootstrap-fixture`
- Production builder：Amazon Bedrock structured extraction（200-record
  train-only pilot 已產出；full corpus 尚未執行）
- LTR model：XGBoost 3.2.0、40 trees、depth 4、seed 1111
- LTR dependencies：[`requirements-ltr.lock`](requirements-ltr.lock)
- Python runtime：3.11+
- Demo dependencies：Python standard library only
- Immutable artifact hashes／external-deliverable status：[`release-manifest.json`](release-manifest.json)
- Kiro activity／review evidence：[`docs/kiro-evidence.md`](docs/kiro-evidence.md)
- Graph coverage／subgroup guardrails：[`docs/graph-coverage.md`](docs/graph-coverage.md)
- Business case／A/B design：[`docs/business-case.md`](docs/business-case.md)
- Five-minute judge pitch：[`docs/judge-pitch.md`](docs/judge-pitch.md)
- Reproducible five-minute video：[`video/README.md`](video/README.md)
- Rubric-to-artifact evidence index：[`docs/evidence-index.md`](docs/evidence-index.md)
- Copy-ready submission packet：[`docs/submission-packet.md`](docs/submission-packet.md)

原始 CSV 不應提交到公開 GitHub。請保留 `data/dataset/` 在 `.gitignore`，只提供取得方式、schema、fingerprint 與重現指令。

## Repo map

```text
app/                       API 與本機 ranker
web/                       Live Demo
config/                    reviewed bootstrap ontology
scripts/build_demo_index.py
scripts/build_benchmark_fixture.py
scripts/benchmark.py
scripts/run_ablation.sh
scripts/run_quality_confirmation.sh
scripts/run_ltr_ablation.sh
scripts/report_graph_coverage.py
scripts/verify_release.py    release evidence audit
scripts/render_demo_video.py reproducible 5-minute judge video
scripts/build_submission_packet.py evidence-derived form copy
scripts/external_release_preflight.py credentials/tag/privacy preflight
tests/                     contract / leakage / ranking invariants
docs/                      graph、GenAI、AWS、data card、提交稽核
video/                     tracked deck、narration、captions
artifacts/                 可重建的本機 demo / benchmark artifacts
reports/                   真實 ablation 輸出
```

## 測試

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile app/*.py scripts/*.py tests/*.py
python3 scripts/verify_release.py
# 或一次執行 tests + deterministic package + release audit
./scripts/release_gate.sh
```

目前測試涵蓋：短縮寫邊界、連續 rank、job ID 去重、地區條件、Node.js
直接證據、future JD 無 graph edge、空 query、live graph toggle、release
evidence anti-tampering、portable tree inference 與原生 XGBoost parity。Compact
Lambda 先取 100 個 lexical candidates，再用同一個 frozen 40-tree LTR 重排；
title relevance floor 防止 compact scanner 與 organizer candidate source 的
distribution shift。正式指標仍由 locked candidate fixture 計算，不用 Demo
結果回頭改寫 confirmation。

## License / privacy

競賽資料是去識別化資料，仍應依主辦方授權範圍使用。公開 repo 不包含原始 CSV、使用者編號或可回推 session 的 artifact。Demo index 只含職缺內容與彙總互動計數。
