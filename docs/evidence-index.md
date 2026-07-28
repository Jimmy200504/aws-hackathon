# 評審證據索引

本頁只列可由目前 release 直接核對的證據。`完成` 不代表已部署；AWS URL、
GitHub URL 與公開影片 URL 仍以 release manifest 的 null 為準。

Machine-readable completion status：`reports/submission-audit.json`。

| 評分／交付項 | 最短證據路徑 | 狀態 |
|---|---|---|
| API contract | `docs/openapi.yaml`、`tests/test_lambda_handler.py`、verifier G1 | 完成 |
| Graph schema + trace | `docs/graph-schema.md`、`POST /api/v1/graph/trace`、verifier G2 | 完成 |
| Live graph ablation | UI graph toggle、verifier G1.7 | 完成 |
| Leakage / cold-start | `docs/data-card.md`、ranker tests、verifier G2 | 完成 |
| LLM 方法／失敗模式 | `pipeline/bedrock_extract.py`、`docs/genai-safety.md` | 設計與 validator 完成；完整 Bedrock batch 未跑 |
| Position bias | `pipeline/ips.py`、final model manifest | 完成 |
| Locked ablation | `reports/ltr-ablation-test.json`、verifier G4 | 完成，整體 +1.34%，5% gate 未過 |
| 失敗實驗揭露 | `reports/ltr-ablation-holdout-1-failed.json`、verifier G5 | 完成 |
| Coverage 診斷 | `reports/graph-coverage.json`、verifier G8 | 完成，明示 post-hoc |
| 商業應用／A/B | `docs/business-case.md`、`reports/business-impact.json`、verifier G9 | 完成，無虛構營收 |
| AWS architecture | `docs/aws-architecture.md`、`infra/template.yaml` | 完成 |
| Lambda runtime proof | `reports/sam-local-smoke.json`、verifier G10 | Python 3.13 arm64 本機 emulation 完成 |
| Load smoke | `reports/load-smoke.json`、verifier G6 | Compact container 完成；AWS production 未跑 |
| Kiro +5% | `docs/kiro-evidence.md` | 可驗證 session，待評審認定 |
| Reproducibility | `scripts/release_gate.sh`、GitHub Actions、`release-manifest.json` | 完成 |
| Five-minute video artifact | `video/`、`reports/demo-video.json`、verifier G12 | 完成 |
| Public GitHub + video URL | release manifest、verifier G7 | 完成 |
| Public AWS URL | verifier G7 warning | 未完成 |

## 現場 90 秒證據路徑

1. UI 搜尋 `AWS Docker Kubernetes`，graph on/off 切換，指出 Top-10 改變。
2. UI 搜尋 `React 前端工程師`，點開 Query→Skill→Job evidence。
3. 開 `reports/ltr-ablation-test.json` 與失敗 holdout，說明 locked overall。
4. 開 `reports/graph-coverage.json`，說明 coverage bottleneck，不偷換 subgroup。
5. 執行 `./scripts/release_gate.sh`，展示 verifier 全綠與三個 external warnings。
