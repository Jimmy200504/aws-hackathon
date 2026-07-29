# 評分規準差距分析

更新日期：2026-07-29。狀態以目前 workspace 可驗證證據為準，不以計畫或口頭主張代替完成。

| 評分類別 | 權重 | 拿高分需要的證據 | 現況 | 判定 | 最高價值下一步 |
|---|---:|---|---|---|---|
| 創意度 | 25% | 原創圖譜建構、融合機制、可展示 trace | Train-time title evidence escrow、rolling Query→Skill/Job graph、confidence abstention、cold-start quarantine、可點選 trace；真實 Bedrock pilot 發布 1,598 個 grounded mentions | **競賽證據完成** | Production 才擴到完整 corpus 與 ambiguity calibration |
| 技術可行性 | 25% | 可落地；處理 leakage、OOV、曝光偏差與失敗模式 | Unbiased LambdaMART、strictly-earlier-day graph、OOV/cold-start、雙 disjoint confirmation 與 paired bootstrap 已執行；exact ZIP 已由 SAM Python 3.13 arm64 validate/build/invoke，且部署至 API Gateway/Lambda；public smoke 30/30、p95 4.40s，frozen LTR 與 Bedrock provenance 均在線 | 接近完成，runtime 已驗證 | 完整 Bedrock batch 與較大規模 production path |
| 商業應用性 | 20% | 清楚解決真實問題、有 KPI 與 rollout | 已定義三方價值、north-star、guardrails、因果 A/B、coverage dashboard；Hit@1 絕對 lift 的七日規模 proxy 約 160,360，明示非轉換／營收 | **本機證據完成** | 部署後收集真實 search-to-apply A/B，不預估營收 |
| 主題切合度 | 20% | 生成式 AI 是必要核心；移除圖譜 NDCG 明顯退化 | Frozen model 在兩個互斥 confirmation buckets 的 NDCG 分別 +5.72%、+5.07%，paired CI 均排除 0；真實 Claude Haiku 4.5 structured-output pilot 200 筆、180 accepted、1,598 mentions | **核心證據完成** | 完整 corpus 是 production migration，不冒充已完成 |
| 完成度 | 10% | 功能順、API 正確、AWS URL、5 分鐘影片、GitHub 可重現 | Public AWS UI/API、live graph toggle、30-request smoke、portable deterministic SAM package、CI/release gate、300 秒 1080p 繁中影片、公開 GitHub release 與影片 URL 均完成 | **完成** | 現場只需帶 submission packet 與備援影片 |
| AWS Kiro 加分 | +5% | 可驗證採用 Kiro | 已登入 Kiro CLI session 完成 verifier contract 設計與實作後 review；session ID、tracked task、產物與機器報告均保存 | **可主張，待評審認定** | 在簡報連結 `docs/kiro-evidence.md`，現場列出 session metadata 並執行 verifier |

## 阻擋滿分的主要缺口

1. **完整 corpus GenAI graph 尚未執行**：200-record 真實 Bedrock pilot 已完成，
   但不冒充完整 production batch。
2. **主辦方統一 evaluation script 尚未提供／執行**：目前 +5.72% 與 +5.07%
   是嚴格時間切分、互斥 bucket 的內部 confirmation。

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
3. 展示兩個互斥 confirmation 與 rejected candidate；再打開一筆 cutoff 後職缺，證明未來 JD 不進圖。
