# AIC／commons-core 修補狀態矩陣

公開揭露審查衍生資料｜2026-10-20

原始程式審查基準：2026-10-19，`96c7285735fd3f9d0b0e9bbef8ac8ef27503982c`。原始官方專案文件基準：`1b4c01b98a4c97d722817b6a017b25110b92facf`。原始完整審查於 2026-10-20（Australia/Brisbane，UTC+10）完成。此次公開揭露審查與差異核對日期同為 2026-10-20，另固定目前 `main` 為 `fdcb7f80a6ac88586993e9df2a04fe2d3945bed4`。後續修補狀態是獨立附加判定，沒有倒改原始審查基準。


原嚴重度／確信度／優先序保留歷史，與目前關閉狀態分開。**2 已驗證解決、4 部分處理、16 仍未解決、1 尚未獨立驗證、1 歷史或營運限制。**

| 狀態 | 本次定義 |
| --- | --- |
| 已驗證解決（Verified resolved） | 相關程式修補與針對性本地測試支持原缺陷已關閉；只限明列範圍，不代表正式環境安全認證。 |
| 部分處理（Partially addressed） | 已有實作或證據進展，但原發現仍有重要剩餘部分。 |
| 仍未解決（Still open） | 已檢查相關程式，未見足以關閉原發現的修補；可能是缺陷或尚未實作能力。 |
| 尚未獨立驗證（Not independently verified） | 關閉條件依賴本次未取得的正式環境或外部證據。 |
| 歷史或營運限制（Historical or operational limitation） | 已明示且持續有效的限制；並非免除修正，也不代表可跨越原 live 門檻。 |


| ID | 公開風險主題 | 原嚴重度／優先序 | 原確信度 | 本次狀態 | 判定依據與界線 | 證據 |
| --- | --- | --- | --- | --- | --- | --- |
| R01 | 執行時撤銷與授權 | 高（High）／P0 | 已確認（Confirmed） | **仍未解決** | 相關 Agent／Scheduler 實作與原基準一致，未見新的執行時授權契約。 | E01 |
| R02 | 任務租約與狀態一致性 | 高（High）／P0 | 已確認（Confirmed） | **仍未解決** | 任務狀態機、租約欄位與 Worker 相關實作未變。 | E01 |
| R03 | 執行隔離與硬期限 | 高（High）／P0 | 已確認（Confirmed） | **歷史或營運限制** | 原協作式 Worker 設計未改；備份子程序的改善不等於 Worker 已獲硬隔離。限制仍有效。 | E01、E04 |
| R04 | 安全停止的持久化保障 | 高（High）／P0 | 已確認（Confirmed） | **仍未解決** | 控制狀態與事件記錄的核心交易實作未變。 | E01 |
| R05 | 活動成員與隱私邊界 | 高（High）／P0 | 已確認（Confirmed） | **仍未解決** | 活動、頻道與通用授權的相關實作未變。 | E01 |
| R06 | 共享配額與交易一致性 | 高（High）／P0 | 已確認（Confirmed） | **仍未解決** | 配額、訊息交易、請求計數與相關排程實作未變。 | E01 |
| R07 | 入口資源與驗證成本 | 高（High）／P0 | 有強力證據支持（Strongly Supported） | **仍未解決** | middleware 與認證路徑未變，未取得正式入口上限證據。 | E01 |
| R08 | 加密記憶的語意完整性 | 高（High）／P1 | 已確認（Confirmed） | **仍未解決** | 記憶與訊息加密綁定實作未變；威脅模型文件的更新不構成實作修復。 | E01 |
| R09 | 正式備份與復原金鑰保管 | 高（High）／P0 | 需環境驗證（Requires Environment Verification） | **部分處理** | PR #12 新增保管包、獨立摘要核對、歷史金鑰連續性與合成 DR；本地 38 項金鑰測試通過。正式保管、離站排程與 RPO／RTO 仍未獨立驗證。 | E02、E05、E07 |
| R10 | 復原連線契約的一致性 | 高（High）／P0 | 已確認（Confirmed） | **已驗證解決** | PR #15 引入統一解析、環境審核與共用連線建構；相關本地回歸通過。判定只涵蓋原始連線不一致缺陷及已測設定。 | E03、E04、E05 |
| R11 | 備份發布成功的可信語意 | 高（High）／P0 | 已確認（Confirmed） | **已驗證解決** | PR #15 增加完整集合重驗、無覆寫發布與失敗關閉；缺失／損壞／競爭等本地回歸通過。範圍為現有本地儲存 adapter。 | E03、E04、E05 |
| R12 | 備份／復原清理與期限 | 高（High）／P0 | 已確認（Confirmed） | **部分處理** | PR #15 改善發布失敗清理、資料庫刪除確認、清理失敗判定與子程序終止；本地故障注入通過。強制中斷殘留清理、跨平台程序樹與完整端到端資源界線仍有缺口。 | E03、E04、E05 |
| R13 | 備份來源認證與新鮮度 | 高（High）／P0 | 有強力證據支持（Strongly Supported） | **仍未解決** | Stage 2 的保管包信任錨不等於備份發布者認證；未見完整備份來源與防回滾機制。 | E02、E03、E01 |
| R14 | 復原驗證的保證層級 | 高（High）／P0 | 已確認（Confirmed） | **部分處理** | Stage 2 擴充多代金鑰與合成 DR 驗證，但結構檢查與完整營運復原尚未形成分層契約；本地精簡探測確認限制仍在。 | E02、E05、E06 |
| R15 | 錯誤、日誌與 metadata 保密 | 中（Medium）／P1 | 已確認（Confirmed） | **部分處理** | Stage 2 及 PR #15 改善備份 URL／工具錯誤去敏；相關本地測試通過。核心錯誤與 metadata 路徑未全部改版，本地 canary 探測仍顯示缺口。 | E01、E02、E05、E06 |
| R16 | 復原後的安全狀態連續性 | 高（High）／P0 | 有強力證據支持（Strongly Supported） | **仍未解決** | 未見獨立撤銷／刪除事件覆蓋、復原隔離狀態及外部結果核對的完整實作。 | E01、E02、E03 |
| R17 | 長期容量與歷史存取 | 中（Medium）／P1 | 有強力證據支持（Strongly Supported） | **仍未解決** | 記憶歷史、相關列表、保留及容量設定實作未變。 | E01 |
| R18 | 模型 Runtime 與成本帳本 | 高（High）／P0 | 有強力證據支持（Strongly Supported） | **仍未解決** | registry、設定與依賴未提供完整 provider runtime／原子成本帳本；PR #14 僅處理開發續作。這是能力缺口，不是已發生失控帳單。 | E01、E08 |
| R19 | 正式部署權限與隔離 | 高（High）／P0 | 需環境驗證（Requires Environment Verification） | **尚未獨立驗證** | 本次只讀公開倉庫；未存取 Railway、正式 DB、部署 secrets 或營運 ACL。部署文件更新不構成實境證據。 | E01、E02 |
| R20 | CI 與供應鏈保證 | 中（Medium）／P1 | 已確認（Confirmed） | **仍未解決** | 工作流程新增驗證，但 dependency-audit 仍非阻擋且未涵蓋完整 postgres 安裝集合；沒有因綠燈而關閉原發現。原始 ruleset 保證未重新認證。 | E01、E07、E08 |
| R21 | 限流測試的確定性 | 中（Medium）／P1 | 已確認（Confirmed） | **仍未解決** | 相關整合測試及 limiter 實作未變；未取得原歷史失敗的完整 logs。 | E01 |
| R22 | 運作健康與中斷恢復 | 中（Medium）／P1 | 有強力證據支持（Strongly Supported） | **仍未解決** | 健康檢查、Worker 與 telemetry 核心實作未變；未取得正式長期 soak／outage 證據。 | E01 |
| R23 | 治理歷史的一致性 | 中（Medium）／P1 | 有強力證據支持（Strongly Supported） | **仍未解決** | 治理服務與資料模型未變；原跨連線併發路徑仍未另行重現。 | E01 |
| R24 | 可攜記憶與休眠契約 | 中（Medium）／P1 | 有強力證據支持（Strongly Supported） | **仍未解決** | Agent／Memory schema 與來源管理核心未變；開發 checkpoint 不能替代居民記憶契約。 | E01、E08 |

## 逐項後續工作

| ID | 公開風險描述 | 必要後續工作／接受證據 |
| --- | --- | --- |
| R01 | 授權狀態需在背景執行期間維持有效。 | 補齊授權世代與執行時檢查，取得撤銷競爭的隔離回歸證據。 |
| R02 | 並行執行與工作回收需要一致的所有權保障。 | 建立租約世代與條件式狀態轉移；以真實 PG 雙連線驗證。 |
| R03 | 現有協作式執行限制不適合直接承載任意長工作。 | 在付費模型／工具接入前補程序隔離、端到端期限及外部結果核對。 |
| R04 | 停止訊號必須具備可靠且可確認的提交語意。 | 分離必要提交與最佳努力事件記錄，驗證失敗回報及停止觀察。 |
| R05 | 不同協作元件必須維持一致的成員與歷史可見性規則。 | 統一成員關係契約及所有入口的授權回歸驗證。 |
| R06 | 配額、重試與持久使用量需要同一原子契約。 | 以共享原子 admission 與正確交易範圍保證一致性。 |
| R07 | 流量拒絕路徑需要可預期的 CPU、連線與解析成本。 | 補低成本前置 admission、同步工作隔離與有界負載測試。 |
| R08 | 內容完整性還需涵蓋版本、來源及歷史語意。 | 制定版本化 AAD 與遷移，並界定抗資料庫回滾的信任根。 |
| R09 | 離站副本與雙層金鑰需形成可驗證的營運閉環。 | 補非秘密的雙副本保管、正式 freshness 與實境復原證明。 |
| R10 | 目的地驗證與執行需採同一連線契約。 | 保留回歸測試；正式網路隔離與 DB 角色仍歸 R19，不能由此宣稱已驗證。 |
| R11 | 成功狀態必須對應可重新驗證的完整物件集合。 | 未來物件儲存 adapter 須另測；此結果不證明來源認證、防回滾或所有平台 crash durability。 |
| R12 | 清理結果、子程序期限及殘留物需可觀察並受控。 | 完成殘留物生命週期、全流程期限及資源上限驗證；保留平台限制。 |
| R13 | 完整性與加密不能單獨取代合法來源及新鮮度證明。 | 建立獨立可信 checkpoint／來源認證及還原隔離，修正過強保證。 |
| R14 | 驗證通過必須明確表達實際證明了哪些復原能力。 | 分層報告、逐金鑰與資料族群驗證，補目標版本及營運不變條件證據。 |
| R15 | 錯誤與明文營運欄位需避免承載私密資訊。 | 完成跨服務錯誤去敏與 log sink canary 驗證，設定保留及存取政策。 |
| R16 | 復原不能倒退必要的安全事件與外部執行事實。 | 建立單調安全帳本、復原隔離與 reconciliation；未實作的刪除能力仍屬設計門檻。 |
| R17 | 長期使用需要總容量、公平性與有界歷史讀取。 | 補總儲存配額、穩定分頁及可驗證保留執行。 |
| R18 | 真實模型與工具接入前需要獨立、可信的成本硬上限。 | 設計 provider-independent 執行狀態、reserve／settle 與事件鏈上限。 |
| R19 | 倉庫不能證明正式部署版本、角色與資源隔離。 | 由擁有者提供去敏部署摘要、服務角色及隔離驗證，不提供秘密。 |
| R20 | CI 綠燈需與實際掃描覆蓋及阻擋政策分開解讀。 | 以正式 image／依賴集合做可見掃描與故障 gate，補可重現供應鏈。 |
| R21 | 時間相關測試需可靠區分實作錯誤與測試前提。 | 固定測試時鐘並保留窗口切換測試；歷史 CI 根因繼續標為未定。 |
| R22 | 存活訊號需與實際工作進展及復原能力區分。 | 補 readiness、獨立進展監測與受控中斷恢復證明。 |
| R23 | 治理版本的識別與資料約束需保持一致。 | 統一 lineage、原子 head 更新與治理內容不授權的結構邊界。 |
| R24 | 長期身分延續需要來源、有效性、休眠與遷移語意。 | 先固定最小 provenance、非懲罰休眠與 archive schema，再分期做壓縮／遷移。 |

## 證據索引與提交

| 代號 | 證據與可支持範圍 |
| --- | --- |
| E01 | 原始報告、目前固定程式與逐項來源檔案雜湊比較。原報告引用的來源逐項比對；相關核心授權、排程、記憶、治理等實作未變。未變本身不取代動態重現，故不升級原確信度。 |
| E02 | [PR #12](https://github.com/autonomous-intelligence-commons/commons-core/pull/12)，合併 [4ec584e79929](https://github.com/autonomous-intelligence-commons/commons-core/commit/4ec584e7992904603bf1fb185e60dcfca9255518)；保管包、歷史 key 連續性與合成復原程式／測試。 |
| E03 | [PR #15](https://github.com/autonomous-intelligence-commons/commons-core/pull/15)，合併 [9dbb68f4599b](https://github.com/autonomous-intelligence-commons/commons-core/commit/9dbb68f4599bbceca203b1868509dae9c4bf037a)；主要實作 [3637c3ffb0de](https://github.com/autonomous-intelligence-commons/commons-core/commit/3637c3ffb0de1a83149caea3b59dd4d089bc85cb)，環境處理補強 [73da57e6e72c](https://github.com/autonomous-intelligence-commons/commons-core/commit/73da57e6e72c4d56a910154ecfd05a3d2b417b0e)，發布補強 [325aa256606d](https://github.com/autonomous-intelligence-commons/commons-core/commit/325aa256606d0633bde0a44c20f3fe54fe6b2b77)。短 SHA 僅作顯示，驗證包保留完整值。 |
| E04 | 目前 commit 的統一連線契約、發布集合重驗、清理回報及平台限制；本地 P0 hardening 測試 78 通過、3 略過。 |
| E05 | 本次本地四個備份／復原測試模組：150 passed、32 skipped、1 warning；真實 age 1.3.2 依倉庫 CI pin 核對 SHA-256 後使用；未執行本地 PostgreSQL。 |
| E06 | 本地暫存 SQLite／合成 canary 的兩項精簡探測：復原保證層級與核心錯誤去敏仍有缺口。只保存布林觀察，公開版不附可直接執行的重現腳本。 |
| E07 | 固定目前 SHA 的 [CI run 39416410990](https://github.com/autonomous-intelligence-commons/commons-core/actions/runs/39416410990)：8 個專案 CI checks 皆 success，另有 3 個 Dependabot checks。這是 GitHub 回報，不是本地 PostgreSQL／Docker 或正式環境獨立實測。 |
| E08 | [PR #14](https://github.com/autonomous-intelligence-commons/commons-core/pull/14)，合併 [fdcb7f80a6ac](https://github.com/autonomous-intelligence-commons/commons-core/commit/fdcb7f80a6ac88586993e9df2a04fe2d3945bed4)；Development Checkpoint & Resume Protocol 改善開發工作復原，不是居民 runtime／production DR 的替代品。 |
| E09 | PR #13 兩個檔案與 supplied Markdown／PDF 逐 byte 相同；仍為 open、未合併。原始證據 ZIP 找回後，29 個 manifest 項目全數雜湊一致。 |


完整 SHA 與檔案摘要見公開驗證包；本矩陣不重刊未解決項目的具體函式行號、利用程序、參數組合或重現程式。原始確信度沒有因來源未變而升級。

## 判定的重要限制

- R10／R11 的「已驗證解決」只限原缺陷與已測倉庫範圍；正式隔離、來源真實性與未來 storage adapter 仍需獨立驗證。
- R12 的原 T10／T11 路徑已有修補與本地回歸；整體 R12 因殘留生命週期、平台及端到端界線仍列部分處理。文件的 total-deadline／bounded-stderr 描述沒有在本次被當成全流程保證。
- R14／R15 的剩餘缺口有本次精簡本地觀察；Stage 2 或去敏測試成功不能替代其餘關閉條件。
- R03 的歷史／營運限制仍阻擋直接接入不受控長工作；R18／R24 是未完成能力，不代表已發生攻擊或費用事故。
- R19 不接受以程式與文件替代真實部署證據。R09 的正式保管及離站備份部分同樣未驗證。
- R20 的規則設定只保留原審查歷史判定，本次未重新認證目前完整 ruleset／bypass 清單。
- R21／R23 的歷史根因與 PG 併發尚未取得新實境證據，故維持原確信度及限制。
