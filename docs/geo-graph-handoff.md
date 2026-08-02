# Geo Graph 交接說明

作者：timchen
分支：`experiment/llm-graph-in-benchmark`（未推送）
規劃書：[`docs/geo-graph.md`](geo-graph.md)
Schema 與量測結果：[`docs/graph-schema.md`](graph-schema.md) 的「Geo graph（行政區層）」一節

**狀態：四個步驟全部完成。** 圖已建好、接上 API，L3/L4/L5 三張表都在 repo 裡，
LLM 判斷跑了兩次（一次不採用、一次採用）。本文件第 2～4 節是建圖前的量測，仍然有效，
說明為什麼規劃書的部分設計做不到。

抽取器目前的預設值是 `--place-layers l5` + `--judgement-source labelled`，
產出 **267,306 筆（27.79%）**、單一行政區 86.80%。五個 arm 的比較見
[`reports/place-layer-arms.json`](../reports/place-layer-arms.json)。

---

## 1. 現況

### 分層檔案（四層都可檢視）

| 層 | 檔案 | 內容 | 驗證 |
|---|---|---|---|
| L1 大區 | `config/geo-authored.json` | 7 個 | cohesion（4 過） |
| L3 生活圈 | `config/geo-authored.json` | 25 個 | cohesion（21 過）+ 文字集中度 |
| L4 行政區 | `config/geo-l4-districts.json` | 368 區、705 surface | 誤留率閘門 |
| L5 地標 | `config/geo-l5-table.json` → `geo-l5-published.json` | 666 → 365 | 集中度 + occurrence 過濾 |

`config/geo-l4-districts.json` 是從 `城市對照表.csv` 生成後 commit 的，
只含行政區劃（縣市名、區名、6 位代碼），不含任何職缺／求職者內容，
兩個 loader 都優先讀它。

### 檔案

| 路徑 | 內容 | 追蹤 |
|---|---|---|
| **L2 縣市層（先前完成）** | | |
| `scripts/build_region_graph.py` | L2 縣市層行為圖 | 是 |
| `artifacts/region-graph.json` | 22 縣市、201 條可替代邊、62 條通勤邊 | 是 |
| `app/region_graph.py` | L2 圖的唯讀查詢與 `meta.region_trace` | 是 |
| **L4 行政區層（本次完成）** | | |
| `scripts/build_district_graph.py` | L4 區級行為圖 | 是 |
| `artifacts/district-graph.json` | 368 節點、4,857 條邊（2,202 條跨縣市） | 是 |
| `config/geo-authored.json` | L1/L3/L5 手填層 + `shortcut` 邊 | 是 |
| `scripts/validate_geo_authored.py` | 手填層對行為圖的稽核 | 是 |
| `reports/geo-authored-validation.json` | 28 個分組的逐條判定 | 是 |
| `app/geo_graph.py` | 組裝、Dijkstra、`meta.geo_trace` | 是 |
| `tests/test_geo_graph.py` | geo graph 測試 | 是 |
| **抽取器與待辦** | | |
| `scripts/extract_job_districts.py` | L4 行政區抽取器 | 是 |
| `reports/job-district-extraction.json` | 705 個 surface 的逐條判定 + 審查佇列 | 是 |
| `artifacts/district-collocation-queue.json` | 完整 collocation 佇列（3,069 筆） | 是 |
| `scripts/judge_district_collocations.py` | L4 collocation LLM 判斷（跑完，**不採用模型判定**） | 是 |
| `scripts/build_l4_table.py` / `config/geo-l4-districts.json` | L4 字表 | 是 |
| `scripts/build_l5_table.py` / `config/geo-l5-table.json` | L5 手寫表 666 筆 | 是 |
| `scripts/validate_l5_table.py` / `config/geo-l5-published.json` | 語料驗證後 365 筆 | 是 |
| `scripts/judge_l5_occurrences.py` | L5 occurrence LLM 判斷（**採用**，lift +0.3185） | 是 |
| `scripts/report_place_layer_arms.py` / `reports/place-layer-arms.json` | 五個 arm 比較 | 是 |
| `tests/test_district_collocations.py` | 抽取與判斷測試 | 是 |
| `artifacts/job-districts.json` | 267,306 筆職缺的行政區解析，43 MB | **否，gitignore** |
| `scripts/measure_location_code_levels.py` | `c0` 層級分布 + 區級共同勾選 | 是 |
| `reports/location-code-levels.json` | 含八里區的驗證結果 | 是 |
| `scripts/mine_region_aliases.py` | L5 口語地名候選挖掘 | 是 |
| `reports/region-alias-candidates.json` | 2,695 個候選 + 負控制組 | 是 |

### 重建指令

全部用 `.\.venv\Scripts\python.exe`，**不要用 PATH 上的 `python`**（那是 Anaconda 3.8.8，缺 `str.removeprefix`，無法 import 本 repo）。

```powershell
.\.venv\Scripts\python.exe scripts\build_district_graph.py             # ~46s
.\.venv\Scripts\python.exe scripts\validate_geo_authored.py            # ~15s
.\.venv\Scripts\python.exe scripts\extract_job_districts.py            # ~75s
.\.venv\Scripts\python.exe scripts\measure_location_code_levels.py     # ~50s
.\.venv\Scripts\python.exe scripts\mine_region_aliases.py              # ~70s
.\.venv\Scripts\python.exe scripts\build_l5_table.py                   # 即時
.\.venv\Scripts\python.exe scripts\validate_l5_table.py                # ~90s
.\.venv\Scripts\python.exe -m unittest discover -s tests               # 185 tests
```

完整 collocation 佇列（LLM 步驟的輸入）要另外產，且**不要蓋掉** checked-in 報告：

```powershell
.\.venv\Scripts\python.exe scripts\extract_job_districts.py `
    --review-collocations 100000 --report artifacts\district-collocation-queue.json
```

---

## 2. 資料層面的硬約束

這四件事已量測確認，會決定規劃書裡哪些部分做得到。

### 2.1 職缺沒有行政區欄位，L4 只能從文字抽

`職缺.csv` 39 個欄位裡只有 `工作城市` 一個位置欄位，distinct 值 27，**縣市級**。1,211,970 筆可解析到縣市。

規劃書 §2 寫 L4 是「最常用的職缺綁定節點」——在這份資料集裡職缺綁不到 L4，只能綁 L2。L4 必須從 `職務名稱` + `職務內容` 抽取，而那有覆蓋率上限。

### 2.2 L4 覆蓋率上限（當時 26.53%，現為 27.79%）

`scripts/extract_job_districts.py` 實測：

| 量測 | 值 |
|---|---:|
| 職缺總數 | 1,218,635 |
| cutoff 後排除 | 251,258（20.6%） |
| 合格職缺 | 961,780 |
| 抽到行政區 | **255,119（26.53%）** |
| 其中單一行政區 | 221,212（86.71%） |

這是只掃 L4 的數字。加上 L5 地標層與實測標註的 occurrence 過濾後為
**267,306（27.79%）**、單一行政區 232,021（86.80%），見 `reports/place-layer-arms.json`。

**因此規劃書 §5 的 LTR 距離特徵會在 73% 的候選職缺上缺值**，而且缺值不是隨機的：會在 JD 寫地址的職缺（門市、工廠、物流）與不寫的（辦公室、業務）系統性不同。要處理缺值語意（缺值 ≠ 距離遠）。

### 2.3 `c0` 有 73% 是區級，所以 L4 可以有行為權重

`scripts/measure_location_code_levels.py` 掃 6,139,952 筆搜尋：

| 量測 | 值 |
|---|---:|
| 代碼選取總數 | 6,221,362 |
| 區級 | **4,544,538（73.05%）** |
| 縣市級 | 1,651,491（26.55%） |
| 只用區級的搜尋 | 55.32% |
| 368 個行政區中被選過的 | 366 |
| 同縣市區對 | 3,827 |
| 達 30 次支撐 | **2,723** |

L2 只有 201 條邊，L4 有 2,723 條。**`is_adjacent_to = 20` 這種手填權重沒有必要。**

### 2.4 離線評測量不到檢索擴展

`artifacts/llm-exp-smoke/temporal-eval.json` 的候選集是固定的（來自 `empStr`，舊系統當時實際曝光的清單）：

| 量測 | test | validation |
|---|---:|---:|
| 候選職缺 | 6,620 | 6,702 |
| 屬於非指定縣市 | 7（0.11%） | 75（1.12%） |
| **候選只落在 ≤1 個縣市的 case** | **84.3%** | 83.2% |

「八里沒職缺時撈出淡水職缺」在 benchmark 結構上不會發生。這不代表不該做，但**報告裡不能寫成離線 NDCG 提升**。詳見 [`docs/evaluation-limits.md`](evaluation-limits.md)。

---

## 3. 規劃書需要修正的四點（皆已實作，見 `docs/graph-schema.md`）

### 3.1 不要用 networkx

Repo 規定本機 demo 零依賴、`python3 -m app.server` 可跑，production 只有 8 個套件在 `requirements-production.lock`。

規模上：22 縣市 + 368 區 + 數十個 L5 ≈ 430 節點、約 3,000 條邊。`heapq` 寫 Dijkstra 約 30 行就夠，networkx 換不到任何東西卻要多一個 Lambda 依賴。

### 3.2 權重按 provenance 分層，不要統一手填

| 層 | 權重來源 | 現況 |
|---|---|---|
| L2 縣市對 | 行為（201 + 62 條邊，零人工值） | `artifacts/region-graph.json` 已完成 |
| L4 區對 | 行為（2,723 條，見 §2.3） | **未建，下一步** |
| L5 園區歸屬 | 手填表 + 集中度驗證 | 表未給 |
| `shortcut` | 手填 + `effective_date`，標 `external` | 未建 |

每條邊帶 `provenance: behaviour | authored | external`，消融時能分開關。

手填相鄰表**仍然值得做**，但價值在於它與行為圖的**不一致處**，見 §3.3。

### 3.3 你的八里例子被驗證了，而且有一個你猜錯的地方

`reports/location-code-levels.json` 的 `spec_example_八里區`。八里區 10,702 次搜尋：

| 排名 | 行政區 | 共同勾選 | Jaccard |
|---:|---|---:|---:|
| 1 | **淡水區** | 6,228 | **0.2369** |
| 2 | **五股區** | 6,347 | 0.1305 |
| 3 | 三芝區 | 1,353 | 0.1100 |
| 4 | 蘆洲區 | 6,131 | 0.1100 |
| 5 | 泰山區 | 3,051 | 0.0765 |
| 6 | **林口區** | 2,366 | 0.0699 |
| 14 | **汐止區** | 159 | **0.0048** |

規劃書的兩個核心判斷都對：淡水第一（領先林口 3.39 倍），汐止第 14（比淡水弱 49 倍），「粗暴回退撈出汐止」是真的失敗模式。

**但林口排第六，被三芝、蘆洲、泰山超過，而三芝甚至不與八里接壤（隔著淡水）。** 淡水、五股、林口三個都與八里接壤，Jaccard 卻差 3.4 倍。統一權重兩件都表達不了。

這個矛盾是報告裡最好的素材：**圖譜學到了人工規則學不到的東西。**

### 3.4 `is_part_of` 用集合查詢，不要當加權邊

規劃書用 weight 999 防「父節點捷徑作弊」，顧慮是對的。但 L3（生活圈）的用途是 OOV 擴展，而要走到 L3 節點必須經過 `is_part_of` —— 成本 999 的話 `max_distance` 永遠碰不到 L3。

層級歸屬應該是集合查詢（`members_of(L3)`），橫向距離走 L4/L2 的邊。

另外規劃書範例的節點代碼是 7 位（`1002008`），實際資料集是 6 位（`100217` = 三峽區）。base map 的節點代碼要從 `data/dataset/城市對照表.csv` 生成，不然接不上 `location_code`。

---

## 4. L4 抽取器的設計（接手前要理解的部分）

`scripts/extract_job_districts.py` 有兩個 surface 層與三道守門，都是為了修掉實測到的錯誤。

### 兩個 surface 層

```
full_name       信義區、板橋區          152,835 筆命中
suffix_dropped  信義、板橋              167,758 筆命中
```

省略後綴那層**貢獻比完整區名還多**，因為職稱會寫「信義」不寫「信義區」。`title` 欄位 192,246 筆、`content` 128,347 筆。

### 三道守門

**a. 縣市一致性。** 比對到的 surface 必須在該職缺自己的 `工作城市` 裡有對應行政區，否則丟棄（58,553 筆）。這道擋掉把「越南」「日本」當行政區的錯誤 —— 前一版 artifact 有 3,810 筆被標成國名。

**b. surface 誤留率閘門。** 一個 surface 只有在「保留下來的比對中錯誤比例」的保守上界 ≤ 10% 時才發布（83,674 筆比對因此被丟棄）。

閘門用誤留率而不是相關性統計量，這點很重要，前兩個版本都失敗過：

| 度量 | 失敗方式 |
|---|---|
| `lift ≥ 3.0` | 上限是 1/基準率。`大安區` 橫跨台中+台北（32% 語料），精確度 100% 也到不了 3 倍 |
| `excess ≥ 0.5` | 反向誤殺偏鄉。`雙溪`、`北埔` 在 60~73% 精確度被拒，且 0.001 的差距決定生死 |
| **誤留率 ≤ 10%** | — |

實際判定（`reports/job-district-extraction.json` 有全部 705 個）：

```
鹿野   err<=  0.1%  ACCEPT      林口    err<= 19.1%  送審查
三重   err<=  2.9%  ACCEPT      南區    err<= 20.7%  送審查
北埔   err<=  2.8%  ACCEPT      北區    err<= 62.9%  拒
大安區            ACCEPT      中山    err<= 77.1%  拒
                              中正    err<=150.4%  拒
                              新社區  err<=128.1%  拒
```

被拒的都是真問題：`中山`/`中正` 是全台最常見的路名，`北區` 常指「北部區域」（北區業務、北區門市），`新社區` 是「新建社區」。

小樣本用 Wilson 95% 區間而非固定次數門檻，否則 `鹿野`（18 筆、100% 精確度、基準率 0.36%）這種離島與東部鄉鎮會被系統性排除。共救回 32 個 surface。

**c. 縣市名與縣市限定詞。** 兩個晚期才發現的錯誤：

```
桃園市大園區  →  被標成 桃園區    35,860 筆（surface + 後一字 = 縣市名）
桃園大園      →  同時標 桃園區 與 大園區   7,948 筆（縣市簡稱當限定詞）
```

八個縣市的簡稱剛好等於首府區名：`南投 台東 宜蘭 屏東 彰化 桃園 花蓮 苗栗`。規則只套用在這八個，所以 `中和永和`（新北市兩個區並列）不會被誤縮。

修掉這兩個後，單一行政區佔比從 **80.41% → 84.48% → 86.71%**，覆蓋率只從 26.72% 降到 26.53%。移除 43,808 筆比對只損失 48 筆職缺，是移除重複錯標的特徵。

---

## 5. 四個步驟（3 已完成，步驟 1 待跑）

**原本這裡寫「前兩步互相依賴」，那是錯的，實作時發現了。**
步驟 1 用的是**職缺文字**（職缺側），步驟 2 用的是**搜尋日誌的 `c0`**（查詢側），
兩者資料來源不相交，抽取器準不準完全不影響共同勾選圖。真正交會的地方在步驟 4 之後，
也就是要把職缺綁到區、算區級 `COMMUTES_TO` 的時候（見 §6 第 2 點）。

實務上差很多：步驟 2 零依賴、零成本、不需要憑證，而且它就是圖本身；
步驟 1 要花額度、受 1 RPS 限制、且可能量出「準確率不夠不能用」。
先做 2 再做 1，最壞情況仍有一張完整的圖。本次即照此順序執行。

### 步驟 1：LLM collocation 判斷 —— **已跑完，結論是不採用模型判定**

完整分析見 [`docs/evaluation-limits.md`](evaluation-limits.md) 的「行政區 occurrence 判斷」一節，
證據檔 [`reports/district-collocation-effect.json`](../reports/district-collocation-effect.json)。

摘要：

- 第一版 prompt 在標註集只有 **66.54%**，被 `--min-accuracy` 擋下，沒有進到套用階段
- 改寫 + dev/holdout 切分後，holdout **87.18%**（dev 89.50%），過關
- 套用到 2,558 筆中間帶才發現分數不能外推：標註帶的 place/not_place 加權精確度分離度 **0.8314**，
  中間帶只有 **0.0872**。標註帶是用誤留率切出來的，依定義可分；中間帶沒標註正是因為分不開
- 模型把 `北區和緯`（實測 0.8050）判成 not_place —— **在 occurrence 層重現了 surface 層的錯誤答案**
- **採用**：511 筆實測標註驅動的 occurrence 過濾（單獨計 255,119 → **256,014** 筆、26.53% → **26.62%**；
  與 L5 層合併後的最終值見 §1）
- **不採用**：2,558 筆模型判定（加上去覆蓋率掉到 26.45%）

根因不是提示詞：collocation 的 key 只有一個後續漢字，`南區)` 這四個字裡沒有可判定的資訊。
要解中間帶需要換特徵（整段職缺文字），不是換模型。

**預設沒有開啟。** `--collocation-judgements` 旗標預設關閉，
`reports/job-district-extraction.json` 與所有引用它的數字都未變動。
要套用實測標註那一版：

```powershell
.\.venv\Scripts\python.exe scripts\extract_job_districts.py `
    --collocation-judgements artifacts\district-collocations.jsonl
```

<details>
<summary>原本的規劃（保留作為脈絡）</summary>

**問題。** surface 層級的通過/拒絕在數學上無法處理 occurrence 層級的錯誤。`北區` 是最清楚的例子：

```
北區業  n=408  p= 4.66%  → 北區業務部專員，不是行政區
北區和  n=159  p=80.50%  → 北區和緯路四段，台南市北區，是行政區
北區忠  n=146  p=72.60%  → 北區忠明路，台中市北區，是行政區
北區三  n=128  p=85.94%  → 北區三民路，台中市北區，是行政區
```

整體接受會留下 `北區業務`，整體拒絕會丟掉 `和緯`/`忠明`/`三民`。兩者都錯。

**資料已備好，但不在 checked-in 報告裡。** `reports/job-district-extraction.json` 的
`occurrence_review_queue` 每個 surface 只留前 30 筆 collocation，所以它有**計數**、沒有**列**
（實際只有 1,159 / 3,069 筆，需判斷的只有 740 / 2,558）。完整佇列要用
`--review-collocations 100000` 另外產到 `artifacts/district-collocation-queue.json`，
該檔已 commit，計數與下表完全相符。

| 量測 | 值 |
|---|---:|
| 佇列 surface 數 | 40 |
| 預估誤留 | 12,217 |
| collocation 總數 | 3,069 |
| 已標註 place | 365 |
| 已標註 not_place | 146 |
| **需要語意判斷** | **2,558** |

collocation 的 key 是 surface 後面**一個**漢字，每筆附 `example` 片段供 prompt 使用。

**標註帶是保守的，刻意留出中間帶。** `place` 需誤留率 ≤ 3%、`not_place` 需 ≥ 50%、且支撐 ≥ 30 筆。中間帶不標，因為 `北區和緯` 誤留率 12% 而它是真街道 —— 用單一 10% 門檻會把它標成負例，然後 LLM 答對反而被扣分。

**做法。程式已寫好在 `scripts/judge_district_collocations.py`，只差憑證。**

```powershell
# 1. 先量準確率（21 次呼叫，約 30 秒）。低於 --min-accuracy 會 exit 非零，
#    所以沒過關的模型不可能被拿去跑步驟 2。
.\.venv\Scripts\python.exe scripts\judge_district_collocations.py `
    --queue artifacts\district-collocation-queue.json --mode validate

# 2. 過關才跑（103 次呼叫，約 2 分鐘）
.\.venv\Scripts\python.exe scripts\judge_district_collocations.py `
    --queue artifacts\district-collocation-queue.json --mode apply
```

`--dry-run` 會印出 system prompt 與第一批的實際內容，不呼叫 Bedrock。

已內建的守則：
- prompt 只給 `surface` / `following` / `example`，**不給** `label` 與 `precision`（那是答案），有測試把關
- cache 是 append-only JSONL，帶 `mode` 欄位，validate 的列永遠不會被當成 apply 的產出
- 憑證過期可換一組接著跑，已成功的不會重複計費
- 1.05 秒間隔（沿用 `scripts/normalize_eval_queries.py` 實測 0.7604 RPS 的限流器）
- `--region` 不是 us-east-1 / us-west-2 直接拒絕，不會跑到一半才炸

**一個好的測試題。** `林口長庚 n=934 p=19.06%` —— 林口長庚醫院行政上屬**桃園市龜山區**，不是新北市林口區。
它落在**實測標註**帶（not_place），所以模型答對與否不影響結果。
**行為圖獨立地證實了同一件事**：`林口區 ↔ 龜山區` 的 Jaccard 是 0.1978、共同勾選 10,723 次，
是全部跨縣市邊的第三強。

</details>

### 步驟 2：L4 區級共同勾選圖 —— **已完成**

`scripts/build_district_graph.py` → `artifacts/district-graph.json`。
與原本規劃的差別：**跨縣市區對也算了**，這是原本列在 §6 的未解問題。

| 量測 | 值 |
|---|---:|
| 節點 | 368 |
| 邊（`min_co_selected = 30`） | 4,857 |
| 　同縣市 | 2,655 |
| 　跨縣市 | 2,202 |

先前報告的 2,723 條同縣市邊與現在的 2,655 差在新增的 `--max-districts-per-search 10`：
49 筆一次勾選十個以上行政區的搜尋被排除，它們表達的是「哪裡都行」而非可替代性，
卻會貢獻 30,668 個配對。排除比例 0.005% 的搜尋，影響 0.33% 的配對證據。

schema 與八里例子的完整結果見 [`docs/graph-schema.md`](graph-schema.md)。

### 步驟 3：L5 表 + 集中度驗證 —— **已完成**

L5 表在 `config/geo-authored.json`，連同 L1 大區與 L3 生活圈。
**而且手填層現在被行為圖稽核**（`scripts/validate_geo_authored.py`）：組內平均 Jaccard 對上
「同縣市同樣大小的隨機分組」的虛無分布，可窮舉時窮舉。28 個分組裡 13 個通過、8 個沒通過、7 個單一行政區無法測。

沒通過的留著不刪 —— 那是最有資訊量的部分。`L5/中科` 落在第 61 百分位，因為后里園區與西屯園區的
Jaccard 只有 0.0097；`L1/宜花東` 第 81 百分位，宜蘭花蓮台東不是一個勞動市場。

驗證機制原本就驗證過可用：`scripts/mine_region_aliases.py` 的負控制組（`經驗`、`團隊`、`加班` 等十個非地名詞）落在 17.6~26.7%，基準率 17.5%；真實地名 `大墩 97.9%`、`文心 91.8%`、`北車 86.4%`、`南科 81.1%`、`中科 74.0%`、`竹科 60.9%`。

閘門也擋掉三個 LLM 會答錯的：

```
內科  25.8%   職缺文字裡多數是醫療科別，不是內湖科技園區
公益  44.2%   公益活動、公益性質
嘉南  46.5%   嘉南藥理大學、嘉南平原
```

`竹科 60.9%` 支持規劃書的多重繼承設計 —— 它**不該**是 100%，因為竹科橫跨新竹市東區與新竹縣寶山鄉。

**注意：集中度是多縣市別名的錯誤測試。** `北北基 41.2%`、`桃竹苗 36.0%`、`雲嘉南 43.8%` 低集中度是**應然**，因為詞本身跨縣市。那些要用 L2 共同勾選圖驗證，不是集中度。

已挖到可用的 L5 候選（附出現次數與證據）：

```
樹谷    99 筆  台南市 100%   新市區堤塘港路 (樹谷園區)
頂崁   176 筆  新北市 100%   頂崁工業區
大發   164 筆  高雄市 100%   大發廠
利澤   121 筆  宜蘭縣 100%   蘇澳鎮頂強路(利澤工業區)
南勢角                       中和，捷運站
```

但 `reports/region-alias-candidates.json` 的 2,695 個候選裡**約兩成是正則邊界碎片**（`區西盛`、`往後`、`過這一`），另有相當比例是路名層級（`內環北`、`五工三`）。真正可用的約一成。挖掘來源是職缺文字，那告訴你職缺在哪，不告訴你求職者會打什麼 —— 打字搜尋含地標名的只有 0.198%。

### 步驟 4：組裝 geo graph —— **已完成**

`app/geo_graph.py`。規劃書的 `build_geo_graph(base, special, cutoff_date)` 與
`get_expanded_locations(G, source, max_distance)` 兩個介面都照用了，實作依 §3 修正：
無 networkx、成本是 `-log(可替代度)`、層級是集合查詢。

`meta.geo_trace` 與 `meta.region_trace` 並存，`applied_to_ranking: false`，
搜尋代碼是縣市級時整個鍵省略。`/health` 有 `geo_graph` 欄位。

**尚未做，且刻意不做：** 沒有接進排序路徑，也沒有做成 LTR 特徵。理由見
[`docs/graph-schema.md`](graph-schema.md) 的「已知限制」——
候選集 84.3% 只落在 ≤1 個縣市，區級特徵在候選組內變異數為零；
加上職缺側只有 27.79% 有行政區，缺值語意無法在這個 benchmark 上驗證。

---

## 6. 已知未解問題

1. **`桃園_`（後面非漢字）n=8,235、p=78.35% 仍在需判斷帶。** 這是 `桃園)`、`桃園|` 這類，量很大且未解。
2. **L4 區級 `COMMUTES_TO` 沒做。** 應徵流向需要知道職缺在哪一區，而職缺只有 27.79% 解析得到，
   且缺值非隨機。這條要等步驟 1 的 LLM 判斷提高抽取品質之後才有意義 —— 那是這兩步唯一真正的依賴關係。
3. **`shortcut` 邊無法用這份資料驗證。** 搜尋日誌是 2026-06-01~06-07，規劃書標的淡江大橋通車日是 2026-05-12，**整個資料窗都在通車後**，沒有 before/after 可比。
   實作上已處理：手填邊是備援不是覆寫，兩條 `shortcut` 都落在行為已涵蓋的區對上，
   所以時序過濾器可證明會動（三鶯線在 06-01 被排除、07-01 被接受），但不改變任何一條邊的權重。
4. **中間帶 2,558 筆 collocation 仍未解。** LLM 已試過並量測，分離度只有 0.087（見 §5 步驟 1）。
   下一次嘗試要換特徵而不是換模型：給整段職缺文字或地址欄位，而不是 surface 後面一個漢字。
5. **已解決：** 實測標註版 occurrence 過濾與 L5 地標層都已設為預設，
   最終為 267,306 / 27.79% / 86.80%（兩個指標都優於原本的 255,119 / 26.53% / 86.71%）。
   模型判定的中間帶仍未採用，需要時用 `--judgement-source all` 開啟。
6. **本分支的 commit 都未推送。**

已解決：
- `.gitattributes` 缺失導致 `scripts/verify_release.py` 在 Windows 上 19/25 個 hash mismatch。已加入 `* text=auto eol=lf`，驗證器現在 89/89 全過。
- **L4 跨縣市區對**（原第 2 點）已在 `build_district_graph.py` 補上，2,202 條。

---

## 7. 環境與規則

- Python：**必須** `.\.venv\Scripts\python.exe`（3.12.8）。LTR 相關用 `.\.venv-ltr\Scripts\python.exe`（xgboost 3.2.0）
- PowerShell 工具在此環境不穩：`Start-Sleep` 常不生效、輸出常為空、`Exit Code 1` 不代表失敗。長時間腳本用重導向到檔案再讀檔
- `.ps1` 檔勿含中文（PS 5.1 以 ANSI 讀取會壞）
- Bedrock：**限 us-east-1 / us-west-2**，1 RPS。`pipeline/bedrock_extract.py` 預設 `--region ap-northeast-1` 與 `--max-workers 4` 兩者都違規，尚未修
- `bedrock:CreateModelInvocationJob` 不在允許清單，batch inference 不可用
- 探索性報告不要登入 `release-manifest.json`，那會變成發布宣稱。`district-graph.json` 與
  `geo-authored-validation.json` 都**沒有**登入，`app/geo_graph.py` 也不進 release gate
- 不要改 `app/ranker.py` 的 `LLM_SKILL_PREFIX = "bedrock."`，否則 `llm_*` 特徵族會靜默歸零
- `scripts/package_lambda.py` 不打包 `region-graph.json`，也不打包 `district-graph.json`。
  兩個模組缺檔時都自動停用、搜尋照常回應，這是既有設計，改動會動到已通過驗證的部署 hash
- bash 工具下 Python 的 stdout 是 cp950，中文會變亂碼。要看中文輸出就設 `PYTHONIOENCODING=utf-8`，
  或寫進檔案再讀
