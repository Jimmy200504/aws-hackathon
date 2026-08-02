# 評審證據索引

本頁只列可由目前 release 直接核對的證據。AWS、GitHub release 與公開影片
URL 均已登錄於 release manifest；歷史 Bedrock pilot 不屬於 production graph build。

Machine-readable completion status：`reports/submission-audit.json`。

| 評分／交付項 | 最短證據路徑 | 狀態 |
|---|---|---|
| API contract | `docs/openapi.yaml`、`tests/test_lambda_handler.py`、verifier G1 | 完成 |
| Graph schema + trace | `docs/graph-schema.md`、`POST /api/v1/graph/trace`、verifier G2 | 完成 |
| Live graph ablation | UI graph toggle、verifier G1.7 | 完成 |
| Leakage / cold-start | `docs/data-card.md`、ranker tests、verifier G2 | 完成 |
| 歷史 LLM pilot／失敗模式 | `docs/genai-safety.md`、`reports/bedrock-pilot.json` | 舊 Bedrock pilot：200 input、180 accepted、1,598 mentions、US$1.06；只保留 aggregate report，非 production build |
| Deterministic corpus inventory | `reports/deterministic-corpus-inventory.json`、`pipeline/deterministic_extract.py` | 1,218,635 input；cutoff eligible 967,377；0 LLM/embedding request |
| Position bias | `pipeline/ips.py`、final model manifest | 完成 |
| Locked ablation | `reports/ltr-quality-confirmation.json`、`reports/verify-quality-release.json` | 完成，NDCG +5.72%，CI 全正 |
| Independent replication | `reports/ltr-quality-replication.json`、quality verifier | 完成，互斥 bucket NDCG +5.07%，CI 全正 |
| 失敗實驗揭露 | `reports/ltr-ablation-holdout-1-failed.json`、`reports/ltr-quality-company-holdout.json` | 完成 |
| Coverage 診斷 | `reports/graph-coverage.json`、verifier G8 | 完成，明示 post-hoc |
| 商業應用／A/B | `docs/business-case.md`、`reports/business-impact.json`、verifier G9 | 完成，無虛構營收 |
| AWS architecture | `docs/aws-architecture.md`、`infra/template.yaml` | 完成 |
| Lambda runtime proof | `reports/sam-local-smoke.json`、verifier G10 | Python 3.13 arm64 本機 emulation 完成 |
| Portable LTR parity | `reports/portable-ltr-parity.json`、`app/tree_ranker.py` | 40,218 rows；centered score 最大誤差 1.08e-7 |
| Load smoke | `reports/load-smoke.json`、verifier G6 | Compact container 完成 |
| AWS public runtime | `reports/aws-production-smoke.json`、verifier G13 | 既有 UI/assets/API/trace smoke；新的 graph manifest 切換仍受 `<800 ms` release gate 約束 |
| Kiro +5% | `docs/kiro-evidence.md` | 可驗證 session，待評審認定 |
| Reproducibility | `scripts/release_gate.sh`、GitHub Actions、`release-manifest.json` | 完成 |
| Five-minute video artifact | `video/`、`reports/demo-video.json`、verifier G12 | 完成 |
| Public GitHub + video URL | release manifest、verifier G7 | 完成 |
| Public AWS URL | release manifest、verifier G7、verifier G13 | 完成 |

## 現場 90 秒證據路徑

1. UI 搜尋 `AWS Docker Kubernetes`，graph on/off 切換，指出 Top-10 改變。
2. UI 搜尋 `React 前端工程師`，點開 Query→Skill→Job evidence。
3. 開兩份 `ltr-quality-*confirmation/replication.json`，說明 frozen model 與互斥 buckets。
4. 執行 `python3 scripts/verify_quality_release.py`，展示 12 個證據檢查全綠。
5. 開 `reports/aws-production-smoke.json`，展示外網 30/30、p95 與 graph toggle。
6. 執行 `./scripts/release_gate.sh`，展示 verifier 全綠、零 warning。
