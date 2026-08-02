# Data Card

## 來源與範圍

- 提供方：1111 人力銀行
- 行為期間：2026-06-01～2026-06-07
- 用途：職缺搜尋 retrieval/ranking、skill graph、離線行為標籤
- 個資：資料已去識別；`talentNo` 仍視為敏感 pseudonymous identifier，不進公開 artifact

## 實際列數與品質

| 檔案 | 列數 | 觀察 |
|---|---:|---|
| `職缺.csv` | 1,218,635 | 主鍵無重複 |
| `userSearchLog_20260601_20260607.csv` | 6,139,952 | query 無空值；約 22.1% 為匿名 |
| `職缺瀏覽_20260601_20260607.csv` | 8,241,233 | 288,318 個不同職缺；含大量匿名事件 |
| `主動應徵_0601-0607.csv` | 225,999 | 112,972 個不同職缺；無匿名 talent |

職缺缺值：

| 欄位 | 缺值筆數 |
|---|---:|
| 電腦技能資料 | 961,442 |
| 工作技能 | 1,067,372 |
| 職務內容 | 13,490 |
| 職務小類 | 713 |
| 工作城市 | 55 |

## Join contract

```text
職缺.職缺編號 ─┬─ 搜尋.empStr[]
                ├─ 瀏覽.employeeNo
                └─ 應徵.empNo

搜尋.talentNo ── 瀏覽.talentNo ── 應徵.talentNo
城市.CodeNo ── 搜尋.c0[]
職務.CodeNo ── 搜尋.d0[]
```

`empStr`、`c0`、`d0` 都是逗號分隔多值欄位。

## Label policy

提供資料沒有 `session_id`，所以本地 benchmark 使用明確、可重現的近似：

- 匿名 talent 排除。
- 對每次搜尋，只看當次 exposure Top 100。
- view/apply 必須發生在搜尋之後 30 分鐘內。
- `view=1`、`apply=2`，同一 `(query, location, duty, job)` 取最大值。
- 未互動保留為未觀測，不宣稱是真負例。
- validation 使用固定 bucket `[0,200)`；失敗 holdout 為 `[200,400)`；鎖定 confirmation 為 disjoint `[400,1400)`（seed 1111）。
- 最終 qrels groups：train 1,673、validation 497、confirmation 1,993。

這個 qrels 主要用於 reranking ablation，不應被描述為主辦方正式 relevance ground truth。

## Temporal leakage

本地 cutoff 為 `2026-06-05 23:59:59.999`。

- 251,258 筆 JD 的 `職缺最後修改時間` 晚於 cutoff。
- 這些 job 仍可由 lexical cold-start path 搜尋。
- 不可使用 cutoff 後的目前 JD 文字。若 cutoff 前應徵事件含當時的 `empName`，只允許用該 train-time 職稱快照建邊；其餘職缺的 graph 欄位為空。
- Query→Skill/Job 行為圖的 train rows 只讀更早日期 snapshot；validation/test 只讀 06-01～06-05 凍結圖。
- production S3 manifest 與 graph publish step 必須再次檢查 cutoff，不只依賴 ETL 呼叫端。

## 偏差

- Position bias：view/apply 只可能發生在既有系統曝光的職缺，且前排得到更多互動。
- Selection bias：正式 API 不含 `talentNo`，不可把會員行為個人化收益帶進線上評估。
- Freshness bias：新職缺互動時間較短，不能把低互動直接解釋為低相關。
- Missing-not-at-random：技能欄位缺值高度不均；production extractor 會在各欄位做 reviewed exact matching，但不推論未出現技能。未知詞只進 candidate frequency/review artifacts。
- Bot/noise：抽樣高頻 query 出現異常極端值（例如「現領」）；train pipeline 要做 query-rate 與 repeated-session downweight，而不是人工刪除真實求職意圖。

## 公開 artifact 原則

可以提交：

- schema、欄位統計、版本 fingerprint
- 彙總指標
- job-only demo index（須符合主辦授權）

不可公開：

- 原始 CSV
- `talentNo`
- 可還原單一使用者時序的 qrels/session artifact
