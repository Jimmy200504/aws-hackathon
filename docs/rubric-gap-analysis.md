# 評分規準差距分析

更新日期：2026-07-28。狀態以目前 workspace 可驗證證據為準，不以計畫或口頭主張代替完成。

| 評分類別 | 權重 | 拿高分需要的證據 | 現況 | 判定 | 最高價值下一步 |
|---|---:|---|---|---|---|
| 創意度 | 25% | 原創圖譜建構、融合機制、可展示 trace | Train-time title evidence escrow、rolling Query→Skill/Job graph、confidence abstention、cold-start quarantine、可點選 trace 已實作 | 接近完成 | 以 Bedrock 完整建圖，加入 ambiguity nodes 與 uncertainty calibration |
| 技術可行性 | 25% | 可落地；處理 leakage、OOV、曝光偏差與失敗模式 | Unbiased LambdaMART、strictly-earlier-day graph snapshots、disjoint hash buckets、alias boundary、OOV/cold-start、paired bootstrap 均已執行；AWS managed path 尚未部署 | 接近完成 | 負載測試、完整 Bedrock batch、AWS deploy |
| 商業應用性 | 20% | 清楚解決真實問題、有 KPI 與 rollout | 搜尋首位品質、可解釋推薦、零結果/OOV/新職缺監控均有產品路徑 | 接近完成 | 補 A/B experiment design、GMV 等價商業 KPI、營運 dashboard |
| 主題切合度 | 20% | 生成式 AI 是必要核心；移除圖譜 NDCG 明顯退化 | 1,993-query locked confirmation：NDCG +1.34%、paired CI 排除 0；gate-active 285-query post-hoc subgroup +11.41%，顯示瓶頸是安全 coverage，但整體仍低於建議 5% | **部分通過，證據加強** | 用完整 Bedrock graph 提升 40.53% relevant-row coverage，再接受新的預註冊 holdout |
| 完成度 | 10% | 功能順、API 正確、AWS URL、5 分鐘影片、GitHub 可重現 | 本機 UI/API/26 tests、holdout、portable deterministic SAM package、CI/release gate 完成；沒有 AWS URL、影片、公開 GitHub | 未完成 | 安裝 AWS/SAM CLI 並取得 credentials 後一鍵部署、錄影、publish release |
| AWS Kiro 加分 | +5% | 可驗證採用 Kiro | 已登入 Kiro CLI session 完成 verifier contract 設計與實作後 review；session ID、tracked task、產物與機器報告均保存 | **可主張，待評審認定** | 在簡報連結 `docs/kiro-evidence.md`，現場列出 session metadata 並執行 verifier |

## 阻擋滿分的三件事

1. **5% 主題門檻未過**：最終 confirmation 是顯著正向，但 NDCG 相對 lift 只有 1.34%。
2. **未實際部署 AWS**：題目明定需評審可存取 URL；workspace 沒有 AWS credentials。
3. **最終提交物不足**：缺 5 分鐘影片、公開 GitHub release 與主辦方統一腳本結果。

## 已移除的零分風險

- API 同時接受 `query/location_code/duty_code` 與 `ks/c0/d0`，回應 rank 連續且 job ID 去重。
- `talentNo=0` 不做使用者串接。
- graph cutoff 後修改的 JD 不產生 graph edge。
- alias extraction 不再把 `js` 錯誤匹配在 `node.js` 中。
- related edge 不能單獨成為候選證據，也不能無上限疊加壓過 lexical/direct evidence。
- 未驗證指標不顯示成成功數字。

## 建議競賽敘事

核心句：

> 多數搜尋系統把 LLM 放在結果後面寫說明；SkillWeave 把 LLM 放在索引前面，產生可驗證、可消融、可追溯的技能結構。機器學習只相信通過 evidence gate 的邊。

三段 demo：

1. 搜尋 `Node.js 後端工程師`，展示 alias normalize 與直接技能 trace。
2. 關閉 graph，展示相同 API 與 feature ablation。
3. 展示 1,993-query confirmation 與失敗 holdout；再打開一筆 cutoff 後職缺，證明未來 JD 不進圖。
