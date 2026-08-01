# 生成式 AI 萃取：已知失敗模式與防護

正式離線 Skill Graph build 是零 LLM deterministic pipeline：reviewed exact alias、
原文 evidence、duty taxonomy 與 corpus statistics 決定所有發布內容。Bedrock 只保留
在線上 Query normalization；失敗時使用 deterministic fallback。

## 已封存的歷史 Bedrock pilot（非 production graph build）

Claude Haiku 4.5 以 strict JSON Schema 對 200 筆 cutoff 前職缺執行。最終
180 筆通過、20 筆因沒有足夠可發布 mention 而 quarantine、0 fatal；
validator 發布 1,598 個 exact-substring-grounded mentions。296 條 relation
proposal 全部維持 `requires_corpus_corroboration`，沒有直接發布。公開證據只含
aggregate counts、token 與成本，不含 job/user identifiers，見
`reports/bedrock-pilot.json`。這份 200 筆報告保留作為舊實驗與失敗模式證據，
不屬於新的 cutoff/latest graph manifests，graph worker 也沒有 Bedrock IAM 權限。

| 失敗模式 | 例子 | 風險 | 防護 |
|---|---|---|---|
| 幻覺技能 | JD 沒寫 Kubernetes，模型因 DevOps 自行補上 | 假相關 | `REQUIRES` evidence 必須是 JD exact substring；否則 reject |
| 同義詞誤合併 | Java 與 JavaScript 合併 | 大量錯排 | alias collision test、canonical allowlist、語言邊界、人工 review queue |
| 層級錯置 | 後端工程師被當成 Skill | schema 混亂 | Skill / Occupation type constraint；edge domain/range validation |
| 中英縮寫歧義 | RN、BI、AI | 多義誤判 | context-conditioned candidate set；低 confidence 建 `AmbiguousAlias`，不直接合併 |
| 子字串誤判 | `js` 命中 `node.js` | 重複／錯節點 | ASCII boundary 含 `. + #`；單元測試鎖住 |
| Required / preferred 混淆 | 「有 AWS 佳」被當必要 | 過度匹配 | `requirement_level` 枚舉；preferred edge 降權 |
| Negation | 「不需具備經驗」 | 反向解讀 | evidence window 包含否定詞；negation validator |
| 薪資／品牌誤認技能 | Excel 公司名或產品名語境 | 噪音 | field-aware prompt、entity type classifier、corpus frequency review |
| 過度關聯 | React → 任意 frontend tool | graph flooding | related edge degree cap、max-one-hop、max aggregation、offline lift gate |
| OOV 新技能 | reviewed ontology 未收錄 | 零召回 | 只從 structured fields 聚合 candidate；至少 3 jobs、2 companies 才進人工審閱，永不直接寫 production graph |
| Prompt/model drift | 同一 JD 新版模型輸出改變 | 不可重現 | 保存 model ID、prompt hash、schema、seed/settings；canary corpus diff |
| Prompt injection in JD | JD 文字要求模型忽略 schema | 越權輸出 | JD 放在 data field、工具不可用、strict JSON schema、allowlisted nodes/edges |

## Validation gates

1. JSON schema validation。
2. `REQUIRES.evidence` 必須存在於指定 source field。
3. source job 的 `modified_at <= graph_cutoff`。
4. node/edge type 與 domain/range 合法。
5. canonical ID 不得由模型自由創造；新 node 先進 quarantine。
6. confidence threshold：
   - ≥0.90 自動接受（仍須所有 deterministic gates）
   - 0.75～0.90 進 review／corpus corroboration
   - <0.75 reject
7. alias 一對多時保留 ambiguity，不做強制 merge。
8. 每個 node degree、每個 job skill count 有上限與異常告警。
9. 固定 gold corpus 計算 extraction precision/recall 與 alias merge precision。
10. graph publish 前必須通過「有圖譜 vs 無圖譜」validation lift；沒有 lift 不發布。

## Query-time disclosure

對使用者顯示的「為什麼推薦」只能由實際 feature/edge 產生，不再呼叫 LLM 自由寫理由。若沒有 evidence：

- 顯示「綜合相關性排序」，或
- 顯示 cold-start 狀態。

不得虛構技能落差，也不得把推論關係說成求職者具備的能力。
