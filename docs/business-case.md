# 商業應用與驗證計畫

## 問題與受益者

1111 提供的七日資料包含 6,139,952 次搜尋、8,241,233 次職缺瀏覽與
225,999 次主動應徵。產品問題不是結果頁「完全沒有職缺」，而是求職者最先
看到的結果是否符合查詢意圖。排序改善同時服務三方：

- 求職者：更快看到適合職缺，減少改寫 query 與無效瀏覽。
- 企業客戶：合適職缺得到更高品質曝光，而不是只靠既有熱門度。
- 平台：提高 search-to-view、search-to-apply 與回訪，降低零結果及人工同義詞
  維護成本。

## 有界的規模換算

鎖定 confirmation 的 Hit@1 從 `0.24134` 到 `0.24787`，絕對增加
`0.00652`。若只把這個離線比例機械式套到資料中的 6,139,952 次七日搜尋，
相當於每週約 **40,050 次「第一名為 relevant」的額外事件**。

這不是轉換、應徵、錄取或營收預測。它只是讓評審理解一個看似小的離線絕對
差異在平台流量尺度上的量級。可重現計算在
`scripts/report_business_impact.py` 與 `reports/business-impact.json`；因尚無
隨機實驗和每次增量相關曝光的貨幣價值，報告刻意讓 revenue 為 `null`。

## North-star 與 guardrails

Primary product metrics：

1. Search-to-apply rate（query 後 24 小時與 7 天兩個窗口）。
2. Top-1 view/apply share。
3. First relevant click rank 與 query reformulation rate。

Guardrails：

- p95 / p99 latency、timeout、HTTP 5xx。
- Zero-result、no-qualified-result、OOV、cold-start rate。
- 新職缺 time-to-first-qualified-view，避免 popularity feedback loop。
- 地區／職類 subgroup 的 exposure 與 apply-through gap。
- 使用者 complaint / hide-job rate。

## 因果 A/B 設計

- Unit：匿名但穩定的 search request bucket；不依賴正式 API 沒有提供的
  `talentNo`。
- Control：現行 BM25 + 同義詞 baseline。
- Treatment：同 candidates 加 evidence-gated graph + locked LTR。
- Ramp：shadow → 5% → 25% → 50% → 100%。
- Primary：search-to-apply；secondary：Top-1 click/apply、reformulation。
- Guardrail：latency、zero result、新職缺 exposure、職類/地區 fairness。
- 分析：預先固定 eligibility、sample-size、minimum detectable effect、窗口、
  stopping rule；不因每日波動提前宣告成功。
- 回滾：任何 guardrail 超標即把 model/index/graph manifest 一起切回上一版。

## 商業化與營運模型

第一階段是平台核心搜尋品質，不向求職者收費。價值由更高品質媒合、企業職缺
成效與留存產生。後續可提供企業端「技能需求缺口」與「職缺文字覆蓋診斷」，
但只輸出彙總趨勢，不販售或重建個人履歷／身份。

營運 dashboard 每日顯示 coverage funnel：

```text
all searches
  → canonical intent resolved
  → candidate has validated skill edge
  → confidence gate active
  → graph changes Top-10
  → view / apply outcome
```

目前 locked evidence 顯示 confidence gate 只覆蓋 14.30% queries；因此下一個
投資假設很清楚：用 train-only Bedrock extraction 提升 relevant-row coverage，
而不是放寬 evidence gate 來追求表面 lift。
