# SkillWeave 決賽提交包

> 由 `scripts/build_submission_packet.py` 從 RC manifest 與機器報告產生。
> 公開 URL 為 PENDING 時不可提交 placeholder；團隊欄位須由參賽者補填。

## 基本欄位

- 作品名稱：**SkillWeave**
- 建議副標：**Evidence-Gated AI Skill Graph for Job Search Ranking**
- Release：`skillweave-2026.07.28-rc6`
- 團隊名稱：**參賽者填寫**
- 聯絡人：**參賽者填寫**

## 一句話（可直接貼上）

SkillWeave 把非結構化職缺編成有原文證據與時間邊界的技能圖譜，再以
Unbiased LambdaMART 重排搜尋結果；每條圖譜路徑都可追溯、可消融，證據不足
時會拒絕使用。

## 作品摘要

1111 的七日資料包含 6,139,952 次搜尋、8,241,233 次職缺瀏覽與 225,999 次
主動應徵，但既有結構化技能欄位大量缺漏，同一技能也有多種寫法。SkillWeave
在索引前使用生成式 AI 提出 skill／alias／relation，只有通過 evidence
substring、temporal cutoff、type whitelist 與 confidence gate 的邊才能發布。
線上以 Query → Skill → Job 的一跳路徑產生可解釋 ranking features；無信心或
新職缺則回退 lexical cold-start，不用生成式 AI 自由撰寫結果文案。

鎖定後的 1,993 個 confirmation queries 上，同一個
已訓練模型在關閉 graph feature family 後作為 baseline。Graph-on 的 NDCG@10
由 0.41684 提升到 0.42245
（相對 +1.34%），MRR +1.72%、Hit@1
+2.70%；paired NDCG 差異的 95% CI 為
[0.00226, 0.00905]。整體建議 5% gate **未通過**，
第一個負向 holdout 也保留在 repo。

## 原創性與生成式 AI 必要性

- LLM 的核心輸出是可驗證的 ranking asset，不是結果後方的裝飾性摘要。
- 每條 edge 保存 evidence、source timestamp、model/prompt version。
- JD cutoff、title escrow、strictly-earlier-day rolling graph 與 disjoint
  buckets 防止 future leakage。
- Confidence abstention、OOV ephemeral node、cold-start quarantine 與
  deterministic fallback 讓錯誤可以被拒絕。
- Graph-on/off 使用同一模型，只歸零 graph feature family，因此 ablation
  直接量測圖譜訊號的增量價值。

## AWS 技術路徑

- Compact judge demo：API Gateway HTTP API + Lambda Python 3.13 arm64 +
  CloudWatch；exact bundle 已通過 SAM validate/build/local invoke。
- Production design：S3/Glue → Step Functions → Bedrock structured batch →
  evidence validator → OpenSearch + Neptune → SageMaker Unbiased LambdaMART →
  API Gateway/WAF。
- 任一 managed service timeout 都回傳 contract-safe fallback ranking。
- Compact demo 不冒充完整 OpenSearch／Neptune／SageMaker production
  deployment。

## 量化證據

| 證據 | 結果 | 解讀限制 |
|---|---:|---|
| Locked NDCG@10 relative lift | +1.34% | 整體正式結果；5% gate 未過 |
| Locked MRR relative lift | +1.72% | 同一模型 graph ablation |
| Locked Hit@1 relative lift | +2.70% | 離線 relevance proxy |
| Locked Hit@10 relative lift | -0.12% | 不隱藏小幅負值 |
| Paired NDCG 95% CI | [0.00226, 0.00905] | 差異全為正 |
| Relevant-row graph coverage | 40.53% | Coverage 是下一個瓶頸 |
| Gate-active subgroup | 285 queries；NDCG +11.41% | Post-hoc，不能取代整體 |
| 七日規模換算 | 約 40,050 次額外 Top-1 relevance events | 非 conversion、apply、hire 或營收 |
| Release verifier | 60 PASS / 0 FAIL / 3 WARN | WARN 僅代表尚未登錄的外部 URL |

## 商業應用

求職者更快看到符合意圖的第一個職缺；企業得到更高品質而非單靠熱門度的曝光；
平台可改善 search-to-view、search-to-apply 與 query reformulation。正式上線
採 shadow → 5% → 25% → 50% → 100% A/B ramp，primary metric 是
search-to-apply，並監控 latency、zero-result、新職缺 exposure 與 subgroup
gap。目前沒有因果轉換或單次相關曝光的貨幣價值，因此**不宣稱營收**。

## AWS Kiro 加分證據

- Kiro CLI：`2.15.0`
- Session：`7cf6f97b-3435-49ff-a214-950453ce8b08`
- Evidence：`docs/kiro-evidence.md`
- 產物：release verifier contract、tracked task、實作後 review 與機器報告。

## 公開交付 URL

- AWS Demo：**PENDING — 不可填 placeholder**
- GitHub Release：**PENDING — 不可填 placeholder**
- 5 分鐘影片：**PENDING — 不可填 placeholder**

本機影片證據：300.046 秒、1080p H.264、AAC、
繁中字幕；SHA-256 `2c7ee08c5851ac8e3673d7f94d7116108846515edaa4ad47188d56cf17f8509a`。

## 評審操作

1. 搜尋 `AWS Docker Kubernetes`，確認 Graph ON 的 Top-10 與 trace。
2. 切換 baseline，確認不是按鈕換色，而是排序與 graph contribution 改變。
3. 搜尋 `React 前端工程師`，展開 Query → Skill → Job evidence。
4. 查看 cold-start 職缺，確認 cutoff 後 JD 沒有 graph edge。
5. 執行 `./scripts/release_gate.sh`，核對 tests、hash、ablation 與失敗實驗。

## 不可誇大的限制

- 不可宣稱整體 NDCG 提升 5%；正式 locked 值是
  +1.34%。
- 不可把 285-query post-hoc subgroup
  +11.41% 偷換成整體結果。
- 不可把 bootstrap fixture 說成完整 Bedrock batch 產物。
- 不可把 30 分鐘 attribution qrels 說成主辦方正式 relevance ground truth。
- 不可把約 40,050 次 Top-1 relevance proxy 說成應徵、錄取或營收。
- 未完成 clean-session 驗證前，不可填入或展示 placeholder URL。
