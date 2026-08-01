# Skill Graph Schema

Schema version：`skillgraph-v1`

## Node types

| Node | Key | 必要屬性 | 說明 |
|---|---|---|---|
| `Skill` | `skill_id` | label, type, aliases, language | 正規化技能，如 React、Excel、病患照護 |
| `Occupation` | `occupation_id` | label, duty_codes | 職務意圖，如後端工程師、護理師 |
| `Job` | `job_id` | source_modified_at, graph_eligible | 職缺；ID 等於職缺主檔主鍵 |
| `Alias` | `normalized_surface` | surface, language, ambiguity | 中英別名／縮寫 |
| `Industry` | `industry_id` | label | 產業 context |
| `Location` | `location_code` | names, level | 城市／行政區 |
| `QueryIntent` | `normalized_query` | first_seen, last_seen | 只保存 train-window 的正規化查詢聚合；不含 talent ID |

`Skill` 與 `Occupation` 分開，避免把「後端工程師」錯當成單一工具技能。

## Edge types

| Edge | From → To | Weight | Evidence |
|---|---|---:|---|
| `ALIAS_OF` | Alias → Skill/Occupation | 0～1 | LLM normalize + collision validator |
| `REQUIRES` | Job → Skill | 0～1 | JD 原文 substring；required/preferred |
| `INSTANCE_OF` | Job → Occupation | 0～1 | 職稱、duty code、LLM classification |
| `RELATED_TO` | Skill ↔ Skill | 0～1 | LLM proposal + corpus support + review gate |
| `IN_INDUSTRY` | Job → Industry | 1 | 主檔欄位 |
| `IN_LOCATION` | Job → Location | 1 | 主檔／對照表 |
| `INTERACTED_WITH` | QueryIntent → Job | exposure-normalized | train-only view/apply qrels aggregate |
| `OBSERVED_SKILL` | QueryIntent → Skill/Occupation | exposure-normalized | Query→positive Job→Skill path aggregate |

禁止任意 edge type。每條 LLM edge 至少保存：

```json
{
  "source_id": "job:130280356",
  "edge_type": "REQUIRES",
  "target_id": "skill:nodejs",
  "weight": 0.96,
  "evidence": "NodeJS資深後端工程師",
  "evidence_field": "職務名稱",
  "source_modified_at": "2026-05-...",
  "extractor_model": "bedrock:model-id",
  "prompt_version": "jd-skill-v3",
  "schema_version": "skillgraph-v1",
  "validated": true
}
```

## Online aggregation

Query 先經 alias normalization 得到 canonical nodes：

```text
Query("後端工程師 Node.js")
  ├─ RESOLVES_TO → Occupation:backend
  └─ RESOLVES_TO → Skill:nodejs
```

對每個候選 Job 計算：

```text
direct_skill = max(QuerySkill == Job-REQUIRES->Skill)
related_skill = max(QuerySkill-RELATED_TO->Skill<-REQUIRES-Job)
occupation = QueryOccupation == Job-INSTANCE_OF->Occupation
graph_score = bounded(direct_skill, related_skill, occupation)
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

沒有區級的 `COMMUTES_TO`。應徵流向需要知道職缺在哪一區，而職缺只有 26.53% 解析得到行政區
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
      "applied_to_ranking": false
    }
  }
}
```

排序為 `(cost, 跳數, 節點名)` 的全序，因此同一份 artifact 永遠得到同一份輸出。
預設回傳前 5 筆（`GEO_GRAPH_LIMIT`）。`GEO_GRAPH_PATH` 缺檔時模組自動停用、`meta.geo_trace` 省略、
搜尋照常回應，`/health` 的 `geo_graph` 欄位回報可用性。`GEO_GRAPH_AUTHORED=0` 關掉整個手填層。

### 已知限制

**`applied_to_ranking` 恆為 `false`，理由與 Region 子圖完全相同，而且在區級更嚴重。**

離線 benchmark 是重排評測，候選集已由現行系統按縣市預先過濾，84.3% 的 case 候選只落在 ≤1 個縣市
（見上一節與 [`docs/evaluation-limits.md`](evaluation-limits.md)）。縣市內都沒有變異，區級只會更少。
把區級距離做成 LTR 特徵，在候選組內取值恆定，決策樹無法用它分裂 —— 效果不是小，是數學上為零。

另外職缺側只有 26.53% 解析得到行政區，其餘 73% 只能退到縣市。那個缺值不是隨機的：
JD 寫地址的（門市、工廠、物流）與不寫的（辦公室、業務）系統性不同。
這是不把區級距離送進 LTR 的第二個理由 —— 進去就必須先處理「缺值 ≠ 距離遠」的語意，
而那個處理無法在這個 benchmark 上被驗證。

因此這個子圖與 Region 子圖一樣不進 release gate，作用位置在檢索擴充與可解釋性展示。

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
