# Geo Graph 交接說明（第二階段）

作者：timchen
分支：`experiment/llm-graph-in-benchmark`（未推送，領先 `origin/main` 19 個 commit）
前一份交接：[`docs/geo-graph-handoff.md`](geo-graph-handoff.md)（建圖前的量測，仍然有效）
規劃書：[`docs/geo-graph.md`](geo-graph.md)
Schema 與完整量測：[`docs/graph-schema.md`](graph-schema.md) 的「Geo graph（行政區層）」一節

**這份文件交接的是「圖建好了，但還沒被用起來」的狀態。**
圖、四層地名表、職缺端抽取全部完成並 commit；缺的是把它們接到服務路徑上。

先讀第 3 節（三個缺口），那是接下來要做的全部內容。第 5 節是不要踩的坑。

---

## 1. 完成了什麼

| 項目 | 狀態 | 產出 |
|---|---|---|
| L2 縣市層行為圖 | 完成 | `artifacts/region-graph.json`（22 縣市、201 + 62 邊） |
| **L4 區級行為圖** | 完成 | `artifacts/district-graph.json`（368 節點、4,857 邊，2,202 條跨縣市） |
| **地理相鄰圖** | 完成 | `config/geo-adjacency.json`（888 條手繪邊 + commute 等級） |
| **四層地名表** | 完成 | 見第 2 節 |
| **職缺端抽取** | 完成 | 267,306 筆（27.79%），key 是職缺編號 |
| **圖的組裝與查詢** | 完成 | `app/geo_graph.py`，stdlib Dijkstra |
| `meta.geo_trace` | **已上線** | 查詢文字解析 + 候選擴充，見第 3 節缺口 2／3 |
| 職缺側 join key | 完成 | `artifacts/demo-job-districts.json`（側車，4.78% 覆蓋） |
| 候選擴充 | 完成（embedded） | OpenSearch 路徑待 mapping + 重建索引 |
| LLM 判斷 | 跑完兩次 | 一次不採用、一次採用，見第 4 節 |

> 本文交接時是 197 個測試 / `verify_release` 89/89。之後的工作把兩者推到 368 / 94，
> 第 3 節的缺口 2 與缺口 3 已經完成，內容已就地更新；第 5 節列的
> `pipeline/bedrock_extract.py` 違規項因 main 刪除該檔而失效。

### 這張圖跟規劃書的差別

規劃書要 networkx、手填分鐘數權重、`is_part_of` 權重 999。三個都改了，理由在
[`docs/graph-schema.md`](graph-schema.md)，摘要：

- **不用 networkx** —— 368 節點、4,940 邊，`heapq` Dijkstra 三十行，production 依賴維持 8 個
- **成本是 `-log(可替代度)`** —— 路徑成本相加恰好等於可替代度相乘，全程沒有分鐘數
- **層級是集合查詢** —— 權重 999 會讓任何 `max_distance` 都走不到 L3

---

## 2. 檔案在哪

### 地名表（四層，全部在 repo）

| 層 | 檔案 | 內容 | 產生方式 | 驗證 |
|---|---|---|---|---|
| L1 大區 | `config/geo-authored.json` → `regions` | 7 個 | 手寫 | cohesion（4 過） |
| L3 生活圈 | `config/geo-authored.json` → `living_areas` | 25 個 | 手寫 | cohesion（21 過）+ 文字集中度 |
| L4 行政區 | `config/geo-l4-districts.json` | 368 區、357 完整名 + 348 省略後綴 | `scripts/build_l4_table.py` | 誤留率閘門 |
| L5 地標 | `config/geo-l5-table.json` | 666 筆（手寫來源） | `scripts/build_l5_table.py` | — |
| L5 已發布 | `config/geo-l5-published.json` | **365 筆**（346 過閘門 + 19 靠 LLM 回收） | `scripts/validate_l5_table.py` | 集中度 ≥0.60 |

**L3 只有 `geo-authored.json` 一個擁有者。** 它曾經同時定義在兩個檔案裡，兩個驗證器對
`北海岸` 給出相反判定，圖還註冊了兩次。`build_l5_table.py` 現在是**從 `geo-authored.json` 讀**，
不要再在別的地方定義 L3。

### 程式

| 檔案 | 做什麼 | 執行時間 |
|---|---|---|
| `scripts/build_district_graph.py` | 搜尋日誌 `c0` → L4 行為圖 | ~46s |
| `scripts/build_l4_table.py` | 城市對照表 → L4 字表 | 即時 |
| `scripts/build_l5_table.py` | 手寫來源 → L5 表 | 即時 |
| `scripts/validate_l5_table.py` | 掃語料驗證 L5，決定發布哪些 | ~90s |
| `scripts/judge_l5_occurrences.py` | L5 occurrence LLM 判斷（**採用**） | ~4min |
| `scripts/build_geo_adjacency.py` | 手繪相鄰圖 | 即時 |
| `scripts/validate_geo_adjacency.py` | 相鄰圖 vs 行為圖交叉檢驗 | 即時 |
| `scripts/validate_geo_authored.py` | L1/L3 分組對行為圖稽核 | ~15s |
| `scripts/extract_job_districts.py` | 職缺文字 → 行政區 | ~75s |
| `scripts/judge_district_collocations.py` | L4 collocation LLM 判斷（**不採用**） | ~2min |
| `scripts/measure_location_cue_ceiling.py` | 覆蓋率天花板量測 | ~90s |
| `app/geo_graph.py` | 組裝、Dijkstra、`meta.geo_trace` | — |

### 報告（證據檔）

```
reports/geo-adjacency-validation.json     相鄰 vs 行為的三個交叉結果
reports/geo-authored-validation.json      L1/L3/L5 分組的 cohesion 判定
reports/l5-table-validation.json          666 → 365 的逐筆判定與理由
reports/l5-occurrence-judgement.json      LLM 回收 17 個 surface，lift +0.3185
reports/district-collocation-effect.json  LLM 中間帶失敗的完整診斷
reports/place-layer-arms.json             五個抽取 arm 的比較
reports/location-cue-ceiling.json         覆蓋率天花板：98.19% 的未解析職缺沒有任何地點線索
reports/job-district-extraction.json      705 個 surface 的判定 + 審查佇列
```

### 重建指令

**必須用 `.\.venv\Scripts\python.exe`**，不要用 PATH 上的 `python`（那是 Anaconda 3.8.8，缺
`str.removeprefix`，無法 import 本 repo）。

```powershell
.\.venv\Scripts\python.exe scripts\build_l4_table.py
.\.venv\Scripts\python.exe scripts\build_district_graph.py
.\.venv\Scripts\python.exe scripts\build_geo_adjacency.py
.\.venv\Scripts\python.exe scripts\validate_geo_adjacency.py
.\.venv\Scripts\python.exe scripts\build_l5_table.py
.\.venv\Scripts\python.exe scripts\validate_l5_table.py
.\.venv\Scripts\python.exe scripts\validate_geo_authored.py
.\.venv\Scripts\python.exe scripts\extract_job_districts.py
.\.venv\Scripts\python.exe -m unittest discover -s tests    # 197 tests
```

---

## 3. 三個缺口（接下來要做的）

### 缺口 1：`resolve_alias()` 有零個呼叫者 —— 建議先做這個

`app/geo_graph.py` 的 `resolve_alias(surface)` 可以把地名字串解析成行政區：

```python
resolve_alias("竹科")   -> ('新竹市/東區', '新竹縣/寶山鄉')
resolve_alias("北海岸") -> ('新北市/淡水區', '三芝區', '石門區', '金山區', '萬里區')
resolve_alias("南崁")   -> ('桃園市/蘆竹區', '龜山區', '桃園區')
```

**全 repo 沒有任何程式呼叫它。** 使用者在 `query` 欄位打「竹科」「北海岸」，這條路徑完全沒被走到。

要做的是：搜尋請求進來時，對 `query` 文字比對 L5/L3 別名，命中就把解析出的行政區放進
`meta.geo_trace`（或新的 `meta.place_trace`）。

**注意：`requires_occurrence_filter: true` 的 19 筆不能用在職缺文字上，但可以用在 query 上。**
`config/geo-l5-published.json` 每筆都有這個旗標，語意寫在檔案的 `rescued.meaning`：

> safe to resolve from a user's query, where the searcher means the place;
> unsafe as a bare substring match over job text without running the occurrence filter first

所以 query 端可以用全部 365 筆，職缺端只能用 346 筆。`scripts/extract_job_districts.py`
已經照這個規則排除，不要在 query 端也跟著排除。

### 缺口 2：職缺端的行政區從來沒進到 `app/` —— **已補**

原本的狀況：`artifacts/job-districts.json` 有 267,306 筆 `職缺編號 → 行政區`，但 consumer
全部在 `scripts/`，`artifacts/demo-index.json` 的職缺欄位只有 `city`（縣市級）沒有 district，
所以圖說「八里 → 淡水」而排序器不知道哪些職缺在淡水。

補法**不是**加進 `scripts/build_demo_index.py`（本文原本的建議）。`artifacts/demo-index.json`
被 `release-manifest.json` 的 `sha256` 釘住，改它的欄位會作廢一個已發布的 hash，
並強迫每一項下游確認重跑。改用側車：

- `scripts/build_demo_job_districts.py` → `artifacts/demo-job-districts.json`（37 KB，已 commit）
- `app/ranker.py` 以 `job_districts` 載入，並比對 `index_version`，
  側車若是為別的索引建的就整份忽略 —— 用錯 job id 去標註比不標註更糟
- 覆蓋率：demo index 574／12,000 = **4.78%**（全量是 27.79%）。未標註的職缺行為完全不變

### 缺口 3：真的用擴展去改候選集 —— **已做，但只在 embedded 路徑**

本文原本寫「這個是刻意不做的」，理由是離線 benchmark 量不到。**那個理由仍然成立，
而且結論已經改成：做，但不宣稱 lift。** 兩件事是分開的。

`GeoExpansion.substitutability` 交給 `app/ranker.py`，對圖背書的行政區免除跨區扣分
（`location` 由 `-16.0` 變 `0.0`）。細節與已知限制見
[`docs/graph-schema.md`](graph-schema.md) 的「候選擴充」一節。三個要點：

- **只免除扣分，不加分。** `location` 停在 LambdaMART 訓練時見過的取值集合內，**不需要重訓**。
  可替代度以 `geo_substitutability` 記錄但權重為零。
- **`applied_to_ranking` 現在會回報 `true`**，`offline_lift_measured` 恆為 `false`。
  本文原本擔心的「不能寫成離線 NDCG 提升」已經在 payload 裡用欄位釘住，不是靠自律。
- **OpenSearch 路徑仍然接不上**，因為線上索引沒有 district 欄位
  （`mapping()` 是 `"dynamic": False`）。那條路徑上排序器回報 `geo_applied: false`。
  本文說「必須在全量檢索路徑做」是對的，那仍然是待辦：需要 mapping 加欄位 + 重建索引 +
  把側車擴到全量。

可示範的案例：「林口區 作業員」。林口區最近的替代區是桃園市/龜山區（0.198），跨縣市，
新北市的縣市過濾在結構上讓它不可見；擴充打開後 demo 第 8 筆就是龜山區的
「【林口半導體廠】-作業員」。

---

## 4. 兩次 LLM 實驗（一敗一成，兩個都要保留）

這是報告裡最有價值的一段，因為兩個實驗只差在**特徵**，同一個模型、同樣 1 RPS。

| | 特徵 | 結果 | 採用 |
|---|---|---|---|
| L4 collocation | 地名後面**一個字** | 分離度 **0.087** | ❌ |
| L5 occurrence | 前後各 **40 字**原文視窗 | lift **+0.3185** | ✅ 回收 17 個 surface |

**失敗那次的細節值得讀**（`reports/district-collocation-effect.json`）：
第一版 prompt 66.54% 被閘門擋下；改寫後 holdout 87.18% 過關；
**但套用到真正要判的中間帶才發現分數不能外推** —— 標註帶是用誤留率切出來的，
依定義可分（分離度 0.8314），中間帶沒標註正是因為分不開（0.0872）。

結論寫在 `docs/evaluation-limits.md`：**通過驗證集不等於能用。**

成功那次的邊界同樣重要：**occurrence 過濾修得了「詞義歧義」，修不了「地點認定錯誤」。**
`青埔` 我原本填高雄捷運站，職缺文字裡 97% 指桃園青埔 —— 模型正確地說「這是地名」，
但一致率沒升，因為錯的是表不是詞義。那類只能改表。

---

## 5. 不要踩的坑

### 環境

- Python **必須** `.\.venv\Scripts\python.exe`（3.12.8）；LTR 相關用 `.\.venv-ltr\Scripts\python.exe`
- **bash 工具下 Python 的 stdout 是 cp950，中文會變亂碼。** 設 `PYTHONIOENCODING=utf-8`，
  或寫進檔案再讀
- PowerShell 在此環境不穩：`Start-Sleep` 常不生效、輸出常為空、`Exit Code 1` 不代表失敗
- `.ps1` 檔勿含中文（PS 5.1 以 ANSI 讀取會壞）

### Bedrock

- **限 us-east-1 / us-west-2，1 RPS。** `scripts/judge_l5_occurrences.py` 有合規的 1.05 秒限流器可抄
- `pipeline/bedrock_extract.py` 預設 `--region ap-northeast-1` 與 `--max-workers 4` **兩者都違規**，尚未修
- `bedrock:CreateModelInvocationJob` 不在允許清單，batch inference 不可用
- Workshop Studio 憑證會過期，寫在 `~/.aws/credentials` 的 `[default]`

### 這個 repo 的紀律（違反會讓評審抓到）

1. **手填權重永遠不覆寫量到的權重。** shortcut 與相鄰圖都遵守這條。
   相鄰邊的權重是「同類型、有行為邊的配對的 Jaccard 中位數」，不是填的
2. **`impassable` 的邊一條都不能產生。** 沒有行為邊可校準 → 山脈不會被補成通路
3. **探索性報告不要登入 `release-manifest.json`**，那會變成發布宣稱。
   geo graph 相關的 artifact 全部**沒有**登入，`app/geo_graph.py` 也不進 release gate
4. **不要改 `app/ranker.py` 的 `LLM_SKILL_PREFIX = "bedrock."`**，否則 `llm_*` 特徵族靜默歸零
5. **`scripts/package_lambda.py` 不打包 `region-graph.json` 也不打包 `district-graph.json`。**
   兩個模組缺檔時都自動停用、搜尋照常回應，這是既有設計，改動會動到已驗證的部署 hash
6. 改動 `reports/job-district-extraction.json` 的數字會連動多份文件，
   目前是 **267,306 / 27.79% / 86.80%**

### 已知未解

1. **中間帶 2,558 筆 collocation 仍未解。** 下次嘗試要換特徵（整段職缺文字），不是換模型
2. **L4 區級 `COMMUTES_TO` 沒做。** 需要職缺側的區，等缺口 2 補上才有意義
3. **`shortcut` 邊無法用這份資料驗證。** 資料窗整段都在淡江大橋通車後，沒有 before/after
4. **覆蓋率接近天花板。** `reports/location-cue-ceiling.json`：未解析的職缺裡 **98.19%
   沒有任何地點線索**，可回收的只有 1.81%。不要再投資在擴大地名表上
5. **本分支 19 個 commit 全部未推送**

---

## 6. 建議順序

```
1. 缺口 1（query 端地名解析）   無阻擋，demo 最直觀
2. 缺口 2（district 進 demo-index）  無阻擋，與缺口 1 合起來才有完整故事
3. 缺口 3                        刻意不做，除非改在全量檢索路徑上
```

單做缺口 1 能展示的是「打竹科 → 系統知道那是東區 + 寶山鄉」，這本身已經是可展示的
OOV 解析，但撈不出職缺。缺口 1 + 2 才構成規劃書 §1 那個「八里沒職缺不會回傳空清單」的完整情境。
