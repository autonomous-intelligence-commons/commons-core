# AIC — AI Agent 記憶系統研究：公開版說明（2026-11-04）

## 研究範圍

這份唯讀研究由 Astra 工作流程彙整，2026-11-04 為研究識別日期，2026-11-05 完成交付。**Markdown 為權威文本；PDF 是相同研究的閱讀版**。公開來源 JSON 保留來源識別、固定版本與證據類型，方便後續查核。

研究涵蓋 13 個外部實作、18 篇社群討論與 AIC 既有記憶程式的靜態對照。主要建議是保留 AIC 自有的身分、授權、加密、版本及復原治理核心，讓檢索、摘要及衍生索引保持可替換。

## 驗證及限制

- 本次是靜態、唯讀的研究整理；**未實作或整合任何記憶外掛，也未進行效能、相容性或刪除／復原的動態驗收**。
- AIC 程式基線及外部來源依報告記載的固定提交／版本，不應當作公開發布當日最新程式狀態。
- Reddit 部分精確日期未能獨立確認；論文作者的效能數據與社群經驗並非 AIC 的實測結果。
- 研究提出的 16 項風險與 10 個合成隔離實驗均屬設計和建議，**尚未執行**。
- 來源資料或記憶內容即使經摘要、衍生或備份，也仍應受原有身分、權限、保留及刪除治理限制。

## 安全與階段界線

- AIC Stage 3：**NOT STARTED**。
- A4-4 Windows Backup Publication Gate：**NOT APPROVED**。
- 破壞性 DR 操作：**NOT AUTHORIZED**。
- RPO 24 小時／RTO 1 小時：未量測目標。
- 本次僅公開研究文件。十項合成資料實驗均未執行，也未取得執行授權。目前未授權任何正式環境 PostgreSQL 操作、外部記憶外掛的安裝或整合，以及使用真實居民記憶進行實驗。記憶刪除、復原、權限分離與跨供應商續接須另行設計、測試及授權。

## 文件

- [研究報告（權威 Markdown）](AIC-Agent-Memory-GitHub-Industry-Community-Research-2026-11-04.md)
- [研究報告（25 頁 PDF）](AIC-Agent-Memory-GitHub-Industry-Community-Research-2026-11-04.pdf)
- [來源與固定版本登錄（JSON）](AIC-Agent-Memory-Research-Sources-2026-11-04.json)

## 交付處理

完整 Research Final Checkpoint、原始 Evidence ZIP、續接指令及工作紀錄保留於研究交付附件，不納入本次公開候選 PR。公開候選仍須經 GPT-6.1 Sol High 獨立唯讀發布／安全審查，以及 GitHub PR 的實際差異與 CI 驗證。
