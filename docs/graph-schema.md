# Deterministic Skill Graph Schema

Schema version：`skillgraph-deterministic-v1`

## Production build boundary

```text
1111 職缺 CSV
→ duty code Occupation mapping
→ reviewed ontology longest exact alias matching
→ evidence / requirement / negation validator
→ unknown structured surface review queue
→ full-corpus co-occurrence RELATED_TO
→ evaluation-cutoff + latest immutable artifacts
→ Neptune Analytics
```

正式離線 build 的 manifest 固定為 `extractor=deterministic-v1`、
`model_id=null`、`llm_requests=0`、`embedding_requests=0`。線上 Bedrock Query
normalization 不在此 build boundary 內。

## Node types

| Node | Stable key | Source |
|---|---|---|
| `Skill` | reviewed ontology ID | project-reviewed seed；通過 reuse review 的 iCAP K/S vocabulary |
| `Occupation` | `duty.{CodeNo}` | `職務對照表.csv` 的大／中／小類精確映射 |
| `Job` | `job:{職缺編號}` | 1111 職缺主檔 |

iCAP 只補充 canonical label、人工列出的 alias、K/S type、standard
code/version/source URL；A 態度不匯入，也不會自動去掉「能力」等後綴。未完成
reuse/attribution review 的詞彙只輸出 `icap-vocabulary-candidates.jsonl`，不進 serving
ontology。iCAP 不會替未出現該詞的 Job 推導技能。

未知 surface 只由電腦技能、工作技能、專業證照的官方分隔欄位產生。相同
normalized surface 至少需 3 jobs、2 companies 才進 review candidate queue；所有
頻率另存報表。Candidate 永不寫入 Neptune CSV。

## Edge types

| Edge | From → To | Rule |
|---|---|---|
| `REQUIRES` | Job → Skill | reviewed unique exact alias；evidence 是來源原始 substring |
| `INSTANCE_OF` | Job → Occupation | exact 1111 duty taxonomy mapping |
| `RELATED_TO` | Skill ↔ Skill | 無方向的全量共現統計 |

Production 不產生 `PREREQUISITE_OF` 或 `SPECIALIZATION_OF`。

`REQUIRES` 同時保存 `requirement_level`：`必須／需具備／熟悉／精通` 為
`required`，`加分／尤佳／優先` 為 `preferred`，否定語境排除，其餘為
`mentioned`。欄位 confidence 順序為 structured skill fields、職稱、分類、職務內容。

`RELATED_TO` gate：共同 jobs ≥20、companies ≥5、lift ≥2.0、NPMI ≥0.15、
candidate Top 20、published degree cap 20。其 weight 與 confidence 相同：

```text
0.6 × normalized NPMI
+ 0.3 × normalized lift
+ 0.1 × min(support_jobs / 100, 1)
```

每條 relation edge 保存 support jobs/companies、最多三筆兩端都能回指原文的 JD
evidence、`rules_version` 與 `corpus_hash`。

## Temporal artifacts

- `evaluation-cutoff`：只含 `2026-06-05 23:59:59.999` 以前的 Job edges，是
  hackathon evaluation 與 API 預設 graph。
- `latest`：可含 cutoff 後資料，使用獨立 immutable manifest。
- 每個輸入必須進 accepted 或 quarantine；cutoff 後的有效資料另計為
  `post_cutoff_excluded`，不可偷偷進 cutoff graph。

真實 inventory scan 已確認 1,218,635 筆輸入，其中 cutoff eligible 967,377、
post-cutoff 251,258、無無效時間戳。完整 graph 發布仍需通過 referential integrity、
ranking non-regression、至少一項主要 NDCG 正 lift、API/degraded smoke 與 p95 `<800 ms`。

## Neptune query

```cypher
MATCH (source) WHERE id(source) IN $skill_ids
MATCH (source)-[edge:RELATED_TO]-(target)
RETURN id(source) AS source_id,
       id(target) AS target_id,
       id(edge) AS edge_id,
       type(edge) AS relation_type,
       edge.weight AS weight,
       edge.confidence AS confidence,
       edge.support_jobs AS support_jobs,
       edge.support_companies AS support_companies,
       edge.evidence AS evidence
ORDER BY source_id, weight DESC, target_id
LIMIT 160
```

離線 LTR 同時取得 train-only behavior paths：

```text
QueryIntent
  ├─ INTERACTED_WITH → Job
  └─ OBSERVED_SKILL → Skill ← REQUIRES ← Job
```

edge 保存 `[exposures, positive_events, graded_relevance_sum]`。訓練資料使用 strictly-earlier-day snapshot；confirmation 只能讀 cutoff 時凍結的完整 train graph。若候選群沒有歷史 `INTERACTED_WITH` edge，confidence gate 將 graph feature family 歸零。

安全限制：

- online 最多一跳 `RELATED_TO`
- related contribution 取 max，不累加所有路徑
- graph neighbor 不是充分候選條件；仍需 lexical 或 direct canonical skill evidence
- edge confidence 低於 publish threshold 不可查詢
- cutoff 後 Job 不存在 `REQUIRES`／`INSTANCE_OF` LLM edges
- behavior edge 不保存 `talentNo`，不可作會員個人化
- graph confidence 不足時 abstain，回退到同模型 graph-off 排名

## 實際 trace 範例

本機查詢：

```json
{
  "query": "後端工程師 Node.js",
  "location_code": ["100100"]
}
```

Top job `130280356` 的 trace：

```text
Query:後端工程師 Node.js
  → RESOLVES_TO Skill:skill.nodejs
  → REQUIRES Job:130280356

weight: 0.96
evidence: 職稱：NodeJS資深後端工程師
```

API 可由 `POST /api/v1/graph/trace` 取得前五筆完整 path。

## Region subgraph

Schema version：`skillweave-region-graph-v1`（獨立版本號，與 `skillgraph-v1` 分開消融）

產出：`artifacts/region-graph.json`，由 `scripts/build_region_graph.py` 重建。

狀態：**已建圖並完成量測，尚未接入 `app/` 排序路徑。** 本節描述 schema 與聚合語意；下方「已知限制」說明為何不列入 release gate。

### 設計前提：相鄰不等於可替代

地圖能回答「哪兩個縣市接壤」，但求職決策問的是另一個問題：**求職者自己把哪些縣市當成同一個選項**。這兩件事在資料上並不重合，而且差距是可量測的。

| 縣市對 | 是否接壤 | 共同勾選 | Jaccard | 條件機率落差 |
|---|---|---:|---:|---|
| 新竹市 / 新竹縣 | 是 | 35,360 | 0.39407 | 0.56535 vs 0.56534（對稱） |
| 屏東縣 / 高雄市 | 是 | 28,634 | 0.06417 | P(高雄\|屏東) 0.37174 vs P(屏東\|高雄) 0.07198（5.2x） |
| 台北市 / 基隆市 | 否 | 15,607 | 0.04402 | P(台北\|基隆) 0.46975 vs P(基隆\|台北) 0.04632（10.1x） |
| 新北市 / 新竹市 | 否 | 8,114 | 0.01537 | P(新北\|新竹市) 0.12973 vs P(新竹市\|新北) 0.01714（7.6x） |

兩件事因此成立：

- **接壤但不對稱。** 屏東與高雄接壤，但依附是單向的。無向相鄰圖無法表達 5.2 倍落差。
- **不接壤但強關聯。** 基隆與台北不接壤（中間隔著新北），行為關聯卻高於多數接壤對。

所有權重都來自行為統計，**沒有任何一條邊的權重是人工填的**。

### Node type

| Node | Key | 必要屬性 | 說明 |
|---|---|---|---|
| `Region` | `county` | name, level=`county` | 縣市層級節點，22 個國內縣市 |

`Region` 是 `Location` 的縣市層上卷。解析路徑（查表，不發布為邊）：

```text
搜尋 c0 code  --城市對照表.csv (CodeType 2/3)-->  Region
Job           --職缺.csv「工作城市」-->             Region
```

海外選項不建節點：同時勾選數個洲表達的是「海外皆可」，不是兩個勞動市場之間的可替代性。

### Edge types

| Edge | From → To | 方向 | Weight | Evidence |
|---|---|---|---|---|
| `SUBSTITUTABLE_WITH` | Region ↔ Region | 無向 | `jaccard` 0～1 | 同一次搜尋事件的 `c0` 同時勾選多個縣市 |
| `COMMUTES_TO` | Region → Region | 有向 | `asymmetry` −1～1 | cutoff 前應徵到求職者從未搜尋過的縣市之職缺 |

`SUBSTITUTABLE_WITH` 不需要任何推論：一次搜尋在 `c0` 同時勾選數個縣市，就是使用者本人宣告這些縣市在該次搜尋中等價。

`COMMUTES_TO` 補上無向邊做不到的事 —— 通勤往就業中心的流動本質上是單向的：

| Edge | 應徵數 | 反向 | asymmetry |
|---|---:|---:|---:|
| 基隆市 → 台中市 | 105 | 2 | 0.9626 |
| 新竹市 → 新北市 | 163 | 6 | 0.9290 |
| 雲林縣 → 高雄市 | 284 | 17 | 0.8870 |
| 新北市 → 台北市 | 459 | 327 | 0.1679 |
| 屏東縣 → 高雄市 | 50 | 62 | −0.1071 |

**62 條 `COMMUTES_TO` 中有 28 條（45.2%）在支撐門檻上不存在反向邊。** 若改用無向表示，這 45% 的方向資訊會直接消失。

注意最後兩列：新北↔台北與屏東↔高雄的通勤流向近乎平衡。屏東對高雄在**搜尋替代**上高度單向（5.2x），在**應徵流動**上卻略為反向。兩個訊號測的不是同一件事，因此分開存成兩種邊，不合併成單一分數。

### Provenance 與 leakage 控制

`artifacts/region-graph.json` 的 `metadata` 記錄：

```json
{
  "schema": "skillweave-region-graph-v1",
  "method": "behaviour-derived; no hand-assigned edge weight",
  "dataset_version": "1111-2026-06-01_2026-06-07",
  "graph_cutoff": "2026-06-05 23:59:59.999000",
  "train_days": ["2026-06-01", "…", "2026-06-05"],
  "leakage_policy": "co-selection restricted to train days; applications restricted to on-or-before the graph cutoff",
  "min_co_selected": 100,
  "min_flow": 30,
  "random_seed": 1111
}
```

建圖時明確排除：

| 排除項 | 筆數 | 原因 |
|---|---:|---|
| 評測期（06-06、06-07）搜尋 | 819,411 | 讀取評測期行為等於洩漏被評分的標籤 |
| cutoff 後應徵 | 44,783 | 同上 |
| 匿名 `talentNo = 0` | — | 無法歸戶，會把不同人的選擇混成同一組 |
| 海外選項 | — | 見上 |

保留的支撐量：搜尋 2,248,053 筆（其中 437,454 筆跨 ≥2 縣市，19.46%）、應徵 121,419 筆（其中 4,417 筆跨縣市，3.64%）。支撐低於 `min_co_selected` / `min_flow` 的組合不發布為邊，避免把雜訊寫成圖。

### 遍歷 trace 範例

新竹市在地圖上只有一個鄰居（新竹縣，被其環抱）。行為圖給出三條性質不同的邊：

```text
Query:"作業員"  c0=[100600]
  → RESOLVES_TO Region:新竹市   (城市對照表 CodeNo 100600, CodeType 2)

  ├─ SUBSTITUTABLE_WITH → Region:新竹縣
  │    co_selected 35,360   jaccard 0.39407
  │    P(新竹縣|新竹市) 0.56534   P(新竹市|新竹縣) 0.56535
  │    → 對稱等價：兩地在搜尋行為上幾乎是同一個市場
  │
  ├─ SUBSTITUTABLE_WITH → Region:新北市
  │    co_selected 8,114    jaccard 0.01537
  │    P(新北市|新竹市) 0.12973   P(新竹市|新北市) 0.01714
  │    → 單向 7.6x：新竹市求職者會看新北，反之極少
  │
  └─ COMMUTES_TO → Region:新北市
       applications 163   reverse 6   asymmetry 0.9290
       → 不接壤，且 27 倍單向流動；相鄰圖產生不出這條邊

排序影響：新竹縣職缺以對稱權重納入；新北市職缺以單向權重納入，
且不因此讓新北市搜尋反向獲得新竹市職缺。
```

### API surface

`POST /api/v1/jobs/search` 在 `meta.region_trace` 回傳這個遍歷結果。官方契約欄位（`request_id`、`result`、`empStr`）完全未動；無任何搜尋代碼可解析成國內縣市時（未帶地區、海外代碼、無效代碼）整個鍵省略。契約定義見 `docs/openapi.yaml` 的 `RegionTrace`。

```json
{
  "meta": {
    "region_trace": {
      "schema": "skillweave-region-graph-v1",
      "searched_counties": ["新竹市"],
      "min_conditional": 0.05,
      "expansions": [
        {
          "county": "新竹縣",
          "from": "新竹市",
          "evidence": ["co_selection", "commute_flow"],
          "explanation": "56.5% 搜尋新竹市的求職者同時勾選新竹縣（35,360 次共同勾選）",
          "co_selection": {
            "co_selected": 35360, "jaccard": 0.39407,
            "p_target_given_source": 0.56534, "p_source_given_target": 0.56535
          },
          "commute_flow": {
            "applications": 59, "reverse_applications": 22, "asymmetry": 0.4568
          }
        }
      ],
      "applied_to_ranking": false
    }
  }
}
```

`applied_to_ranking` 恆為 `false`。這個子圖不參與評分，回傳的是行為資料支持什麼，不是排序被改成什麼。

發布閘門（兩者取聯集，任一通過即納入，並記錄在 `evidence` 欄位）：

| Gate | 條件 | 理由 |
|---|---|---|
| `co_selection` | P(target \| source) ≥ 0.05 | 低於此值代表不到二十分之一的來源縣市搜尋者會勾選目標縣市，不足以當作替代地點呈現 |
| `commute_flow` | 存在 `COMMUTES_TO` 邊且 `asymmetry > 0` | 淨流向目標縣市；`min_flow = 30` 已在建圖時把低支撐組合擋掉 |

排序為 `(−P(target|source), −applications, county)` 的全序，最後以縣市名稱決勝，因此同一請求永遠得到同一份輸出。預設回傳前 3 筆（`REGION_GRAPH_LIMIT`）。

`REGION_GRAPH_PATH` 缺檔時模組自動停用、`meta.region_trace` 省略、搜尋照常回應，`/health` 的 `region_graph` 欄位回報可用性。

面向求職者的一行說明直接取自 `explanation`：

```text
你搜尋 新竹市 —— 也納入 新北市
依據：163 位新竹市求職者應徵了新北市職缺，反向僅 6 位
```

### `COMMUTES_TO` 的語意邊界

這條邊量的是**淨應徵流向**，不是通勤本身。資料無法區分「每日通勤」與「搬遷就業」，所以像 `台北市 → 高雄市`（140 vs 32）這種長距離流動也會成為邊。實測 `meta.region_trace` 在搜尋台北市時會據此提出台中市與高雄市，那是真實的流向，但不是通勤。

因此 `explanation` 一律陳述可稽核的原始事實（「140 筆台北市求職者應徵高雄市職缺，反向僅 32 筆」），不宣稱「附近」或「可通勤」。邊名保留 `COMMUTES_TO` 以對應主要語意，語意邊界以本節為準。

### 已知限制

**這個子圖在現行離線評測上的效果必然為零，原因是結構性的，不是調參問題。**

judged 候選集已由現行系統按縣市預先過濾。以 `artifacts/llm-exp-smoke/temporal-eval.json` 實測（職缺縣市取自 `職缺.csv`「工作城市」，1,211,970 筆可解析）：

| 量測 | test | validation |
|---|---:|---:|
| 可解析縣市的 query | 383 | 380 |
| 候選職缺 | 6,620 | 6,702 |
| 屬於非指定縣市 | 7（0.11%） | 75（1.12%） |
| 相關職缺（grade > 0） | 1,027 | 1,022 |
| 相關且屬於非指定縣市 | 2 | 15 |
| **候選只落在 ≤1 個縣市的 case** | **323 / 383（84.3%）** | 316 / 380（83.2%） |

最後一列是關鍵：**超過八成的 case，所有候選職缺同屬一個縣市**。任何縣市層級特徵在這些 case 的候選組內取值恆定，變異數為零，決策樹無法用它分裂。19.46% 的多縣市搜尋亦不例外 —— 候選仍全數落在使用者已勾選的縣市內。

`Region` 子圖的作用位置在**檢索擴充**（放進候選集裡原本不會出現的縣市），而現行 benchmark 是重排評測，結構上不提供擴充候選的機會。詳見 `docs/evaluation-limits.md`。

## Geo graph（行政區層）

Schema version：`skillweave-geo-graph-v1`（再一個獨立版本號，可與 Region 子圖分開消融）

產出：`artifacts/district-graph.json`（行為層，由 `scripts/build_district_graph.py` 重建）
＋ `config/geo-authored.json`（手填層）。組裝與查詢在 `app/geo_graph.py`。

規劃書 [`docs/geo-graph.md`](geo-graph.md) 要的是六層地理圖，權重手填：相鄰 20 分鐘、捷徑 10 分鐘、`is_part_of` 999。
[`docs/geo-graph-handoff.md`](geo-graph-handoff.md) 量到查詢側有 73.05% 的 `c0` 選取是區級代碼、368 個行政區有 366 個被選過，
所以那些權重可以用量的，不必用填的。本節記錄實際做出來的東西與規劃書的差異。

### 與規劃書的四個差異

| 規劃書 | 實作 | 原因 |
|---|---|---|
| `networkx.DiGraph` | `heapq` Dijkstra，約 30 行 | 368 節點 / 4,857 邊。production 只有 8 個套件，換不到東西卻多一個 Lambda 依賴 |
| `is_adjacent_to = 20`、`shortcut = 10` | `-log(共同勾選 Jaccard)` | 資料裡沒有任何東西支持 20 或 10。取 log 之後路徑成本相加恰好等於可替代度相乘 |
| `is_part_of` 權重 999 | 集合查詢 `members_of()` | 999 的顧慮（父節點捷徑作弊）是對的，但那樣任何 `max_distance` 都走不到 L3，該層就失去意義 |
| 節點代碼 7 位 | 6 位，取自 `城市對照表.csv` | 實際資料集是 6 位（`100226` = 八里區），7 位接不上 `location_code` |

### Node type

| Node | Key | 必要屬性 | 說明 |
|---|---|---|---|
| `District` | `<縣市>/<行政區>` | county, district, codes, searches | 368 個行政區，`codes` 對應 `c0` |

節點 key 一定帶縣市。`北區` 在台中、台南、新竹各有一個，`東區` 有四個，裸區名會直接合併成同一個節點。

L1/L3/L5 不是節點，是容器：`members_of(group_id)` 回傳成員行政區，`groups_of(district)` 回傳所屬容器。
橫向距離一律走 L4 的邊。

### Edge type

| Edge | From → To | 方向 | Weight | Provenance |
|---|---|---|---|---|
| `SUBSTITUTABLE_WITH` | District ↔ District | 無向 | `jaccard` 0～1 | `behaviour` |
| `shortcut` | District ↔ District | 無向 | `implied_substitutability` | `external`，帶 `effective_date` |

沒有區級的 `COMMUTES_TO`。應徵流向需要知道職缺在哪一區，而職缺只有 27.79% 解析得到行政區
（`reports/job-district-extraction.json`），那個缺值不是隨機的。縣市層的通勤邊留在 Region 子圖。

建圖量測（`min_co_selected = 30`，train days only，匿名 `talentNo = 0` 排除）：

| 量測 | 值 |
|---|---:|
| 帶區級條件的搜尋 | 1,341,617 |
| 跨 ≥2 區的搜尋 | 967,066 |
| 觀察到的區對 | 9,082 |
| **達支撐門檻的邊** | **4,857** |
| 　同縣市 | 2,655 |
| 　**跨縣市** | **2,202（45.3%）** |
| 有邊的行政區 | 355 / 368 |

### 跨縣市區對是這次才有的

先前的量測把選取先按縣市分組再配對，所以跨縣市的區對根本產生不出來。通勤不會在縣市界停下來，
放開之後 45.3% 的邊是跨縣市的：

```
台北市/南港區  新北市/汐止區  jaccard 0.2701  co 9,053
台北市/北投區  新北市/淡水區  jaccard 0.2190  co 7,559
新北市/林口區  桃園市/龜山區  jaccard 0.1978  co 10,723
新竹市/東區    新竹縣/竹北市  jaccard 0.1461  co 4,475
台中市/大甲區  苗栗縣/苑裡鎮  jaccard 0.1627  co 2,484
```

第三列值得單獨看：**林口與龜山是跨縣市的第三強邊**，而 `林口長庚` 正是抽取器的已知難題
—— 林口長庚醫院行政上屬桃園市龜山區，不屬新北市林口區。搜尋行為獨立地把這兩個區歸成同一個市場，
與那個難題指向同一件事。

### 成本模型

邊成本是 `-log(substitutability)`，所以路徑成本相加等於各段可替代度相乘：

```
八里區 → 五股區       0.1302              cost 2.039
八里區 → 五股區 → 新莊區  0.1302 × 0.3022    cost 3.236
```

`max_cost` 預設 3.0，代表「整條路徑的可替代度 ≥ e⁻³ = 0.0498」，也就是約二十分之一，
與 Region 子圖 `min_conditional = 0.05` 用的是同一個標準。**全程沒有出現任何分鐘數。**

規劃書的八里例子（`max_cost` 放寬到 9.0 取全序）：

| 排名 | 行政區 | cost | 證據 |
|---:|---|---:|---|
| 1 | 淡水區 | 1.4423 | 6,210 次共同勾選 |
| 2 | 五股區 | 2.0391 | 6,329 |
| 3 | **台北市/北投區** | 2.0633 | 3,489（跨縣市） |
| 8 | 林口區 | 2.6684 | 2,348 |
| 28 | **汐止區** | 5.4656 | — |

規劃書的兩個判斷都成立：淡水第一、汐止遠在後段。跨縣市打開後多了一個規劃書沒想到的鄰居（北投）。

### 手填層與它的稽核

`config/geo-authored.json` 放行為導不出來的東西：L1 大區、L3 生活圈、L5 聚落／園區、`shortcut` 邊。
手填本體的問題是沒人能查證，但這些分組其實有可否證的預測：
說「台中海線是一個生活圈」等於預測它的成員被同時勾選的程度高於機遇。

`scripts/validate_geo_authored.py` 就測這件事。統計量是組內平均 Jaccard（沒有邊算 0），
虛無分布是**從同一批縣市的行政區中抽出同樣大小的組**，可窮舉時窮舉（≤ 300,000 組合）、否則抽 20,000 次。
用同縣市而非全台當母體很重要：兩個隨機的台中區本來就比兩個隨機的台灣區更常被一起勾選。

判準是單尾百分位 ≥ 0.95，不是 `> p95`。小母體下兩者會不一致 —— `北車`（中正+大同）
的 cohesion 高於 p95 那個值，但在 66 個可能配對中只排到第 93.9 百分位，而名次才是有意義的量。

`reports/geo-authored-validation.json`：

| 層 | supported | not supported | 不可測 |
|---|---:|---:|---:|
| L1 大區 | 4 | 3 | 0 |
| L3 生活圈 | 6 | 1 | 0 |
| L5 園區 | 3 | 4 | 7（單一行政區） |

**沒通過的不刪掉。** 地圖上理所當然、資料卻不支持的分組，是這份輸出裡最有資訊量的部分：

```
L1/宜花東    cohesion 0.0202  第 81.1 百分位   宜蘭花蓮台東是三個互不流通的市場，不是一個
L5/中科      cohesion 0.0798  第 61.2 百分位   西屯↔后里 只有 0.0097；后里園區是另一個勞動市場
L5/竹科      cohesion 0.0955  第 79.2 百分位   東區↔寶山鄉 0.0955，與別名集中度 60.9% 一致
L3/大新竹    cohesion 0.1049  第 94.2 百分位   該組佔母體 37.5%，本來就與母體難以區分
L3/台北北海岸 cohesion 0.1017  第 95.8 百分位   低空通過，但內部是裂的：金山↔萬里 0.3684、淡水↔萬里 0.0033
```

`竹科` 兩個獨立訊號指向同一結論：別名集中度 60.9%（不是 100%）與 cohesion 未通過，
都反映它真的橫跨新竹市東區與新竹縣寶山鄉。規劃書說竹科需要多重繼承，這是它的證據。

### L5 的別名閘門

L5 別名要進圖必須通過縣市集中度 ≥ 0.60（`reports/region-alias-candidates.json`）。
門檻位置有實測依據：十個非地名負控制組上限 26.66%，最高的被拒候選 46.47%，兩群之間有餘裕。

```
通過   大墩 97.9%  文心 91.8%  北車 86.4%  南科 81.1%  中科 74.0%  竹科 60.9%
被拒   嘉南 46.5%  公益 44.2%  內科 25.8%
```

三個被拒的都是 LLM 很可能答錯的：`內科` 在職缺文字裡絕大多數是醫療科別而非內湖科技園區；
`公益` 是公益活動；`嘉南` 是嘉南藥理大學與嘉南平原。被拒項目留在 config 檔裡並記錄理由，
但永遠不載入圖中 —— `resolve_alias("內科")` 回傳空。

### `shortcut` 與時序過濾

`shortcut` 是外部知識，帶 `effective_date`，`provenance: external`。建圖時 `effective_date > cutoff_date` 的邊直接不載入。

**手填邊是備援，不是覆寫。** 搜尋日誌已經連起來的區對，保留量到的權重，手填值只記錄為佐證。
否則整張圖最顯眼的那條邊會是手填的，「沒有任何邊的權重是人工填的」這句話就在評審第一眼看的地方失效。

實際結果是兩條 `shortcut` 都落在行為已經涵蓋的區對上：

| 邊 | 手填值 | 行為量到的值 | cutoff 2026-06-01 |
|---|---:|---:|---|
| 八里 ↔ 淡水（淡江大橋 2026-05-12） | 0.25 | **0.2364**（6,210 次共同勾選） | 載入，權重取行為值 |
| 三峽 ↔ 鶯歌（三鶯線 2026-06-30） | 0.45 | **0.3827** | **排除** |

所以時序過濾器可證明會動（`cutoff_date` 改成 `2026-07-01`，三鶯線就被接受），
但**在這份資料上它不改變任何一條邊的權重**，因為行為圖已經知道這兩條交通建設。
`artifacts/district-graph.json` 與 `include_authored=False` 的邊數完全相同，4,857 = 4,857。

淡江大橋的手填值 0.25 與量到的 0.2364 很接近，這是巧合等級的一致，不構成驗證：
資料窗（2026-06-01～06-07）整段都在 2026-05-12 通車之後，沒有 before/after 可比。

### API surface

`POST /api/v1/jobs/search` 在 `meta.geo_trace` 回傳遍歷結果，與 `meta.region_trace` 並存且互不影響。
官方契約欄位（`request_id`、`result`、`empStr`）完全未動。**搜尋代碼是縣市級時整個鍵省略** ——
只勾選「新北市」的人沒有指名任何行政區，替他指定一個等於替使用者發言，那個情況由 Region 子圖處理。

```json
{
  "meta": {
    "geo_trace": {
      "schema": "skillweave-geo-graph-v1",
      "searched_districts": ["新北市/八里區"],
      "groups": ["L1/北北基桃"],
      "cost_model": "-log(substitutability); hop weights multiply along a path",
      "max_cost": 3.0,
      "expansions": [
        {
          "district": "新北市/淡水區",
          "cost": 1.4423,
          "substitutability": 0.23639,
          "path": ["新北市/八里區", "新北市/淡水區"],
          "provenance": ["behaviour"],
          "explanation": "6,210 次搜尋同時勾選八里區與淡水區",
          "hops": [
            {"from": "新北市/八里區", "to": "新北市/淡水區",
             "weight": 0.23639, "provenance": "behaviour", "co_selected": 6210}
          ]
        }
      ],
      "edges_excluded_by_cutoff": [
        {"id": "新北市/三峽區--新北市/鶯歌區", "kind": "shortcut",
         "reason": "effective_date after graph cutoff",
         "effective_date": "2026-06-30", "cutoff_date": "2026-06-01"}
      ],
      "authored_edges_corroborated": [
        {"a": "新北市/八里區", "b": "新北市/淡水區", "label": "淡江大橋",
         "implied_substitutability": 0.25, "behaviour_substitutability": 0.23639,
         "co_selected": 6210}
      ],
      "applied_to_ranking": true,
      "ranking_effect": "the out-of-area penalty is withheld from districts listed above; no positive weight is added, and substitutability is carried as an unweighted feature",
      "offline_lift_measured": false,
      "results_from_expanded_districts": 1
    }
  }
}
```

排序為 `(cost, 跳數, 節點名)` 的全序，因此同一份 artifact 永遠得到同一份輸出。
預設回傳前 5 筆（`GEO_GRAPH_LIMIT`）。`GEO_GRAPH_PATH` 缺檔時模組自動停用、`meta.geo_trace` 省略、
搜尋照常回應，`/health` 的 `geo_graph` 欄位回報可用性。`GEO_GRAPH_AUTHORED=0` 關掉整個手填層。

### 手繪相鄰圖：規劃書原本要的東西，以及行為圖對它的判決

產出：`config/geo-adjacency.json`（`scripts/build_geo_adjacency.py`）
驗證：[`reports/geo-adjacency-validation.json`](../reports/geo-adjacency-validation.json)

規劃書 §2 的 `is_adjacent_to` 要的是**地理相鄰**。行為圖答的是另一個問題（誰把哪些區當成同一個選項），
所以兩者可以、而且應該互相檢查。888 條手繪邊、368 個節點、沒有任何一個行政區是孤島（離島另記渡輪／橋樑連結）。

每條邊帶 `commute` 等級，這是**在計算任何東西之前**寫下的，所以它是預測不是描述：

| commute | 意思 | 配對數 | 平均 Jaccard | 有行為邊的比例 |
|---|---|---:|---:|---:|
| `easy` | 一般界線，過去不會有感覺 | 774 | **0.2257** | 95.9% |
| `moderate` | 靠特定橋樑／隧道／隘口 | 7 | 0.0760 | 71.4% |
| `hard` | 山路或渡輪，多數人不會天天通勤 | 7 | 0.0004 | 28.6% |
| `impassable` | 地圖上接壤，實際沒有可用道路 | 9 | **0.0000** | **0.0%** |

**單調遞減，而且 `impassable` 是乾淨的零。** 中央山脈上那些「接壤」的界線
（台中和平↔花蓮秀林、南投仁愛↔花蓮秀林、新竹尖石↔宜蘭大同…）在 4,857 條行為邊裡一條都沒有。
地圖說相鄰，求職者說完全無關，兩邊都對——因為那裡沒有路。

三個交叉結果：

**1. 手繪相鄰有 93.98% 被行為證實**（797 條可測邊中 749 條有對應行為邊）。手繪表本身站得住。

**2. 但 83.8% 的行為邊不相鄰**（4,857 條中 4,070 條）。
**這是「相鄰圖是錯的模型」最直接的證據** —— 規劃書原本只打算建相鄰邊，
那樣會漏掉五分之四的真實可替代關係。跨縣市的 corridor 有 2,091 條，最強的幾條：

```
台北市/萬華區  新北市/永和區   jaccard 0.1412   隔新店溪，不接壤
台北市/北投區  新北市/八里區   jaccard 0.1270   隔淡水河口，中間是淡水區
台北市/中正區  新北市/中和區   jaccard 0.1220
```

**3. 縣市界本身就是障礙，而地圖看不到它。** 只取「平坦、無障礙、`easy`」的相鄰邊，
再依界線是否為縣市界分組：

| | 配對數 | 平均 Jaccard |
|---|---:|---:|
| 同縣市 | 649 | **0.2579** |
| 跨縣市 | 119 | **0.0544** |

**同樣平坦的一條界線，在縣市內的可替代度是跨縣市的 4.74 倍。**
搜尋量最大的幾個例子：新莊↔龜山（39,434 次搜尋、Jaccard 僅 0.0398）、
樹林↔龜山（0.0492）、烏日↔彰化市（0.0483）、三峽↔大溪（0.0255）。
這些都是平地、都有路、都接壤，但求職者不跨那條線。

### 相鄰圖如何進入圖中：補洞，而且權重仍然不是手填的

行為圖覆蓋 368 區中的 355 個，剩下 13 個是偏鄉與離島——沒人搜，所以圖上沒話說。
相鄰圖補這些洞，規則與 `shortcut` 相同：**手繪邊只在行為沉默處生效，永遠不覆寫量到的權重。**

權重也不是填的。`app/geo_graph.py` 在載入時，對每個 `commute × scope` 組合，
取**該組合中確實有行為邊的那些配對的 Jaccard 中位數**，再把這個中位數給同組合中沒有行為邊的配對：

```
easy|intra_county      0.24483        moderate|intra_county  0.03408
easy|cross_county      0.05408        moderate|cross_county  0.08884
hard|cross_county      0.00121        impassable             （無行為邊，不給權重）
```

所以一條手繪邊的價格，是「同類型的、量得到的邊實際上值多少」。
`impassable` 因為沒有任何行為邊可校準，一條邊都不會產生——**山脈不會被補成通路**。

實際加入 83 條邊（4,857 → 4,940）。`include_authored=False` 回到 4,857，完全可消融。

### 候選擴充：這個子圖與 Region 子圖在這裡分岔

**`applied_to_ranking` 在區級不再恆為 `false`。** Region 子圖仍然只是證據；行政區層已接到候選選取。

`GeoExpansion.substitutability`（節點 → 可替代度，被搜尋的區為 1.0）交給 `app/ranker.py`，
對圖背書的行政區**免除跨區扣分**（`location` 由 `-16.0` 變為 `0.0`）。
這改變候選集，所以 `meta.geo_trace.applied_to_ranking` 在排序器確實收到擴充時回報 `true`，
並以 `ranking_effect` 明確描述做了什麼。

**只免除扣分，不加分。** 同區職缺 `location = 2.8`，鄰區職缺 `location = 0.0`，無關區域 `-16.0`，
因此同區永遠排在鄰區前面。可替代度本身以 `geo_substitutability` 記錄但**權重為零**，
與 `intent_*` 家族同樣的理由：那個量級沒有任何東西量過。
這樣做也讓 `location` 停留在 LambdaMART 訓練時見過的取值集合內，不需要重訓。

**這是 recall 改動，沒有離線 lift 數字，也不會有。** 離線 benchmark 是重排評測，
候選集已由現行系統按縣市預先過濾，84.3% 的 case 候選只落在 ≤1 個縣市
（見上一節與 [`docs/evaluation-limits.md`](evaluation-limits.md)），表達不出跨區替代。
`offline_lift_measured` 恆為 `false`，是為了讓這件事無法被讀成量過。

效果只出現在 recall 原本就是瓶頸的地方。可示範的案例：搜「林口區 作業員」。
林口區最近的替代區是**桃園市/龜山區**（可替代度 0.198），跨縣市，
所以新北市的縣市過濾在結構上讓它永遠不可見 —— demo index 裡有 7 筆龜山區作業員職缺，
其中一筆標題就是「【林口半導體廠】-作業員」。

### 已知限制

**職缺側行政區覆蓋率是硬上限。** 全量 27.79%，demo index 只有 4.78%（574／12,000）。
未標註的職缺 `geo_substitutability = 0`，行為與接線前完全相同，所以擴充只會加候選、不會減。
那個缺值不是隨機的：JD 寫地址的（門市、工廠、物流）與不寫的（辦公室、業務）系統性不同。
這也是可替代度不進 LTR 加權的第二個理由 —— 進去就必須先處理「缺值 ≠ 距離遠」的語意，
而那個處理無法在這個 benchmark 上被驗證。

**OpenSearch 路徑目前接不上。** join key 來自 `artifacts/demo-job-districts.json`，只覆蓋 demo index。
線上索引沒有 district 欄位（`scripts/index_full_opensearch.py` 的 `mapping()` 是 `"dynamic": False`，
未宣告的欄位會被靜默丟棄），所以走 OpenSearch 時擴充是惰性的。
排序器據此回報 `geo_applied: false`，`meta.geo_trace.applied_to_ranking` 也跟著是 `false` ——
沒有 mapping 與重建索引之前，不會宣稱線上有這個效果。

**同縣市但很遠的職缺仍然贏過鄰近的跨縣市職缺。** 新北市瑞芳區離林口區 60 公里，
`location = 2.8`；桃園市龜山區緊鄰林口，`location = 0.0`。圖看得見這個矛盾，
但修它需要對「同縣市但無可替代度」的職缺扣分，那會改動每一個沒指定行政區的查詢，
而且需要 95% 的職缺都有行政區標註 —— 兩個條件目前都不成立。

這個子圖仍然不進 release gate。

## Neptune query sketch

```gremlin
g.V().has('Alias','normalized_surface', queryAlias)
 .outE('ALIAS_OF').has('confidence', gte(0.85)).inV()
 .union(
   identity(),
   bothE('RELATED_TO').has('weight', gte(0.55)).otherV()
 )
 .inE('REQUIRES').has('validated', true)
 .where(values('source_modified_at').is(lte(trainCutoff)))
 .group()
 .by(outV().values('job_id'))
 .by(values('weight').max())
```

Production trace 必須同時回傳 edge ID，讓服務可從 provenance store 取回 evidence。
