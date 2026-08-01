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

對照的可解釋輸出（一行，面向求職者）：

```text
你搜尋 新竹市 —— 也納入 新北市
依據：163 位新竹市求職者應徵了新北市職缺，反向僅 6 位
```

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
