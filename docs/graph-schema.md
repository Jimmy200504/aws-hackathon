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
