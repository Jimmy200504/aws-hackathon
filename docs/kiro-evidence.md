# Kiro 使用證據

本專案於 2026-07-28 使用已登入的 Kiro CLI 2.15.0 完成 release-readiness
工作。Kiro session 讀取既有 `.kiro/specs/skillweave/`、設計獨立 release
verifier 的檢查群組與 failure semantics，並在實作後進行 read-only review。

## 可驗證 activity

- Session ID：`7cf6f97b-3435-49ff-a214-950453ce8b08`
- Source：`classic`
- Workspace：本 repo 根目錄
- 最後確認：26 messages，更新時間 `2026-07-28T11:58:37.030Z`
- Tracked task：`.kiro/specs/skillweave/tasks.md` 的 release evidence audit
- 產物：`scripts/verify_release.py`、`tests/test_verify_release.py`
- 機器報告：`reports/verify-release.json`

第一次由 Kiro 直接寫檔時，上游 tool stream 未完成，因此沒有把失敗嘗試宣稱為
Kiro 產生的 commit。之後在同一 session 中，Kiro 完成 verifier contract 設計；
本地實作完成後，再由同一 session 讀取實際程式與報告並提出完整性 review。
Review 促成 graph path 結構驗證與 runtime index/version binding 的加強。

## 重現

已登入 Kiro CLI 的環境可執行：

```bash
kiro-cli chat --list-sessions --format json-pretty
python3 scripts/verify_release.py
```

Session metadata 不含提示內容全文、credential 或 token；repo 只保存足以讓評審
核對的 session ID、任務、產物與驗證結果。
