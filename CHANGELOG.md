# CHANGELOG

兩部分：**Part A** 是這個 branch 的設計與改善（做了什麼、為什麼、量到什麼），
**Part B** 是合併進 `main` 時的整合決定與取捨。

---

## Part A — 本分支的設計與改善

基準：`main` @ `0a733f5`（Deploy provisioned OpenSearch judge environment）

### 1. Query 正規化改成批次結構化輸出

**問題**：原本每個 request 打一次 Bedrock，但對真實 query 幾乎沒有語意作用——
`現領` / `親子` / `萊爾富` / `青埔` / `二度就業` 實測全部原樣輸出，唯一有效的
`nodejs → Node.js` 早就由 `app/ranker.normalize()` 的 regex 免費處理。代價是每個
request 多 0.9–7.5s，且 `connect_timeout=0.5` / `read_timeout=2.0` 保證每次都
timeout + retry，等於每個 request 燒掉兩格 quota。

**改動**：

- Bedrock 限制是「每分鐘請求數」，與單次帶幾筆查詢無關。新增 `BatchCoalescer`：
  **湊滿 10 筆或等滿批次視窗**（先到為準）合併成一次請求。視窗長度依部署形狀
  而異，見 Part B3。
- 輸出改為結構化 JSON，約束在封閉字彙：`職務對照表.csv` 的 690 個職務小類、
  `城市對照表.csv` 的縣市、`職缺屬性` / `工時` / 薪資型態。
- Python 端重新驗證每個值。無法對應的職類、有歧義的行政區、與自身行政區矛盾的
  縣市，一律降級為全文比對詞，**絕不成為檢索過濾條件**（錯誤的過濾比錯誤的關鍵字
  代價高得多）。
- 薪資型態必須在查詢字面上出現才採用。`現領` 原本被推成 `daily`，但實際應徵資料
  顯示 73% 是月薪全職。
- 計分字串只保留使用者自己的字。把職務小類直接混入會稀釋 `exact_title` /
  `title_phrase`，實測讓 `青埔` / `診所` / `親子` 的 top-3 退步，因此改為獨立的
  **taxonomy 展開，僅在第一輪候選不足時才啟動第二輪救援**。
- 新增 in-process LRU 與非阻塞降級：逾時立刻回傳 deterministic 解讀，批次仍會完成
  並寫回 cache。
- deterministic 路徑升級為封閉字彙掃描（4,674 個職務別名 + 1,049 個地名），採
  最長匹配滑動視窗，不用 5,700 分支的 regex。

**實測**（Claude Haiku 4.5）：10 筆查詢 → 1 次請求 / 9.1s；重複查詢 13–24 µs；
deterministic 路徑 32–69 µs。

**新增檔案**：`scripts/build_query_vocab.py`、`config/query-intent-prompt.txt`、
`config/query-intent-vocab.json`、`scripts/build_top_queries.py`、
`scripts/validate_query_normalization.py`

---

### 2. 預算 query intents（Lambda 專用）

**問題**：批次與 pre-warm 都假設一個進程服務多個 request。Lambda 一次 invocation
只服務一個，container 之間還會凍結——coalescer 永遠湊不到同批夥伴，LRU 永遠累積
不起來。結構化正規化在 serverless 路徑上等於不存在。

**改動**：

- `scripts/build_query_intents.py` 離線算好頭部查詢的 intent，打包進 Lambda zip。
  載入後放在 LRU **之外**，執行期的 miss 無法將其淘汰。
- 修正 cache 查詢順序：原本在 `enabled` 判斷之後，導致 Bedrock 未設定時完全不查
  預算資料——而那正是預算資料最有價值的時候。
- `scripts/package_lambda.py` 補打包 `config/`。少了它，部署後正規化會**靜默**
  退回舊的字串行為。

**產出**：top 2,000 查詢（涵蓋 61% 搜尋量）中 1,991 筆，271 秒 / 199 次 Bedrock
請求 / 528 KiB。

---

### 3. 索引補結構化欄位與真實行為數據

三個缺陷都在 `scripts/index_full_opensearch.py`：

- **`職缺屬性` / `工時` / `學歷需求` / `工作經驗需求` 沒有被索引。**
  `現領` 單一 query 就佔全站搜尋 12.8%（102,716 / 800,000），但只出現在 0.03% 的
  職缺文字中；它產生的應徵 73% 是全職、集中在六個職務小類。屬性類查詢
  （現領 / 正職 / 兼職 / 工讀生 / 二度就業 / 晚班 / 暑期）遠超 15% 流量，答案就在
  這些結構化欄位裡。
- **`view_count` / `apply_count` 寫死 0**，等於關掉全語料的 `behavior` 排序特徵，
  而 824 萬筆瀏覽與 22.6 萬筆應徵就躺在資料集裡沒用。新增
  `scripts/build_job_behavior.py`（286,632 個職缺有行為數據，10.8 秒）。
- **生產索引誤用了 graph `TRAIN_CUTOFF`**，導致 24% 職缺完全沒有技能標註。該
  cutoff 是為了離線 ablation 防洩漏，線上路徑沒有這個問題。`graph_eligible` /
  `post_cutoff_jd` 仍保留作為 provenance。**技能覆蓋率 65.8% → 91.4%**（線上全量實測；
本機 5 萬筆抽樣時估為 87.2%）。

`--update-mapping` 可對已部署的 provisioned index 附加新欄位，搭配 `--skip-create`
以 `_id` 就地覆寫全部文件，不需刪除索引或切換 alias。

**線上索引已完成升級**，1,218,635 筆全部改寫，因兩次網路中斷分三段執行
（`--skip-records` 續傳），全程索引持續可查詢：

| 段次 | 寫入 | 耗時 | 速率 |
| --- | ---: | ---: | ---: |
| 1 | 456,250 | 44 分 | 172 jobs/s |
| 2 | 443,750 | 60 分 | 124 jobs/s |
| 3 | 331,135 | 35 分 | 158 jobs/s |
| **合計** | **1,231,135**（含續傳重疊 12,500） | **139 分** | **平均 148 jobs/s** |

| 欄位 | 升級後 | 升級前 |
| --- | ---: | ---: |
| `employment_type` / `education` / `experience` / `salary_type` | 100.0% | 0% |
| `shifts` | 99.5% | 0% |
| `skills` | **91.4%** | 65.8% |
| `view_count > 0` | 23.2% | 0% |
| `apply_count > 0` | 8.0% | 0% |
| post-cutoff 職缺且有技能 | 18.9% | 0% |

---

### 4. 檢索條件下推

`c0` 出現在 68.8% 的請求、`d0` 出現在 43.4%。原本地區只是 `should` boost，
但 ranker 對縣市不符給 -16 分，再把 `score <= 0` 的候選全部丟棄——等於把 BM25
撈回來的整頁結果砍光。

- 明確的 `c0` 地區改為 **filter**；過濾後若無結果則退回不過濾，不會丟給評審空白頁。
- 正規化「推導」出來的條件一律維持 boost，推錯不會清空結果。
- `duty_categories` / `employment_types` / `shifts` / `salary_type` / `company`
  加入 should-clause。

---

### 5. 全量索引的可靠性與速度

- **重試**：長時間任務會遇到間歇性連線中斷，原本一次中斷就毀掉整輪。改為指數退避
  重試，每次重新簽章（SigV4 涵蓋時間戳）。HTTP 狀態錯誤仍立即失敗。
- **並行**：原本一次送一個 bulk，整個往返期間閒置。改為受限執行緒池並行送出，
  semaphore 限制在途 payload 數量。3 萬筆 benchmark 上 **72 → 222 jobs/s**；
  全量實跑受網路狀況影響，持續速率 124–172 jobs/s。
- **batch size 400 → 250**：大 payload 才是連線中斷的真因（500 幾乎每次請求都失敗，
  250 完全沒有）。重試邏輯原本只是在掩蓋它。
- **`--skip-records` 續傳**：中斷後可從斷點接續，不必從頭重跑。
- 修正結束時的計數檢查：`--skip-create` / `--max-records` 是就地更新，原本要求
  「文件總數 == 本輪寫入數」會讓每次正確完成的升級都被判定失敗。

---

### 6. 與現有部署對齊

- `app/retrieval.py` 改為讀取 `OPENSEARCH_SERVICE`。`infra/template.yaml` 從
  provisioned 部署起就傳了這個變數，但 retriever 一直從 endpoint 主機名推導 SigV4
  service。**簽錯 service 產生的 403 與權限問題完全無法區分**。
- `scripts/build_top_queries.py` 優先讀 `userSearchLog_cleaned.csv`，與其他 pipeline
  腳本一致，避免把 Bedrock 請求花在 SEO spam 上。
- `BEDROCK_QUERY_MAX_TOKENS` 128 → 4000。128 會把整批回應截斷。

---

### 7. 部署方式：`scripts/deploy_lambda_code.sh`

**不使用 `sam deploy`。** 現行 judge stack 是從未 push 的分支部署的，帶有本 repo
template 未定義的參數（`SkillAliasIndex` / `GraphVersion` / `NeptuneGraphId` /
`NeptuneGraphRegion`）。從這裡跑 stack update 會靜默移除它們。這些功能目前是失效的
（skill-alias 索引回 404、Neptune ID 為空字串），但移除不屬於本 repo 的設定不該由
這個變更決定。

腳本只更新 function code 並**合併**正規化相關環境變數——`--environment` 會覆寫
整份變數表，未合併的呼叫會抹掉 judge 路徑依賴的 OpenSearch 設定。同時拒絕部署
缺少 `config/` 的 bundle。

---

## Part B — 合併進 `main` 的整合決定

合併基準：`main` @ `467ed2e`（hybrid retrieval、deterministic graph pipeline、
production deploy）。

### B1. 為什麼 `app/` 三個檔案沿用 main 版本

`main` 先前以**檔案複製**方式吸收了本分支的 `query_normalizer.py` / `ranker.py` /
`retrieval.py`（非 merge，故本分支 commit 不在其歷史中），並在其上加了他們的
hybrid kNN、graph provider 與 release-intent path。那三個檔案 `main` 已是超集，
故直接沿用——已逐項確認 `BatchCoalescer`、`load_intents`、`_grounded_salary_type`、
`_collect_stage_one`、`recall_expansion`、`_intent_boosts` 全數保留。

### B2. `scripts/index_full_opensearch.py` 是唯一需要真正合併的檔案

`main` 的版本不含本分支任何改動。**用它重跑會把線上索引現有的 8 個結構化欄位、
行為數據與 post-cutoff 技能全部清掉。** 以本分支版本為底，移植 `main` 的三項：

- `AliasMatcher` 取代 regex alternation（`finditer()` / `group(0)` 介面相容，是
  drop-in；automaton 才撐得住 ontology 擴張）
- `knn_vector` mapping 與 `index.knn` 設定
- `--embed`，**維持 opt-in**：embedding 是同步逐筆 Bedrock 呼叫，開啟會主宰索引時間

### B3. 批次視窗改為依部署形狀分開

`main` 把 `max_wait_seconds` 預設從 `1.0` 改成 `0.05`。在 Lambda 上這是對的——
一次 invocation 只服務一個 request，沒有可合併的同批查詢，長視窗只是純延遲。
但那個值同時被長駐進程吃到，而**長駐進程才是批次真正有效的地方**，0.05 秒視窗
等於把 Part A 第 1 節「10 筆併發合併成 1 次請求」的設計取消掉。

| 部署形狀 | `MAX_WAIT_SECONDS` | 理由 |
| --- | --- | --- |
| library 預設 / `infra/template.yaml`（Lambda） | `0.05` | 沒有同批夥伴，等待只是延遲 |
| `app/server.py`（長駐 / ECS Fargate） | `1.0` | 併發請求可合併，這是批次設計的目標形狀 |

`app/server.py` 用 `os.environ.setdefault`，明確設定的環境變數仍然優先。

**`deadline_seconds` 採用 main 的 `6.0`（本分支原為 2.5）。** 這是 main 修正了本
分支的錯誤：實測單筆 Bedrock 呼叫約 2.1 秒，端到端驗證中 `時薪210` 花了 4.6 秒——
2.5 秒會讓大量本來來得及的查詢被降級成 deterministic。

### B4. 補上 main 缺少的建置腳本

`main` ship 了 `config/query-intent-vocab.json`，卻沒有產生它的腳本——無法重建。
本次補齊 `build_query_vocab.py`、`build_top_queries.py`、`build_job_behavior.py`、
`build_query_intents.py`、`validate_query_normalization.py`、
`deploy_lambda_code.sh`，並加入 `make query-artifacts`。

### B5. 合併副作用修正

- `scripts/package_lambda.py`：兩邊的 config 清單疊加，造成 zip 有 3 個重複項目
  （多 800 KB，`unzip` 會跳覆寫提示）。改為 required / optional 兩組並去重，同時
  保留 main 的 `query-intents-release.json`。
- `infra/template.yaml`：保留 main 的 `NEPTUNE_*` / `GRAPH_*` / `SKILL_ALIAS_INDEX`，
  補上 `QUERY_INTENTS_PATH` / `QUERY_VOCAB_PATH`。

### B6. 合併後的驗證

148 tests 全過；`app/` `scripts/` `tests/` 通過 `py_compile`；6 支建置腳本可執行；
索引腳本 dry-run 正常。Lambda bundle 解壓後直接 invoke，打線上 provisioned domain：

```text
precomputed intents 1,996 筆載入
現領      n=20  amazon_bedrock_cached   opensearch_full_corpus
萊爾富     n=20  amazon_bedrock_cached   duty=[門市／店員／專櫃人員, 店長／門市管理人員]
時薪210    n=20  amazon_bedrock          ← 即時呼叫 4.6s，在 6.0s deadline 內完成
遠端工作    n=20  duty=[軟體工程師, 網站程式設計師]   ← main 的 remote 特徵
行政 +c0=100100  結果全為台北市            ← 本分支的地區 filter 生效
```

---

## 資料檔案不入版控

主辦方資料衍生物，依 repo 既有政策排除，用 `make query-artifacts` 與
`scripts/build_query_intents.py` 重建：

- `config/top-queries.json`（2,000 筆真實使用者搜尋字串）
- `config/query-intents.json`
- `artifacts/job-behavior.json`

---

## 已知未解事項

- `retrieval_reciprocal_rank` 訓練時取自曝光排名（現行 production 排序器的答案，
  屬洩漏型特徵），線上變成 BM25 排名——同一特徵在訓練與推論時語意不同。
- 訓練候選集為曝光清單 top-100（正解必在其中），線上需從 122 萬筆檢索。離線
  +5.72% 不保證轉移。
- `behavior_query_job_*` / `behavior_query_skill_*` 共 8 個特徵以 query 字串為 key，
  對評審的保密查詢恆為 0。改以職務小類聚合可救回，但需重訓。
- `graph_novelty_threshold` 訓練 1.0 / 線上 10.0 不一致（僅影響 stage-one 候選篩選，
  該特徵不在 37 個模型特徵內）。
- kNN hybrid retrieval 目前無法在線上啟用：`embedding` 欄位不存在，且 `index.knn`
  是 static setting，不能對開啟中的索引修改；啟用需另建索引重灌，並為 122 萬筆
  產生 embedding（Titan 無原生批次，`job_document` 為同步逐筆呼叫）。`--embed`
  維持 opt-in。

前三項需重新訓練 LTR，本次未處理。
