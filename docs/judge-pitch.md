# 評審簡報骨架（5 分鐘）

## Slide 1 — 第一名就對（20 秒）

一句話：SkillWeave 把非結構化 JD 編成可驗證的技能關係，再讓排序模型只採用
有 evidence 的圖譜訊號。

證據：七日 6.14M 搜尋；Top-1 是題目明確重視的品質。

## Slide 2 — 為何關鍵字不夠（35 秒）

- React / ReactJS / React.js 是同一技能。
- 後端工程師、Node.js、資料庫存在關係，但不是任意同義詞。
- 結構化技能欄位大幅缺漏，不能只靠既有欄位。

畫面：Query → Skill → Job schema。

## Slide 3 — 原創核心（50 秒）

- LLM 在索引前提出 edge，不在結果後自由寫文案。
- Evidence substring、temporal cutoff、confidence、degree cap 決定是否發布。
- Rolling Query→Skill/Job graph 僅讀 strictly-earlier-day snapshot。
- 無信心就 abstain；新職缺仍可由 lexical cold-start 搜尋。

畫面：一筆 direct trace 與一筆 cold-start。

## Slide 4 — Live Demo（70 秒）

1. 搜尋 `React 前端工程師`，點開 path/edge/weight/evidence。
2. 切換無圖譜 baseline，強調同模型、只歸零 graph feature family。
3. 搜尋 `後端工程師 Node.js`，展示 cutoff 後職缺仍可返回但沒有未來 edge。

## Slide 5 — 數字與誠實邊界（55 秒）

- Primary 1,991 queries：NDCG@10 `+5.72%`、MRR `+6.45%`、Hit@1
  `+9.35%`；paired CI `[+0.01491, +0.03607]`。
- 同一 frozen model 在第二個互斥 1,992-query bucket：NDCG `+5.07%`，
  paired CI `[+0.01218, +0.03272]`。
- 歷史負向 holdout與一個 +4.62% 的 rejected candidate 都保留在 repo。
- 目前證明 graph feature family 有價值；尚未把 bootstrap fixture 冒充
  Bedrock 完整產物。

## Slide 6 — 商業價值（35 秒）

Hit@1 的絕對改善換算到七日搜尋量約 160,360 次額外 Top-1 relevance proxy。
這不是 conversion 或營收宣稱；正式 rollout 用 search-to-apply A/B 驗證。

## Slide 7 — AWS 落地與收尾（35 秒）

Offline：S3/Glue → Step Functions → Bedrock → validator → Neptune/OpenSearch。

Online：API Gateway/WAF → Fargate → OpenSearch + Neptune → SageMaker →
contract-safe Top-20。任一 managed service timeout 都能降級。

收尾：多數系統讓 LLM 解釋結果；SkillWeave 讓 LLM 產生可消融、可追溯、可
拒絕的排名資產。

## 評審追問速答

- 為何不是 RAG 文案？核心輸出是 ranking features，移除後有量化退化。
- 如何避免 leakage？JD cutoff、title escrow、rolling graph、disjoint buckets。
- 如何證明不是挑 bucket？先凍結模型，再跑第二個互斥 bucket；兩次都超過 5%，
  且 paired CI 都排除 0。
- 何時可發布 Bedrock graph？完整 train-only batch 通過 evidence precision、
  coverage、ablation 與新預註冊 holdout 後。
- 會傷害新職缺嗎？冷啟動走 lexical/semantic path，並監控
  time-to-first-qualified-view。
