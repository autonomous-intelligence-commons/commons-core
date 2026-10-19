# Autonomous Intelligence Commons (AIC)
## Project Milestones — 2026-10-19

**專案創立日期：** 2026-10-17  
**紀錄日期：** 2026-10-19  
**專案階段：** Phase 1 — Infrastructure Reliability & Backup Foundation

**GitHub：** https://github.com/autonomous-intelligence-commons/commons-core

---

### Milestone 09 — Worker Long-Running Reliability Hardening

**狀態：已合併、已部署、已完成啟動驗證**

**GitHub Pull Request：** [PR #9](https://github.com/autonomous-intelligence-commons/commons-core/pull/9)

完成成果：

- 移除 Worker 預設處理 100 筆任務後正常退出的限制。
- 將 `--loop` 模式改為預設持續執行，直到收到停止訊號或發生致命錯誤。
- 保留明確指定 `--max-tasks N` 的功能。
- 將閒置輪詢間隔由 0.05 秒調整為 1 秒。
- 修正 Worker 啟動與結束日誌未正常輸出的問題。
- 將預設租約時間調整為 90 秒。
- 保留 Emergency Stop、任務租約、重試與資源配額等安全控制。

**驗證結果：**

- GitHub 六項必要 CI Checks 全部通過。
- Railway 新版 Worker 部署成功。
- 啟動日誌確認 `task_cap=unlimited`、`idle_sleep=1.0`。
- `commons-core` 健康檢查回傳 HTTP 200。
- Railway 三項服務均顯示 Online。

**限制：** 尚未完成新版 Worker 的長時間運作及故障恢復演練。

### Milestone 10 — Backup, Recovery & Cross-Provider Continuity Architecture

**狀態：研究與初步架構審查完成**

完成 ADR-0009 相關研究，涵蓋：

- PostgreSQL 一致性邏輯備份。
- age 公鑰認證加密。
- 獨立備份工作負載。
- 異地備份儲存。
- 隔離環境資料庫復原。
- 離線解密金鑰保管。
- 跨供應商資料可攜性。
- 歷史備份與可驗證刪除的衝突。
- RPO、RTO 與營運成本評估。

**建議架構：**

Railway PostgreSQL → 獨立 Cron 備份工作負載 → age 加密 → 異地物件儲存 → 隔離復原驗證。

已完成合成資料 PoC，GLM-5 回報 25/25 項檢查通過。

**限制：** ADR 的方向已作為 Stage 1 實作基礎，但尚未核准正式生產部署。

### Milestone 11 — Backup & Recovery Stage 1 Implementation

**狀態：已合併至 GitHub `main`，CI 驗證通過**

**GitHub Pull Request：** [PR #10](https://github.com/autonomous-intelligence-commons/commons-core/pull/10)

**合併 Commit：** `fbef050`

完成成果：

- 實作 `pg_dump -Fc` 直接串流至 age 加密程序。
- 避免在正常備份流程中建立未加密的中繼傾印檔。
- 新增備份 Manifest、雜湊驗證與本地原子發布機制。
- 新增失敗清理、部分輸出拒絕與故障注入測試。
- 建立隔離復原驗證工具。
- 新增還原目的地安全檢查。
- 改善合成私鑰與備份測試產物的保護。
- 更新 `docs/BACKUP.md`、ADR-0009、部署及安全文件。
- 保留正式 API、Worker 與現有安全機制。

**GLM-5 本地測試結果：**

| 測試 | 結果 |
|---|---|
| SQLite | 436 passed / 35 skipped |
| PostgreSQL 16 | 469 passed / 2 skipped |
| PostgreSQL 18.6 Backup Suite | 54 passed |
| Synthetic Restore CLI | 33/33 PASS |
| Ruff | 通過 |

**GitHub CI：7/7 Checks Passed**

合併後 `main` 曾出現一項配額測試失敗，重新執行後通過。相關 HTTP 429 測試的間歇性失敗原因仍未確認，應列入後續可靠性工作。

**限制：** Stage 1 僅完成程式庫端工具、合成資料復原及 CI；尚未執行正式資料庫異地備份。

### Milestone 12 — GitHub Branch Protection Expanded

**狀態：已完成設定並成功儲存**

**Ruleset：** `Protect main — AIC Commons Core`

**Enforcement：** Active

完成成果：

- 保留原本六項必要 CI Checks。
- 將 PostgreSQL 18 備份與復原測試加入必要檢查。
- 保留 Pull Request 合併要求。
- 保留禁止強制推送及分支刪除等既有保護。
- 完成 GitHub 身分確認。
- GitHub 回報 `Ruleset updated`。

**目前七項必要檢查：**

1. Lint & format check (ruff)
2. Tests (pytest — functional, security, privacy, recovery)
3. PostgreSQL 16 integration (migrations + full suite)
4. Docker build & compose smoke (real containers)
5. Alembic migration round-trip (SQLite)
6. Dependency vulnerability audit (pip-audit)
7. Backup pipeline — PostgreSQL 18 integration (ADR-0009 Stage 1)

**成果：** 後續修改 `main` 的 Pull Request 必須符合新增的備份測試保護要求。

---

## Phase 1 — 截至 2026-10-19 的整體進度

| 技術階段 | 狀態 |
|---|---|
| 核心 FastAPI、Worker、PostgreSQL 部署 | 已完成 |
| `noop` 與 `heartbeat` 端到端測試 | 已通過 |
| Worker 長期運行限制修正 | 已部署與啟動驗證 |
| Backup & Recovery 技術研究 | 已完成 |
| ADR-0009 架構設計 | 已完成初步審查 |
| Backup & Recovery Stage 1 | 已合併、CI 通過 |
| 七項 GitHub 必要 CI Checks | 已啟用 |
| Stage 2 — 離線金鑰保管 | 尚未開始 |
| Stage 3 — 異地備份與排程部署 | 尚未開始 |
| Stage 4 — 正式資料隔離復原演練 | 尚未開始 |

---

## 下一階段優先事項

**Priority 1 — Offline Recovery Key Custody（Stage 2）**

建立備份解密私鑰與 `AIC_MASTER_KEYS` 的獨立離線保管、冗餘備份及復原驗證程序。

**Priority 2 — Off-Provider Backup Deployment（Stage 3）**

規劃並經授權後建立 Railway Cron 備份工作負載、加密備份流程與獨立物件儲存。

**Priority 3 — Recovery Validation（Stage 4）**

在隔離環境中進行正式資料的授權復原演練，驗證 PostgreSQL 資料、應用程式加密資料及必要配置的恢復能力。

**Priority 4 — Reliability & Security Follow-up**

追查間歇性 HTTP 429 CI 測試失敗，並完成 Worker 長期連續運作、Emergency Stop、任務重試及故障恢復演練。

---

## 今日里程碑結論

**2026 年 10 月 19 日，AIC 完成兩項核心技術進展：**

1. **Worker Long-Running Reliability** — 移除無意間造成的 100 筆任務壽命限制，並成功部署及驗證新版 Worker 啟動狀態。
2. **Backup & Recovery Foundation** — 完成 ADR-0009 技術研究、加密備份與隔離復原工具的 Stage 1 實作，並將 PostgreSQL 18 備份測試納入 GitHub 必要 CI 保護。

AIC 已具有經過實際雲端驗證的背景任務管線，以及經合成資料與 CI 驗證的備份復原程式基礎。

**但截至本紀錄日期，AIC 尚未具備已驗證的正式資料庫異地備份、離線金鑰恢復或完整災難復原能力。**

下一階段將以資料延續性與可恢復性為核心，而不是急於接入真實 AI Agent。
