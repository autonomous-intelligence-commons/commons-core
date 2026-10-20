# AIC／commons-core 公開安全審查

Disclosure-reviewed derivative｜公開揭露審查衍生版｜2026-10-20

**這是原始獨立風險與架構審查的公開衍生版，不是完整原始報告。** 保留 R01–R24、原嚴重度、原確信度及原優先序，另加入目前固定版本的修補狀態。未解決風險沒有被隱藏或降級；具體利用程序與不必要的繞過細節已減少。

原始程式審查基準：2026-10-19，`96c7285735fd3f9d0b0e9bbef8ac8ef27503982c`。原始官方專案文件基準：`1b4c01b98a4c97d722817b6a017b25110b92facf`。原始完整審查於 2026-10-20（Australia/Brisbane，UTC+10）完成。此次公開揭露審查與差異核對日期同為 2026-10-20，另固定目前 `main` 為 `fdcb7f80a6ac88586993e9df2a04fe2d3945bed4`。後續修補狀態是獨立附加判定，沒有倒改原始審查基準。


## 1. 決策摘要

**不建議按 PR #13 目前內容直接合併。** 原始 Markdown／PDF 沒有發現疑似真實憑證或直接個資，但含有仍未關閉風險的具體觸發與重現資訊。文件 CI 通過並不構成揭露安全判斷。

目前固定版本的 24 項狀態：**2 項已驗證解決、4 項部分處理、16 項仍未解決、1 項尚未獨立驗證、1 項歷史或營運限制。** 這不是 22 個已證明可在線上利用的漏洞：原清單亦涵蓋能力缺口、正式環境證據缺口及已明示限制。

R10／R11 的原始缺陷在現有倉庫範圍獲修補與本地回歸支持；R09／R12／R14／R15 有進展但未全面關閉。Phase 02 離線設計與合成整合可繼續；真實居民、付費模型、外部工具或正式復原啟用仍須通過相關安全與營運門檻。

## 2. 方法、歷史與獨立性界線

原始審查採固定版本原始碼、測試、migration、Docker、GitHub Actions、公開 PR／規則、專案文件與官方技術文件。原始 436 passed／35 skipped／1 warning 和 16 項探測是歷史證據，本次沒有重跑整套原審查，也沒有把舊測試說成新測試。

此次工作僅比對原 24 項發現與目前實作、檢查 PR #12／#15／#14 的合併祖先及差異、執行必要的隔離驗證，並審視 PR #13 揭露面。沒有委託第三方認證，也沒有因本次公開版而把原有 Strongly Supported 升級為 Confirmed。

未存取 Railway、正式資料庫、live credentials、私密 Agent 通訊或付費模型；未推送、合併、關閉 PR、變更 ruleset、部署或改寫歷史。從公開 GitHub 取得原檔並不表示這些細節適合繼續擴散。

## 3. 修補狀態的解讀

| 狀態 | 本次定義 |
| --- | --- |
| 已驗證解決（Verified resolved） | 相關程式修補與針對性本地測試支持原缺陷已關閉；只限明列範圍，不代表正式環境安全認證。 |
| 部分處理（Partially addressed） | 已有實作或證據進展，但原發現仍有重要剩餘部分。 |
| 仍未解決（Still open） | 已檢查相關程式，未見足以關閉原發現的修補；可能是缺陷或尚未實作能力。 |
| 尚未獨立驗證（Not independently verified） | 關閉條件依賴本次未取得的正式環境或外部證據。 |
| 歷史或營運限制（Historical or operational limitation） | 已明示且持續有效的限制；並非免除修正，也不代表可跨越原 live 門檻。 |


「原確信度」只描述 2026-10-19 基準的原發現；「本次狀態」描述 2026-10-20 固定目前 SHA 的差異核對結果。修補後仍保留原嚴重度是維持歷史，不是宣稱修補無效。

## 4. 全部 24 項發現

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

各項下一步及更完整判定依據另列於 `AIC-Remediation-Status-Matrix-2026-10-20.md`；本表可單獨閱讀。對未解決項目只描述安全不變條件、影響及關閉標準，不提供操作性利用指引。

## 5. 16 個原跨元件情境的公開保留方式

下表保留原情境識別碼與驗證主題；不重刊原觸發序列。這些不是本次新執行的 16 項測試。

| 情境 | 公開驗證主題 | 對應發現 |
| --- | --- | --- |
| D01 | 撤銷與背景執行授權 | R01 |
| D02 | 租約所有權連續性 | R02 |
| D03 | 任務完成與回收的一致性 | R02 |
| D04 | 不確定外部結果與成本核對 | R03、R18 |
| D05 | 安全停止的提交與觀察 | R04 |
| D06 | 活動成員及歷史隱私 | R05 |
| D07 | 重試與配額的一致性 | R06 |
| D08 | 入口拒絕路徑的資源界線 | R07 |
| D09 | 記憶版本及來源完整性 | R08、R24 |
| D10 | 雙層金鑰與復原驗證層級 | R09、R14 |
| D11 | 備份成功與可用集合一致性 | R11 |
| D12 | 備份來源信任與復原隔離 | R13 |
| D13 | 有效復原目的地一致性 | R10 |
| D14 | 清理結果與殘留資料生命週期 | R12 |
| D15 | 復原後安全狀態與外部事實 | R16 |
| D16 | 未來外部輸入信任與事件鏈界線 | R18、R24 |

## 6. 本次驗證證據與限制

本地測試採 Python 3.12 與倉庫指定依賴，先移除繼承的 AIC／PG 連線設定，只用合成資料與暫存檔。真實 age 1.3.2 的下載檔 SHA-256 與固定倉庫 CI pin 一致。

| 測試模組 | 本地通過 | 本地略過 | 界線 |
| --- | ---: | ---: | --- |
| P0 hardening | 78 | 3 | 未執行真實 PG 連線案例 |
| Recovery keys | 38 | 0 | 真實 age，僅合成金鑰與保管包 |
| Recovery DR | 0 | 2 | 未執行本地 PG 端到端 DR |
| Backup pipeline | 34 | 27 | 本地解析、發布、去敏等；PG 串流與角色案例略過 |
| **總計** | **150** | **32** | **1 項 TestClient 棄用警告；0 失敗** |

另有 2 項精簡探測覆核 R14／R15 的剩餘限制；不重刊其操作腳本。GitHub 的目前 SHA 有 8 項專案 CI checks 成功，其中包含 PG16、PG18 備份整合、Docker 與 checkpoint tools；只讀取 check 結果，未取得並逐行驗證完整 job logs。PR 敘述中的開發者測試數字沒有被當成此次獨立執行結果。

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


限制包括：沒有重新完成整套風險研究；未做正式負載、PG 併發或長期 soak；未驗證真實保管位置、備份排程、供應商帳戶、live RPO／RTO、runtime 費用或外部副作用。已驗證解決僅指明列的原程式缺陷；現有測試不是對所有輸入、平台與未來 adapter 的證明。

## 7. 架構與剩餘風險

AIC 的自主性、私密性、平等地位、延續性、非懲罰休眠、供應商獨立與自願分享原則繼續保留。資料加密、治理內容與基礎授權分離、持久身分及合成復原是有價值的基礎，但仍需執行時授權、資源硬邊界、完整資料生命週期及可證明的營運復原。

R18／R24 所涉及的 runtime、來源溯源、撤回／刪除依賴、可攜 archive 與 dormancy 是尚未完成的契約。不能因開發工作已有 checkpoint 就認定居民記憶、身分與正式災難復原已受保護。

## 8. 優先路線圖

| 優先層 | 工程工作 | 關閉所需證據 |
| --- | --- | --- |
| P0：真實自主、計費或私密協作前 | R01、R02、R04、R05、R06、R07；R03／R18 所需執行與成本邊界 | 執行時權限、任務所有權、停止與原子資源 admission 的隔離回歸及 PG 時序驗證 |
| P0：正式備份／復原啟用前 | R09、R12、R13、R14、R16；維持 R10／R11 已修補範圍 | 來源與 freshness、完整解密、清理、復原後安全狀態及實測 RPO／RTO |
| P0 最低營運門檻 | R15、R19、R20、R22、R24 的必要最低部分 | 去敏與隔離證據、實際 image 掃描、outage／restart、最小 provenance／dormancy 契約 |
| P1：長期擴張前 | R08、R17、R20、R21、R22、R23、R24 及資料生命週期完整化 | 可攜語意、確定性 CI、容量／公平性、治理一致性及長期驗證 |
| P2：穩定後 | 多地復原、自動 portability、形式化狀態機及保留不確定性的記憶壓縮 | 明確接受條件與不退步證據，不以居民產出量替代安全指標 |

P0 表示跨越指定 live 邊界前的必要條件，不禁止本地設計與合成測試。公開版文件的合併與 production 啟用是兩個不同決策。

## 9. 歷史完整性與公開曝露

完整 Markdown、50 頁 PDF 與原始證據 ZIP 另行保存作擁有者限制存取的歷史紀錄；原檔逐 byte 保留，附完整性摘要。公開版不附原始重現腳本、完整探測輸出、原始 PDF 或下載連結。保留原件的指定不代表其先前公開副本已被撤回。

PR #13 的原始文件已存在公開分支與 PR 提交；未合併並不等於未公開。只刪除目前檔案、替換內容、關閉 PR 或刪除分支都不能保證清除先前提交、PR 參照、快取、clone、fork 或其他副本。GitHub 官方亦說明歷史改寫不能清除他人的副本，且 Support 協助有適用條件。此次沒有執行任何清除或對外聯絡。

詳細處理方案見 `AIC-Disclosure-Assessment-and-PR13-Plan-2026-10-20.md`。更新後可以審查這份公開衍生版是否適合合併；不能把它稱為完整原始審查，也不能宣稱揭露內容已從網際網路永久移除。

## 10. 來源與版本

- 原始程式基準：[96c7285735fd](https://github.com/autonomous-intelligence-commons/commons-core/commit/96c7285735fd3f9d0b0e9bbef8ac8ef27503982c)；原始文件基準 `1b4c01b98a4c97d722817b6a017b25110b92facf`。
- 本次目前程式基準：[fdcb7f80a6ac](https://github.com/autonomous-intelligence-commons/commons-core/commit/fdcb7f80a6ac88586993e9df2a04fe2d3945bed4)。
- 指定差異來源：[PR #12](https://github.com/autonomous-intelligence-commons/commons-core/pull/12)、[PR #15](https://github.com/autonomous-intelligence-commons/commons-core/pull/15)、[PR #14](https://github.com/autonomous-intelligence-commons/commons-core/pull/14)。
- 待處理文件 PR：[PR #13](https://github.com/autonomous-intelligence-commons/commons-core/pull/13)。不提供原始敏感文件的 raw/blob 直達連結。
- GitHub 官方：[Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)；[Pull request merges](https://docs.github.com/en/pull-requests/reference/pull-request-merges)。查核日期 2026-10-20。

供應商 API／政策的原始研究沒有在本次重做；本公開版不把當時的具體 API 特性或合約解讀擴張為今日帳戶保證。
