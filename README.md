# SkillWeave

> 把職缺文字編成可查詢的技能關係，讓求職者看到的第一個結果更接近真正想找的工作。

SkillWeave 是針對「2026 雲湧智生：臺灣生成式 AI 應用黑客松」1111 人力銀行題目打造的可解釋職缺搜尋系統。它同時提供：

- 符合工作坊合約的 `POST /api/v1/jobs/search`
- 可互動的求職者 Live Demo
- Query → Skill → Job 的圖譜遍歷 trace
- 嚴格時間 cutoff 與 cold-start 降級路徑
- 可一鍵執行的「有圖譜 vs 無圖譜」雙重指標 ablation
- AWS production architecture 與 Bedrock 結構化萃取規格

目前狀態必須說清楚：本機 demo、雙合約 API、train-only graph pipeline 與 XGBoost Unbiased LambdaMART 都已可執行。最終鎖定的 confidence-gated graph 在 **1,993 筆未參與調參的 disjoint confirmation queries** 上得到 **NDCG@10 +1.34%、MRR +1.72%、Hit@1 +2.70%**；paired NDCG 95% CI 為 `[+0.00226, +0.00905]`，排除 0。這是顯著正向結果，但仍未達題目建議的 `≥5%`。repo 同時保留一個失敗 holdout，不隱藏負面實驗；完整結果見 [`reports/ltr-ablation-test.json`](reports/ltr-ablation-test.json) 與 [`reports/ltr-ablation-holdout-1-failed.json`](reports/ltr-ablation-holdout-1-failed.json)。

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
./scripts/run_ltr_ablation.sh
```

流程會：

1. 以固定 hash seed 建立 train、validation 與 disjoint confirmation buckets。
2. 只連結搜尋之後 30 分鐘內的 view/apply。
3. 聚合同一 `(query, condition, job)` 的最大 grade。
4. 建立 judged-candidate benchmark index。
5. 以 XGBoost Unbiased LambdaMART 訓練固定 seed 的 LTR。
6. 固定同一個模型，graph-off 時只把 graph feature family 歸零，避免模型容量混淆。
7. 套用可解釋的 historical-edge confidence gate。
8. 輸出 NDCG@10、MRR、Hit@1、Hit@10、Precision@10 與 paired bootstrap CI。

輸出：

- `reports/ltr-ablation-validation-gated.json`
- `reports/ltr-ablation-test.json`
- `reports/ltr-ablation-holdout-1-failed.json`

各報告的 release／development 定位見 [`reports/README.md`](reports/README.md)。

鎖定 confirmation 結果（1,993 queries）：

| 指標 | 無圖譜 | 有圖譜 | 相對變化 |
|---|---:|---:|---:|
| NDCG@10 | 0.4168 | 0.4225 | **+1.34%** |
| MRR | 0.4031 | 0.4101 | **+1.72%** |
| Hit@1 | 0.2413 | 0.2479 | **+2.70%** |
| Hit@10 | 0.8149 | 0.8138 | -0.12% |
| Precision@10 | 0.1603 | 0.1625 | **+1.35%** |

NDCG paired delta 為 `+0.00561`，95% CI `[+0.00226, +0.00905]`。改善具統計顯著性，但相對 lift 仍 **不通過 ≥5% theme gate**。第一個未 gate 的 holdout 曾得到負向結果，促使系統加入 abstention；該報告保留在 repo，不能只展示成功的 bucket。

Post-hoc coverage 診斷顯示：confidence gate 實際啟用的 285 queries 上，
NDCG@10 從 0.3434 到 0.3826（相對 `+11.41%`；paired CI
`[+0.01609, +0.06378]`）；其餘 1,708 queries 完全 abstain。這指出主要瓶頸
是可安全使用的 graph coverage，但不取代 locked overall result，也不是新的
release gate。完整 aggregate-only 報告與限制見
[`docs/graph-coverage.md`](docs/graph-coverage.md)。

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

目前 repo 沒有 AWS credentials，也沒有可驗證的 AWS URL，所以交付清單中的「實際 AWS 部署網址」仍是 blocker，不能填假連結。部署前須完成 [`docs/submission-checklist.md`](docs/submission-checklist.md) 的 production gates。

## 版本與重現資訊

- Random seed：`1111`
- Dataset：`1111-2026-06-01_2026-06-07`
- Schema fingerprint：`105f60c88cdef8a3`
- Demo index：`demo-2026.06.05-v1`
- Benchmark index：`benchmark-2026.06.05-v1`
- Graph schema：`skillgraph-v1`
- Bootstrap graph builder：`reviewed-bootstrap-fixture`
- Production builder target：Amazon Bedrock structured extraction（尚未產出）
- LTR model：XGBoost 3.2.0、20 trees、depth 2、seed 1111
- LTR dependencies：[`requirements-ltr.lock`](requirements-ltr.lock)
- Python runtime：3.11+
- Demo dependencies：Python standard library only
- Immutable artifact hashes／external-deliverable status：[`release-manifest.json`](release-manifest.json)
- Kiro activity／review evidence：[`docs/kiro-evidence.md`](docs/kiro-evidence.md)
- Graph coverage／subgroup guardrails：[`docs/graph-coverage.md`](docs/graph-coverage.md)

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
scripts/run_ltr_ablation.sh
scripts/report_graph_coverage.py
scripts/verify_release.py    release evidence audit
tests/                     contract / leakage / ranking invariants
docs/                      graph、GenAI、AWS、data card、提交稽核
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

目前測試涵蓋：短縮寫邊界、連續 rank、job ID 去重、地區條件、Node.js 直接證據、future JD 無 graph edge、空 query。

## License / privacy

競賽資料是去識別化資料，仍應依主辦方授權範圍使用。公開 repo 不包含原始 CSV、使用者編號或可回推 session 的 artifact。Demo index 只含職缺內容與彙總互動計數。
