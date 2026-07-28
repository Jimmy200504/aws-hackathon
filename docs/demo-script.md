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

> LLM 的核心工作發生在索引前：它把 JD 中不同寫法正規化成 AWS、Docker、Kubernetes nodes，並保存原文 evidence。線上只走一跳、帶權、可追溯的路徑；只有凍結 train graph 有高信心歷史 edge 時才啟用 graph model，否則 abstain。

顯示：

```text
Query → Skill:kubernetes → Job
weight + evidence + edge type
```

## 2:05–2:45 — Ablation

切換「無圖譜 Baseline」。

講稿：

> 同一個已訓練模型可以把 graph feature family 歸零，避免拿兩個不同容量的模型製造假 lift。鎖定後的 1,993-query confirmation 中，NDCG 提升 1.34%、MRR 1.72%，paired CI 排除 0。它是顯著改善，但沒有到 5%；第一個失敗 holdout 也完整留在 repo。事後 coverage 診斷顯示，嚴格 confidence gate 真正啟用的 285 queries 提升 11.41%，其餘完全 abstain；這解釋 coverage 瓶頸，但不拿 subgroup 偷換整體成績。

## 2:45–3:30 — Leakage / cold-start

開一筆標為 `COLD START` 的新職缺。

講稿：

> 這筆 JD 修改時間晚於 train cutoff。系統仍能用文字搜尋它，但刻意沒有技能邊。這不是功能缺陷，而是我們避免 holdout leakage 的可驗證設計。新技能則走 ephemeral OOV node，不會偷偷寫進 production graph。

## 3:30–4:15 — AWS architecture

畫面：架構圖。

講稿：

> Offline 由 Step Functions 執行 temporal gate、Bedrock batch structured extraction 與 evidence validation；OpenSearch 做 hybrid candidates，Neptune 聚合一跳 graph feature，SageMaker Unbiased LambdaMART 重排。任一服務超時仍回合法 fallback ranking。

## 4:15–4:45 — 商業價值

講稿：

> 排名的商業指標不是漂亮文案，而是 Top-1 view/apply、search-to-apply、query reformulation 與新職缺 time-to-first-qualified-view。我們先 shadow，再 5% A/B，並把 latency 與零結果率當 guardrail。

## 4:45–5:00 — 收尾

> SkillWeave 讓生成式 AI 成為可量測、可消融、可追溯的索引資產；如果移除圖譜不會退化，我們就不宣稱它有價值。

畫面停在 RC4 release 狀態與誠實的 5% gate。公開 AWS、GitHub 與影片 URL
尚未產生，因此影片不顯示 placeholder 或假 QR code。

## 可重現影片

tracked source 位於 `video/pitch-deck.html` 與 `video/scenes.json`：

```bash
python3 scripts/render_demo_video.py
```

輸出 `dist/skillweave-demo-5min.mp4`、繁中 SRT sidecar 與
`reports/demo-video.json`。公開上傳後，才以 `scripts/update_release_urls.py`
登錄真實 HTTPS URL。
