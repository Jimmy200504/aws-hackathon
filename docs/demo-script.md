# 5 分鐘決賽 Demo Script

## 0:00–0:25 — 問題

畫面：首頁。

講稿：

> 1111 不缺職缺，也不缺搜尋；真正昂貴的是第一頁不準。SkillWeave 的目標不是讓使用者「找得到」，而是讓第一個結果就更接近真正意圖。

## 0:25–1:10 — 第一次搜尋

輸入：`AWS Docker Kubernetes`。

指出：

- Top 20、latency、graph on
- Top 1 的職稱、地區、匹配技能
- `WHY THIS JOB` 不是 LLM 自由寫文案，而是實際 feature evidence

## 1:10–2:05 — Graph trace

點「查看推論證據」。

講稿：

> 離線 extractor 不使用 LLM：它只把 JD 中 reviewed exact aliases 對應成 canonical nodes，並保存原文 evidence。未知詞先進人工審閱佇列。線上只走一跳、帶權、可追溯的路徑；沒有通過 evidence validator 的靜態邊不會發布。

顯示：

```text
Query → Skill:kubernetes → Job
weight + evidence + edge type
```

## 2:05–2:45 — Ablation

切換「無圖譜 Baseline」。

講稿：

> 同一個已訓練模型可以把 graph/behavior feature family 歸零，避免拿兩個不同容量的模型製造假 lift。凍結後的主要 1,991-query confirmation 中，NDCG 提升 5.72%、MRR 6.45%、Hit@1 9.35%，paired CI 完全大於 0。同一模型在第二個互斥 1,992-query bucket 仍提升 5.07%。歷史失敗 holdout與一個只到 4.62% 的 rejected candidate 也完整留在 repo。

## 2:45–3:30 — Leakage-free evaluation / full serving graph

開一筆 6/5 後修改的職缺。

講稿：

> 這筆 JD 修改時間晚於 train cutoff；離線評測仍使用 frozen cutoff graph 避免 leakage，production 則使用獨立 latest graph，所以它現在有可追溯技能邊且不是 cold start。兩個 scope 的 immutable manifest 完全分離。

## 3:30–4:15 — AWS architecture

畫面：架構圖。

講稿：

> Offline 由 Step Functions 執行 DeterministicExtract、ResolveExactAliases、BuildStatisticalRelations 與 ExportAndValidate；OpenSearch 做全量 candidates，Neptune 聚合一跳 graph feature，LambdaMART 重排。線上 Bedrock 只正規化 Query，任一服務超時仍回合法 fallback ranking。

## 4:15–4:45 — 商業價值

講稿：

> 排名的商業指標不是漂亮文案，而是 Top-1 view/apply、search-to-apply、query reformulation 與新職缺 time-to-first-qualified-view。我們先 shadow，再 5% A/B，並把 latency 與零結果率當 guardrail。

## 4:45–5:00 — 收尾

> SkillWeave 讓生成式 AI 成為可量測、可消融、可追溯的索引資產；如果移除圖譜不會退化，我們就不宣稱它有價值。

畫面停在雙 confirmation、79/79 release checks 與公開 AWS Demo。

## 可重現影片

tracked source 位於 `video/pitch-deck.html` 與 `video/scenes.json`：

```bash
python3 scripts/render_demo_video.py
```

輸出 `dist/skillweave-demo-5min.mp4`、繁中 SRT sidecar 與
`reports/demo-video.json`。公開上傳後，才以 `scripts/update_release_urls.py`
登錄真實 HTTPS URL。
