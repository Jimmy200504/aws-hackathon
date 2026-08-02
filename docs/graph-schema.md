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
  hackathon leakage-free evaluation graph，不是 production serving pointer。
- `latest`：包含完整七日資料集，使用獨立 immutable manifest，為本機與 AWS API
  的預設 serving graph。
- 每個輸入必須進 accepted 或 quarantine；cutoff 後的有效資料另計為
  `post_cutoff_excluded`，不可偷偷進 cutoff graph。

真實 inventory scan 已確認 1,218,635 筆輸入，其中 cutoff eligible 967,377、
post-cutoff 251,258、無無效時間戳。完整 graph 發布仍需通過 referential integrity、
ranking non-regression、至少一項主要 NDCG 正 lift、API/degraded smoke 與 p95 `<800 ms`。

目前 `latest` production graph 實際包含 1,219,372 nodes、5,249,573 edges；
其中 Job nodes 為 1,218,635，沒有職缺因 cutoff 被排除。

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
