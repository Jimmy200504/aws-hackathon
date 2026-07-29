# 決賽提交稽核

## 必交

- [x] 本機 Live Demo：完整搜尋路徑
- [x] AWS 上可公開存取的 Demo URL
- [x] 5 分鐘繁中錄影 artifact（1080p、語音、內嵌字幕、SHA-256）
- [x] 公開可觀看的 Demo video URL
- [x] 生成式 AI 方法與設計依據文件
- [x] 真實 Amazon Bedrock train-only pilot（200 records、180 accepted、
  1,598 validated mentions、0 fatal）
- [x] 已知 LLM 失敗模式與防護
- [x] 數據與資料應用說明
- [x] 系統功能說明
- [x] Graph schema
- [x] 至少一個 traversal / aggregation trace
- [x] AWS 部署架構圖
- [x] 實際 AWS 部署驗證
- [x] 公開 GitHub URL
- [x] 環境設定
- [x] 執行範例
- [x] Benchmark 重現步驟
- [x] Random seed
- [x] 資料／模型／索引版本
- [x] Release manifest 與 artifact SHA-256
- [x] 依賴鎖定策略（demo 無第三方 runtime dependency；container tag 固定）
- [x] 有圖譜／無圖譜一鍵 ablation
- [x] NDCG@10、MRR、Hit@1、Hit@10、Precision@10 報告
- [x] Position bias correction 狀態明示
- [x] Kiro session、tracked task、review 與機器報告證據
- [x] Aggregate-only graph coverage／subgroup report 與 post-hoc guardrail
- [x] Portable deterministic Lambda bundle、單指令 release gate、GitHub Actions
- [x] SAM validate/build + Python 3.13 arm64 local invoke
- [x] 商業 case、A/B 設計、規模換算與無營收宣稱 guardrail
- [x] Live graph toggle 確實改變排序且 graph contribution 非零

## 品質 gate

- [ ] Bedrock 完整 train-only graph 已產出
- [x] Bedrock bounded train-only pilot 已產出且 aggregate report 可驗證
- [x] NDCG@10 相對 baseline ≥5%（primary +5.72%、replication +5.07%）
- [x] Paired CI 不支持明顯退化（兩次 confirmation CI 全為正）
- [x] Unbiased LambdaMART 已訓練並保存 model manifest
- [x] 模型鎖定後的兩個互斥 confirmation holdouts 已跑
- [ ] 主辦方統一 evaluation script 已跑
- [x] Compact container：50 requests / concurrency 10，50/50 HTTP 200，p95 1.76 s
- [x] AWS production p95 / timeout / concurrency 負載測試（30 requests、concurrency 5、30/30 HTTP 200、p95 4.40 s）
- [x] OOV、新職缺、空結果、未知 code 測試
- [x] AWS URL 從無 AWS session 的 public HTTPS client 可開
- [x] Compact demo 無 WAF；bounded concurrency 5 未被擋，評審不需登入
- [x] Demo video 版本與 release tag 相同
- [x] 公開 URL 上傳後以 unauthenticated HTTP 驗證 200 與完整影片 bytes

## 決賽前 contract smoke

```bash
curl -i -X POST "$DEMO_URL/api/v1/jobs/search" \
  -H 'content-type: application/json' \
  -d '{"query":"後端工程師"}'

curl -i -X POST "$DEMO_URL/api/v1/jobs/search" \
  -H 'content-type: application/json' \
  -d '{"query":"後端工程師","location_code":["100100"],"duty_code":["140200"]}'
```

人工確認：

- HTTP 200
- 至少 10 筆（資料有足夠結果時）
- rank 1 起連續
- job ID 皆存在職缺主檔
- 無 duplicate
- p95 合理
- 未知 code 不造成 500
- query only 可執行
- 空 query 回明確 client error

## 不可在簡報宣稱

- 把內部時間切分 qrels 說成主辦方官方 holdout
- 只展示成功 confirmation、隱藏第一個失敗 holdout
- bootstrap fixture 是 Bedrock 產物
- 內部 30 分鐘 attribution 是正式 relevance ground truth
- 尚未部署的完整 OpenSearch／Neptune／SageMaker production path
- 將 Kiro 直接寫檔的失敗嘗試宣稱為 Kiro 產生的 commit
