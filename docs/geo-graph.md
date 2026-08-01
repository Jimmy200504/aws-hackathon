# Geo Graph (地理知識圖譜) Implementation Spec

## 1. 專案背景與開發脈絡 (Context)

這是一個為「2026 1111人力銀行 生成式AI黑客松」開發的排序系統模組。本次競賽要求建立一個職缺搜尋排序 API (`POST /api/v1/jobs/search`)，輸入包含搜尋關鍵字 (`query`) 與可選的地點條件 (`location_code`)。

**傳統系統痛點：**
傳統基於樹狀結構的地區代碼表在處理地點限制時，存在兩個極端的缺陷。例如，當求職者搜尋「八里區 餐飲業」但該區剛好無職缺時：
1. 零結果死胡同 (Zero-Result)：若系統採取嚴格過濾，會直接回傳空清單 `[]`，導致求職流程中斷，使用者體驗極差。
2. 粗暴向上回退 (Bad Fallback)：若系統為了避免空結果，採取「退回上一層級」策略擴大範圍至「新北市」，則極有可能會撈出同屬新北市、但實際車程極遠的「汐止區」職缺，提供毫無意義的媒合。

**本模組核心目的：**
為了解決上述「不是沒結果，就是亂推薦」的痛點，我們需要建構一個「In-Memory 地理知識圖譜 (Geo Graph)」。
透過定義「相鄰邊」與「交通捷徑邊」，當八里區沒有職缺時，系統既不會回傳空值，也不會盲目退回新北市全區；而是能沿著圖譜，優先向外擴展到地理與交通距離最近的「淡水區（受惠於淡江大橋通車）」、「林口區」、「五股區」，而非毫無關聯的汐止區。此圖譜計算出的 Graph Distance 也將作為後續 LTR (Learning-to-Rank) 模型的連續性地理空間特徵，實現「優雅降級 (Graceful Degradation)」。

---

## 2. 圖譜層級定義 (Graph Topology)

我們將使用 Python 的 `networkx.DiGraph` 來實作一個 6 層的異質有向圖（包含多重繼承與雙向邊）。

* **L0 (國家 Root)**：台灣
* **L1 (大區/都會區)**：北北基桃、中彰投...
* **L2 (縣市)**：台北市、台中市...
* **L3 (生活圈/次分區)**：台中海線、台北北海岸... (解決 OOV 查詢擴展)
* **L4 (官方行政區)**：八里區、淡水區、信義區... (最常用的職缺綁定節點)
* **L5 (特定聚落/園區)**：竹科園區、七期... (支援多重繼承，如竹科同時屬於新竹市東區與新竹縣寶山鄉)

### 邊 (Edges) 的類型與權重設計：

1. **`is_part_of` (上下層包含)**：由父節點指向子節點。
* *Weight設定：為了避免計算最短路徑時發生「父節點捷徑作弊」，此類邊不參與橫向距離計算（或將 weight 設為 999）。*


2. **`is_adjacent_to` (同層相鄰)**：建立雙向邊。
* *Weight設定：代表基礎開車預估時間（例如 20 分鐘，設為 20）。*


3. **`shortcut` (交通特例捷徑)**：建立雙向邊。
* *Weight設定：反映真實交通建設大幅縮短的時間（例如淡江大橋通車，設為 10）。*



---

## 3. 資料儲存與時序過濾策略 (Storage & Temporal Logic)

為符合黑客松「嚴禁資料洩漏 (Data Leakage)」原則，且測試集資料鎖定在 **2026-06-01 至 2026-06-07**。圖譜必須具備時間校準能力。

資料採「混合法」存於兩個靜態 JSON 檔，由 Python 在記憶體中融合：

### A. `geo_base.json` (基礎底圖)

儲存大批量的基礎相鄰關係與層級關係。

```json
{
  "nodes": [
    {"id": "L4_八里區", "type": "District", "name": "八里區", "code": "1002008"},
    {"id": "L4_林口區", "type": "District", "name": "林口區", "code": "1002015"},
    {"id": "L4_淡水區", "type": "District", "name": "淡水區", "code": "1002016"}
  ],
  "edges": [
    {"source": "L4_八里區", "target": "L4_林口區", "relation": "is_adjacent_to", "weight": 20}
  ]
}

```

### B. `geo_special.json` (特例覆寫檔)

儲存我們手動定義的商業亮點與交通特例。**必須包含 `effective_date` 屬性**。

```json
{
  "edges": [
    {
      "source": "L4_八里區",
      "target": "L4_淡水區",
      "relation": "shortcut",
      "weight": 10,
      "effective_date": "2026-05-12",
      "note": "淡江大橋通車 (在測試期前，需載入)"
    },
    {
      "source": "L4_三峽區",
      "target": "L4_鶯歌區",
      "relation": "shortcut",
      "weight": 5,
      "effective_date": "2026-06-30",
      "note": "三鶯線通車 (測試期後才發生，建圖時應過濾掉)"
    }
  ]
}

```

---

## 4. 給 Claude Code 的開發任務指派

本次你的任務 **僅限於實作 Geo Graph 的建圖與查詢邏輯**，無需實作完整的 API endpoint。請幫我建立一個獨立的 Python 模組 `geo_graph_builder.py`，完成以下功能：

1. **實作 `build_geo_graph(base_path, special_path, cutoff_date)` 函式**：
* 讀取兩個 JSON 檔案並使用 `networkx.DiGraph` 建圖。
* 將 `geo_special.json` 中的邊疊加覆寫。
* **核心邏輯**：必須實作時間過濾。只有當特例邊的 `effective_date` $\le$ `cutoff_date` (預設傳入 `"2026-06-01"`) 時，才將該邊加入圖中。沒有 `effective_date` 的邊視為永遠有效。


2. **實作 `get_expanded_locations(G, source_node, max_distance)` 函式**：
* 輸入一個起點（如八里區）與最大容忍權重/距離。
* 使用 NetworkX 回傳距離範圍內的候選節點列表，並按距離由近到遠排序。


3. **提供簡單的 `__main__` 測試區塊**：
* 在記憶體內模擬寫入包含八里/淡水（淡江大橋）與三鶯線特例的 dummy JSON 資料。
* 展示在 `cutoff_date="2026-06-01"` 時，淡江大橋邊生效、三鶯線邊被過濾的結果。
* 展示查詢「L4_八里區」的擴展結果，驗證淡水區的距離優先於林口區。



---

## 5. (補充說明) 整體 Pipeline 如何使用這個 Graph

此部分僅供你理解整體脈絡，**本次無需實作**：
在未來的評估階段，當後端 API 收到請求時，資料流如下：

1. **Query Expansion (召回期)**：使用者輸入 `location_code = [八里區代碼]`。API 呼叫你寫的 `get_expanded_locations`，利用 Graph 找出 `[八里區, 淡水區, 五股區]`，去資料庫撈出 100 筆初步候選職缺。
2. **Feature Engineering (LTR 排序期)**：針對這 100 筆職缺，計算其所在行政區與「八里區」在 Graph 上的最短距離（Shortest Path Weight）。淡水的職缺特徵值為 10，林口為 20。
3. **ML 降排 (Graceful Degradation)**：LightGBM 模型會依據此距離特徵，給予距離近的淡水職缺較高分數，自然而然將其排在前面，完美解決無職缺時的回退問題。

---
