# Graph coverage 與必要性診斷

本頁是 **RC6 歷史模型** 的 post-hoc coverage 診斷，保留來說明模型如何從
失敗實驗演進；它不是目前 release claim。目前 authoritative evidence 是
`reports/ltr-quality-confirmation.json`（+5.72%）與互斥 replication
（+5.07%）。本頁的 1,993-query 舊結果由
`scripts/report_graph_coverage.py` 產生到 `reports/graph-coverage.json`。

## 為何整體 lift 被稀釋

| 範圍 | Queries | 無圖譜 NDCG@10 | 有圖譜 NDCG@10 | 相對 lift |
|---|---:|---:|---:|---:|
| RC6 locked confirmation | 1,993 | 0.4168 | 0.4225 | +1.34% |
| Confidence gate active | 285 | 0.3434 | 0.3826 | +11.41% |
| Relevant row 有 graph feature | 1,115 | 0.3902 | 0.4042 | +3.60% |

Gate-active subgroup 的 paired mean delta 是 `+0.03920`，95% CI
`[+0.01609, +0.06378]`。其餘 1,708 queries 因 gate abstain，graph-on
與 graph-off 完全相同。因此目前限制主要是 coverage，而不是已覆蓋 query 上
完全沒有 ranking signal。

其他 coverage：

- 85.90% queries 至少一個 candidate 帶 graph feature。
- 55.95% queries 至少一個 relevant candidate 帶 graph feature。
- 只有 14.30% queries 通過嚴格 historical Query→Job confidence gate。
- Relevant candidate row 的 graph coverage 是 40.53%。
- Graph feature 佔 XGBoost normalized gain 50.04%；這是描述性 model reliance，
  不是因果效果。

## 解讀限制

這是鎖定 confirmation 上的 post-hoc subgroup 診斷，不是新的預註冊
release gate，也不能取代整體結果。報告刻意保留 `five_percent_gate_passed:
false`，release verifier 會在有人改寫該欄位時失敗。

目前本機 benchmark 的 graph builder 是 `reviewed-bootstrap-fixture`。上述結果
證明已實作的 graph feature family 在有信心覆蓋時有價值，但尚不能單獨證明
Amazon Bedrock extraction 的增量。下一個合法改善方向是只在 train-only JD
跑 Bedrock batch、通過 evidence validator 後擴大 relevant-row coverage，再用
新的預註冊 holdout 驗證；不可在目前 confirmation 上繼續調 gate。
