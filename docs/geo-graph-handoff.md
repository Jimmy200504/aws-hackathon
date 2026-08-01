# Geo Graph 交接說明

作者：timchen
分支：`experiment/llm-graph-in-benchmark`（未推送）
規劃書：[`docs/geo-graph.md`](geo-graph.md)

這份文件交接 geo graph 的前置量測階段。**尚未開始建圖**，做完的是「哪些層能有資料支撐、哪些不能」的量測，以及 L4 行政區的抽取器。

先讀這一節與「資料層面的硬約束」，那裡有三件事會直接推翻規劃書裡的部分設計。

---

## 1. 現況

### 已 commit（本分支領先 `origin/main` 五個 commit，全部未推送）

```
60fc880  Stop the district extractor from tagging the wrong district
4422b75  Measure the geo graph's data support before building it
ec25456  Surface region graph evidence in the search response and demo
909deef  Add behaviour-derived region substitutability subgraph
674e768  Make the generative-AI contribution separately measurable
─────────  origin/main = 0933510
```

### 檔案

| 路徑 | 內容 | 追蹤 |
|---|---|---|
| `scripts/build_region_graph.py` | L2 縣市層行為圖（已完成） | 是 |
| `artifacts/region-graph.json` | 22 縣市、201 條可替代邊、62 條通勤邊 | 是 |
| `app/region_graph.py` | L2 圖的唯讀查詢與 `meta.region_trace` | 是 |
| `scripts/extract_job_districts.py` | L4 行政區抽取器 | 是 |
| `reports/job-district-extraction.json` | 705 個 surface 的逐條判定 + 審查佇列 | 是 |
| `artifacts/job-districts.json` | 255,119 筆職缺的行政區解析，43 MB | **否，gitignore** |
| `scripts/measure_location_code_levels.py` | `c0` 層級分布 + 區級共同勾選 | 是 |
| `reports/location-code-levels.json` | 含八里區的驗證結果 | 是 |
| `scripts/mine_region_aliases.py` | L5 口語地名候選挖掘 | 是 |
| `reports/region-alias-candidates.json` | 2,695 個候選 + 負控制組 | 是 |

### 重建指令

全部用 `.\.venv\Scripts\python.exe`，**不要用 PATH 上的 `python`**（那是 Anaconda 3.8.8，缺 `str.removeprefix`，無法 import 本 repo）。

```powershell
.\.venv\Scripts\python.exe scripts\extract_job_districts.py            # ~75s
.\.venv\Scripts\python.exe scripts\measure_location_code_levels.py     # ~50s
.\.venv\Scripts\python.exe scripts\mine_region_aliases.py              # ~70s
.\.venv\Scripts\python.exe -m unittest discover -s tests               # 91 tests
```

---

## 2. 資料層面的硬約束

這四件事已量測確認，會決定規劃書裡哪些部分做得到。

### 2.1 職缺沒有行政區欄位，L4 只能從文字抽

`職缺.csv` 39 個欄位裡只有 `工作城市` 一個位置欄位，distinct 值 27，**縣市級**。1,211,970 筆可解析到縣市。

規劃書 §2 寫 L4 是「最常用的職缺綁定節點」——在這份資料集裡職缺綁不到 L4，只能綁 L2。L4 必須從 `職務名稱` + `職務內容` 抽取，而那有覆蓋率上限。

### 2.2 L4 覆蓋率上限 26.53%

`scripts/extract_job_districts.py` 實測：

| 量測 | 值 |
|---|---:|
| 職缺總數 | 1,218,635 |
| cutoff 後排除 | 251,258（20.6%） |
| 合格職缺 | 961,780 |
| 抽到行政區 | **255,119（26.53%）** |
| 其中單一行政區 | 221,212（86.71%） |

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

## 3. 規劃書需要修正的四點

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

## 5. 下一步（有序，前兩步互相依賴）

### 步驟 1：LLM collocation 判斷（本次交接的主要待辦）

**問題。** surface 層級的通過/拒絕在數學上無法處理 occurrence 層級的錯誤。`北區` 是最清楚的例子：

```
北區業  n=408  p= 4.66%  → 北區業務部專員，不是行政區
北區和  n=159  p=80.50%  → 北區和緯路四段，台南市北區，是行政區
北區忠  n=146  p=72.60%  → 北區忠明路，台中市北區，是行政區
北區三  n=128  p=85.94%  → 北區三民路，台中市北區，是行政區
```

整體接受會留下 `北區業務`，整體拒絕會丟掉 `和緯`/`忠明`/`三民`。兩者都錯。

**資料已備好。** `reports/job-district-extraction.json` 的 `occurrence_review_queue`：

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

**做法。**

1. 先在 511 筆已標註 collocation 上量 LLM 準確率（這是唯一有答案的部分，不能跳過）
2. 準確率可接受再套用到 2,558 筆需判斷的
3. 判斷結果寫回 artifact，帶 `extractor_model`、`prompt_version`
4. 重跑抽取器，套用 occurrence 層級判定，看單一行政區佔比與覆蓋率怎麼變

**成本。** 2,558 筆，每次批 25 個 → 約 103 次呼叫。**Bedrock 硬限 1 RPS，約 2 分鐘。** 加上標註集驗證約 21 次。

**一個好的測試題。** `林口長庚 n=927 p=18.66%` —— 林口長庚醫院行政上屬**桃園市龜山區**，不是新北市林口區。資料抓到了這件事，而一般模型很可能答錯。

### 步驟 2：L4 區級共同勾選圖

`scripts/measure_location_code_levels.py` 已經算出 2,723 條邊，但目前只寫在報告裡。需要一支 `scripts/build_district_graph.py` 產出與 `region-graph.json` 同構的 artifact：

- `SUBSTITUTABLE_WITH`（無向，Jaccard + 兩向條件機率）
- 節點 key 用 `縣市/行政區`，不要裸區名（`北區` 在台中、台南、新竹都有；`東區` 有四個）
- metadata 記 `dataset_version` / `graph_cutoff` / `schema` / `seed`
- train-only（06-01~06-05），排除匿名 `talentNo = 0`

跨縣市的區對目前沒算（只算同縣市），若要做通勤走廊需要補。

### 步驟 3：L5 表 + 集中度驗證

L5 表還沒給。驗證機制已經驗證過可用：`scripts/mine_region_aliases.py` 的負控制組（`經驗`、`團隊`、`加班` 等十個非地名詞）落在 17.6~26.7%，基準率 17.5%；真實地名 `大墩 97.9%`、`文心 91.8%`、`北車 86.4%`、`南科 81.1%`、`中科 74.0%`、`竹科 60.9%`。

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

### 步驟 4：組裝 geo graph

前三步完成後才有足夠的節點與邊。`docs/geo-graph.md` 的 `build_geo_graph` / `get_expanded_locations` 介面可以照用，但依 §3 修正實作。

---

## 6. 已知未解問題

1. **`桃園_`（後面非漢字）n=8,235、p=78.35% 仍在需判斷帶。** 這是 `桃園)`、`桃園|` 這類，量很大且未解。
2. **L4 跨縣市區對沒算。** 通勤走廊（如新竹市→新北市在 L2 有 163 vs 6）在 L4 沒有對應資料。
3. **`shortcut` 邊無法用這份資料驗證。** 搜尋日誌是 2026-06-01~06-07，規劃書標的淡江大橋通車日是 2026-05-12，**整個資料窗都在通車後**，沒有 before/after 可比。淡水是八里第一名鄰居這件事與大橋已通車一致，但不構成證明。
4. **本分支的 commit 都未推送。**

已解決：`.gitattributes` 缺失導致 `scripts/verify_release.py` 在 Windows 上 19/25 個 hash mismatch。已加入 `* text=auto eol=lf`，驗證器現在 89/89 全過。

---

## 7. 環境與規則

- Python：**必須** `.\.venv\Scripts\python.exe`（3.12.8）。LTR 相關用 `.\.venv-ltr\Scripts\python.exe`（xgboost 3.2.0）
- PowerShell 工具在此環境不穩：`Start-Sleep` 常不生效、輸出常為空、`Exit Code 1` 不代表失敗。長時間腳本用重導向到檔案再讀檔
- `.ps1` 檔勿含中文（PS 5.1 以 ANSI 讀取會壞）
- Bedrock：**限 us-east-1 / us-west-2**，1 RPS。`pipeline/bedrock_extract.py` 預設 `--region ap-northeast-1` 與 `--max-workers 4` 兩者都違規，尚未修
- `bedrock:CreateModelInvocationJob` 不在允許清單，batch inference 不可用
- 探索性報告不要登入 `release-manifest.json`，那會變成發布宣稱
- 不要改 `app/ranker.py` 的 `LLM_SKILL_PREFIX = "bedrock."`，否則 `llm_*` 特徵族會靜默歸零
