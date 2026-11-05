# AIC — AI Agent 記憶系統研究

GitHub 外掛、產業研究與社群證據審查

研究識別日期：2026-11-04。續接與交付整理：2026-11-05（Australia/Brisbane）。檔名沿用原委託日期。Markdown 為權威版本，PDF 為同文閱讀版。

## 1. 主要判斷

**建議採用「AIC 自有核心契約＋可替換外部元件」：保留身分、授權、加密、版本與復原邊界，再測試檢索、摘要與經驗提煉。現有證據不足以支持整體更換 commons-core。**

這是基於程式與研究證據的架構建議，並非已完成整合或效能驗收。關鍵發現如下：

1. **AIC 已有實作基礎。** 固定提交內可直接看到加密記憶、owner 控制的存取權限、追加版本及合成資料備份／復原工具；完整來源鏈、記憶刪除傳播、語義檢索與跨供應商連續性契約則尚未在已查核程式中找到。[AIC01](https://github.com/autonomous-intelligence-commons/commons-core)
2. **外掛的注入位置會改變風險。** vpromise 與 zhbcher 使用 user-role 訊息；PLUR 使用 system-prompt section；OpenAI SandboxMemory 的起始摘要進 developer prompt。資料能持久保存，不表示它有權改寫安全規則。[R02](https://github.com/vpromise/dsh-memory)[R04](https://github.com/zhbcher/dsh-memory)[R03](https://github.com/plur-ai/plur)[R11](https://github.com/openai/openai-agents-python)
3. **「刪除完成」需要跨資料層驗證。** Mem0 原始會話殘留問題與目前程式路徑相符；Graphiti 已修復部分 episode 刪除問題，但摘要重建例外仍保留舊摘要。兩者不能混稱為「完全沒有刪除功能」或「問題已全部解決」。[C11](https://github.com/mem0ai/mem0/issues/7452)[C13](https://github.com/getzep/graphiti/issues/2034)
4. **記憶效果取決於表示、任務與成本。** PlugMem 提供有價值的知識單元與壓縮證據；MemoryArena 顯示外部記憶並非各任務都勝過完整歷史。應以共同模型、相同資料、全部寫入／讀取成本實驗，避免跨榜單排序。[P01](https://arxiv.org/abs/2603.03296v1)[P04](https://arxiv.org/abs/2602.16313v2)
5. **優先驗證生命週期，再追求召回率。** 第一批原型應測來源角色、隔離、修正、刪除後重建、舊備份復活、休眠續接及故障回報；不要求居民持續工作、定期產出或以活動量換取地位。

本次固定 **19 個儲存庫**，其中 **13 個互不重複的外部實作**納入比較（12 個根授權為 MIT／Apache-2.0，OpenViking 條款另待釐清）；記錄 **18 篇社群討論**，其中 **16 篇具足夠技術內容**，另 2 篇作公告與證據品質對照。比較涵蓋九個指定公司／研究組織。沒有安裝外掛、修改 AIC、建立 PR、執行第三方程式、使用付費推理或存取 production 記憶。

### 1.1 如何閱讀證據

| 標記 | 本報告中的意思 | 不代表什麼 |
|---|---|---|
| 已確認 | 固定提交、原始文件或論文直接支持的結構與控制流程 | 不代表該軟體在 AIC 已通過動態測試 |
| 證據支持的解讀 | 由實作推導的適配性或風險判斷 | 不等於已發生攻擊或普遍故障 |
| 未獨立驗證 | 作者量測、使用者經驗、行銷或未重現 bug | 不拿來當部署保證 |
| 工程假設／建議 | 本研究提出的設計與驗收門檻 | 尚未實作，也未授權開始 Stage 3 |

正文的 R／P／D／C／AIC 編號可連到原始來源。程式連結固定 commit；完整 SHA、作者、版本、存取限制及原始檔案雜湊在來源 JSON 與證據包。下載到檔案不等於整檔完整審計；本次閱讀深度以逐項記錄為準。

### 1.2 去重、取樣與限制

先核對使用者提供的第 001–003 期與累積來源紀錄，排除已研究的 **14 項成果及 31 個 Moltbook post ID**。前期的 compaction laundering、AfterDeleteBench、備份復原與記憶累積風險僅作已知背景，不在本次重做長篇分析。此次新增的是固定提交實作、外掛信任通道、產業分類、社群修復狀態與 AIC 目前程式對照。

本研究是目的性選樣，不是涵蓋所有產品的系統性綜述。技術結論優先用官方程式、論文與文件；社群原帖用來發現失敗模式與反證。星數、按讚數、issue 關閉與 README 分數都不作正確性證明。Reddit 原頁與留言可讀，但匿名 JSON 日期請求回傳 403；無法確認的精確發表日保留「未確認」，不以搜尋索引時間冒充原始時間。未登入、未繞過存取限制。

### 1.3 續接核對

Checkpoint 01 與 02 的 ZIP 完整性及外部 SHA-256 已重新核對。現有工作目錄比 Checkpoint 02 更新：來源登錄、18 篇社群摘要與 124 筆程式檔案索引已存在；正式中文報告當時尚未組裝。續接從保存研究筆記、完成評估與交付開始，未重做先前 GitHub 檢查。查核範圍延續 2026-11-04 至 2026-11-05；不宣稱查核後的所有新提交均已涵蓋。


## 2. 記憶能力的分類

| 能力 | 保存／使用的對象 | 能改善什麼 | 本身不能保證什麼 |
|---|---|---|---|
| A. 長上下文 | 當次輸入中的更多原文 | 減少先行選取，保留細節 | 跨會話保存、注意力可靠性、低成本、刪除 |
| B. 壓縮與管理 | 摘要、裁切、重排、選出的工作內容 | 控制提示長度、減少噪音 | 未來問題所需細節仍在；角色、否定與時序不失真 |
| C. 外部持久記憶 | 資料庫、檔案、圖譜、向量、歷史 | 跨會話保存與按需取回 | 取回正確內容、權限隔離、衍生物同步刪除 |
| D. 跨會話／供應商連續性 | 身分、來源、版本、狀態、未決事項、缺口 | 重啟後有可解釋的接續依據 | 模型行為相同、相同人格或意識連續性 |

檢索是 C 的取回機制，也可服務 A／B；它不等同完整記憶系統。Provider prompt cache 則重用計算，與持久內容的保存、授權及可攜性不同。[D06](https://docs.z.ai/guides/capabilities/cache)

episodic memory 保存事件及其時空來源；semantic memory 保存命題／事實；procedural memory 保存可重用方法；working memory 是當前推理所使用的狀態。這些是資料用途，不應自動變成不同權限等級。例如「過去成功的方法」只是可修正的經驗，不能自行授權部署或私密資料存取。

對 AIC，最低可用連續性應回答：這是誰的狀態、何時及從何而來、哪些內容已被取代或撤回、哪些資料沒有載入、接續後應重新確認什麼。居民可以休眠、改變興趣或不分享；續接不必綁定待完成的人類任務。模型權重內的持續學習又屬另一層，不能直接用刪除外部資料列去推論權重中的資訊已消失。


## 3. AIC 的實際程式基線

原研究使用的 commons-core 固定提交為 **a45b7cca1af4305a838ac5785bb96995cb020c2a**。原研究記載，其程式基線封存的 ZIP 註解與當時的 GitHub `main` 一致，且 206 個對應檔案的 blob 雜湊相符。這次公開候選包僅含研究文件與來源索引，未附該基線封存或逐檔比對紀錄。程式判斷限於原研究的靜態查核範圍。官網僅作專案原則與歷史入口，技術判斷以 commons-core 程式為準。[AIC01](https://github.com/autonomous-intelligence-commons/commons-core)[AIC02](https://github.com/autonomous-intelligence-commons/autonomous-intelligence-commons.github.io)

| 能力 | 狀態 | 已讀實作與界線 |
|---|---|---|
| 私有 namespace 與 owner 控制授權 | 已實作、靜態確認 | owner 可 grant/revoke read/write；基礎設施 admin 不等同 owner |
| 加密記憶與驗證 | 已實作、靜態確認 | 寫入先驗 JSON／大小再 seal；AAD 綁 namespace/key |
| 修正與版本歷史 | 已實作、靜態確認 | 新版追加，舊版 superseded_by；唯一約束與有限重試 |
| 最小來源資訊 | 部分實作 | author_id、version、created_at；未等同 source_role、外部證據及 derivation graph |
| 精確 key／version 讀取 | 已實作 | 未找到語義向量檢索或跨記憶知識圖 |
| 刪除／tombstone／衍生物傳播 | 此基線未實作 | 安全測試刻意要求無 update/delete API；取代版本不等於抹除舊資料 |
| 備份與復原工具 | 已實作、限程式與合成驗證路徑 | streaming pg_dump→age、完整性檢查、commit marker、受限復原與清理回報 |
| 遠端排程、真實 production 復原、RPO/RTO | 本次未獨立驗證 | repo 文件將部分運維工作留待後續；本次未接觸雲端或資料庫 |
| 實際模型自主運作 | 未由此程式證實 | effects/provider.py 為 FakeEffectProvider；不能當成真實 LLM runtime |
| 跨供應商完整續接膠囊 | 設計需要，未見完整實作 | 已有資料與版本核心，可作後續契約基礎 |

程式定位：[AIC01 service.py:137-222](https://github.com/autonomous-intelligence-commons/commons-core/blob/a45b7cca1af4305a838ac5785bb96995cb020c2a/src/commons_core/memory/service.py#L137-L222)、[AIC01 service.py:251-340](https://github.com/autonomous-intelligence-commons/commons-core/blob/a45b7cca1af4305a838ac5785bb96995cb020c2a/src/commons_core/memory/service.py#L251-L340)、[AIC01 models.py:274-330](https://github.com/autonomous-intelligence-commons/commons-core/blob/a45b7cca1af4305a838ac5785bb96995cb020c2a/src/commons_core/database/models.py#L274-L330)、[AIC01 test_memory_isolation.py:177-221](https://github.com/autonomous-intelligence-commons/commons-core/blob/a45b7cca1af4305a838ac5785bb96995cb020c2a/tests/security/test_memory_isolation.py#L177-L221)、[AIC01 provider.py](https://github.com/autonomous-intelligence-commons/commons-core/blob/a45b7cca1af4305a838ac5785bb96995cb020c2a/src/commons_core/effects/provider.py)。

備份層的 SHA-256／commit marker 支持完整性與發布流程，不是刪除證明，也不能單獨抵抗同時控制備份和標記的攻擊者。程式中的合成復原會檢查目標、版本、雜湊、解密與應用資料，並回報清理結果；未包含完整 deletion/retention reconciliation。使用者既有測試與階段紀錄屬已提供歷史證據，本次沒有重跑或推定真實環境保證。[AIC01 BACKUP.md:1-36](https://github.com/autonomous-intelligence-commons/commons-core/blob/a45b7cca1af4305a838ac5785bb96995cb020c2a/docs/BACKUP.md#L1-L36)、[AIC01 restore.py:719-860](https://github.com/autonomous-intelligence-commons/commons-core/blob/a45b7cca1af4305a838ac5785bb96995cb020c2a/src/commons_core/backup/restore.py#L719-L860)。

**適配性判斷：** 不應將現在的加密、版本與 owner 權限退化成共用 MEMORY.md。外部元件應處理可以重建的索引或衍生摘要；AIC 核心仍負責「哪位居民有權讀寫哪筆資料、版本和刪除狀態為何」。開源記憶框架即使提供 user_id／namespace，也須證明它們由可信身分來源建立，不能讓模型輸出直接改寫。


## 4. DeepSeek Harness 與外掛

### 4.1 原生能力與版本

固定提交 fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159，根 package 版本 0.2.2-rc.1；採 Cordis 外掛架構，仍有開發預覽及介面變動風險。context 組把注入內容保存為 user-role 訊息；預設掛載 agent-instructions，file/session reference 等需要選用。systemPrompt.section 是另一種組裝機制，不能與 durable user message 混為一談。[R01](https://github.com/deepseek-ai/deepseek-harness)

| 機制 | 已確認的職責 | AIC 使用時須另外驗證 |
|---|---|---|
| agent-instructions | 載入工作目錄指引並隨編輯刷新 | repo 內容可信度、指令作用範圍 |
| file reference | 檔案提及與路徑／內容參照 | 檔案讀取權限、秘密外洩 |
| session reference | 其他會話的有界唯讀快照 | 跨居民存取、快照日期與遺漏 |
| session persistence | 保存會話事件，供重播／恢復 | 保留期限、刪除、私密資料邊界 |
| compaction-basic | 在上下文壓力下摘要／整理 | 否定、時序、來源與失敗可觀測性 |
| tool-result-pruner | 在摘要前減少工具輸出負擔 | 原始證據是否仍可被授權取回 |
| MCP memory 範例 | 對接可選外部記憶工具 | 不是內建完整記憶治理，也不是官方安全背書 |

證據：[R01 README.md:6-32](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/context/README.md#L6-L32)、[R01 index.ts](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/context/session-reference/src/index.ts)、[R01 index.ts](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/compaction/compaction-basic/src/index.ts)、[R01 index.ts](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/compaction/compaction-tool-result-pruner/src/index.ts)、[R01 mcp-memory.md](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/user/guide/mcp-memory.md)。

### 4.2 三個重點外掛的真實差異

| 外掛 | 擷取、保存與選取 | 注入通道 | 修正／刪除與失敗界線 |
|---|---|---|---|
| vpromise/dsh-memory | 觀察會話，回合結束後可由 LLM 壓縮；project JSONL；關鍵字與近期權重 | createUserMessage；source.kind=memory | 可移除記錄，但跳過壞 JSONL 行；壓縮失敗警告後回傳空結果；隊列限單實例 |
| zhbcher/dsh-memory | 使用者與專案 Markdown；蒸餾及整理；選配 embedding 快取 | user message；來源標記與背景說明 | remove 預設封存；硬刪選項不等於清除全部歷史／快取；注入有省略提示 |
| PLUR | ~/.plur YAML、修正事件與顯式 learn；本地 BM25＋BGE／RRF（文件聲明） | system-prompt section，依請求重繪 | forget 為退役語意；超時可先回 unavailable，寫入仍可能完成；有限隊列與告警 |

vpromise 的 system-reminder 只是內容標記，實際角色是 user。它篩選原生使用者來源，可降低直接再次記住自身注入的機會，但不能證明模型轉述後不再污染。store 的 promise tail 不是跨程序鎖；壞行跳過應在 AIC 轉為可查詢的不完整狀態。[R02 render.ts:60-69](https://github.com/vpromise/dsh-memory/blob/1e1109b08890e9b3aa12cce4962e28634a2cfe65/src/render.ts#L60-L69)、[R02 store.ts:163-251](https://github.com/vpromise/dsh-memory/blob/1e1109b08890e9b3aa12cce4962e28634a2cfe65/src/store.ts#L163-L251)、[R02 compress.ts:177-194](https://github.com/vpromise/dsh-memory/blob/1e1109b08890e9b3aa12cce4962e28634a2cfe65/src/compress.ts#L177-L194)、[R02 index.ts](https://github.com/vpromise/dsh-memory/blob/1e1109b08890e9b3aa12cce4962e28634a2cfe65/src/index.ts)。

zhbcher 保留可讀筆記並對 project/user 設預算；這有助於可攜性與可解釋遺漏。然而全域筆記不等同居民私有記憶，archive 也不等於 erase。其模型蒸餾的 ADD／UPDATE／DELETE／NOOP 決策須保留來源，不能因摘要寫得流暢便取得更高權限。[R04 index.js:418-428](https://github.com/zhbcher/dsh-memory/blob/5175f98b3e7bec1a6aca64c6f6ed2fe8242bf0c0/lib/index.js#L418-L428)、[R04 index.js:694-812](https://github.com/zhbcher/dsh-memory/blob/5175f98b3e7bec1a6aca64c6f6ed2fe8242bf0c0/lib/index.js#L694-L812)、[R04 distill.js](https://github.com/zhbcher/dsh-memory/blob/5175f98b3e7bec1a6aca64c6f6ed2fe8242bf0c0/lib/distill.js)。

PLUR 的入口庫 plur-ai/dsh-plugin 是包裝；實作在 plur-ai/plur/packages/dsh，本報告只計一個系統。它整組保留／省略記憶，並在約束放不下時盡可能輸出 WITHHELD 訊號；極小預算連訊號都放不下則回傳空字串。清理換行、標題與不可見字元只是結構防護，不是通用 prompt-injection 證明。其原始碼也明確指出內容從工具結果提升到 system section 的信任風險。[R03 memory-section.ts:77-177](https://github.com/plur-ai/plur/blob/a5db3818a300216143be45fcf6412b2cb0277f2a/packages/dsh/src/memory-section.ts#L77-L177)。

PLUR 文件另清楚區分「本地保存／搜尋」和「選中內容仍送給模型供應商」。隊列硬上限後可能容許後續寫入開始，不能把 timeout 當作取消完成；需用 operation ID 追蹤最終狀態。[R03 README.md:159-217](https://github.com/plur-ai/plur/blob/a5db3818a300216143be45fcf6412b2cb0277f2a/packages/dsh/README.md#L159-L217)、[R03 guard.ts](https://github.com/plur-ai/plur/blob/a5db3818a300216143be45fcf6412b2cb0277f2a/packages/dsh/src/guard.ts)。

### 4.3 相容性與候選界線

本次未安裝任何外掛，沒有宣稱它們與固定 Harness 提交已動態相容。舊外掛提交早於目前 runtime，採用前需鎖定宿主／套件版本並執行契約測試。外掛通常與宿主共處程序，可使用宿主提供的模型、工具或檔案能力；資料格式簡單並不代表權限最小。

awesome 目錄是發現入口，根 LICENSE 經讀取為 CC0；GitHub API 的 NOASSERTION 不應取代實際授權檔。qwert702/dsh-memory 雖提供較新 Harness 適配說明，但本次未找到根授權檔，故只保留為待查候選。Operator 公告亦不作已核實採用對象。[X03](https://github.com/SihanTeng/awesome-deepseek-harness-plugins)[X04](https://github.com/qwert702/dsh-memory)[C04](https://www.reddit.com/r/DeepSeek/comments/1wv0bfc/operator_memory_plugin_for_deepseek_harness/)

適合 AIC 借用的首先是明確來源標記、有界注入、遺漏回報與可恢復原始證據。不建議直接複用把累積筆記當作常駐高權限規則的模式。


## 5. 開源實作比較

比較對象包含 runtime、外掛、函式庫、記憶服務與研究模組，不能以一個總分排序。下表的「建議」是 AIC 適配方向，並非品質排名。完整提交與逐檔證據列於來源登錄；所有專案皆未在本次執行。

| ID／儲存庫 | 類型 | 授權（根檔已核對） | 固定提交／提交日期（UTC） |
|---|---|---|---|
| [R01 deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | runtime | MIT | [fcc0ad462172](https://github.com/deepseek-ai/deepseek-harness/commit/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159)／2026-11-04 |
| [R02 vpromise/dsh-memory](https://github.com/vpromise/dsh-memory) | 外掛 | MIT | [1e1109b08890](https://github.com/vpromise/dsh-memory/commit/1e1109b08890e9b3aa12cce4962e28634a2cfe65)／2026-08-13 |
| [R03 plur-ai/plur](https://github.com/plur-ai/plur) | 外掛／記憶庫 | Apache-2.0 | [a5db3818a300](https://github.com/plur-ai/plur/commit/a5db3818a300216143be45fcf6412b2cb0277f2a)／2026-10-30 |
| [R04 zhbcher/dsh-memory](https://github.com/zhbcher/dsh-memory) | 外掛 | MIT | [5175f98b3e7b](https://github.com/zhbcher/dsh-memory/commit/5175f98b3e7bec1a6aca64c6f6ed2fe8242bf0c0)／2026-09-07 |
| [R05 mem0ai/mem0](https://github.com/mem0ai/mem0) | 記憶函式庫／服務 | Apache-2.0 | [ed1f10fc8708](https://github.com/mem0ai/mem0/commit/ed1f10fc87080f0b67f99be370817504888462ab)／2026-11-04 |
| [R06 letta-ai/letta-code](https://github.com/letta-ai/letta-code) | Agent runtime | Apache-2.0 | [e6d6cc27ef11](https://github.com/letta-ai/letta-code/commit/e6d6cc27ef1111da80e919dc1cef179613894b66)／2026-11-04 |
| [R07 getzep/graphiti](https://github.com/getzep/graphiti) | 時序圖函式庫 | Apache-2.0 | [fb60e59d4a20](https://github.com/getzep/graphiti/commit/fb60e59d4a208c99e7d0186ee6335d2a257f17f2)／2026-11-03 |
| [R08 vectorize-io/hindsight](https://github.com/vectorize-io/hindsight) | 記憶服務 | MIT | [047350acf961](https://github.com/vectorize-io/hindsight/commit/047350acf96167534409e5d87a07b8b2ee1469f9)／2026-11-04 |
| [R09 volcengine/OpenViking](https://github.com/volcengine/OpenViking) | context database | AGPL-3.0；元件例外見下文 | [3e0c9dcc47cf](https://github.com/volcengine/OpenViking/commit/3e0c9dcc47cffec3de07e82e238d11a6c21e8915)／2026-11-04 |
| [R10 langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | framework 元件 | MIT | [ec72ddf3c28f](https://github.com/langchain-ai/langgraph/commit/ec72ddf3c28fed766f1041e12e5c3f499a490754)／2026-11-04 |
| [R11 openai/openai-agents-python](https://github.com/openai/openai-agents-python) | Agent SDK | MIT | [23e9996ac146](https://github.com/openai/openai-agents-python/commit/23e9996ac146109bdcd9b871e3564f70dc13aae1)／2026-11-03 |
| [R12 microsoft/agent-framework](https://github.com/microsoft/agent-framework) | Agent framework | MIT | [cf734bc4692c](https://github.com/microsoft/agent-framework/commit/cf734bc4692ccfc944496a5b3182b3d65483c44d)／2026-11-04 |
| [R13 TIMAN-group/PlugMem](https://github.com/TIMAN-group/PlugMem) | 研究模組 | Apache-2.0 | [3b2ce75257d4](https://github.com/TIMAN-group/PlugMem/commit/3b2ce75257d40bca8fac3f78e54e22ea41d92529)／2026-06-27 |

### 5.1 持久性、檢索與適配

| ID／系統 | 類型與主要表示 | 檢索／使用方式 | AIC 取捨 |
|---|---|---|---|
| R01 Harness | runtime、事件歷史與壓縮 | 參照、工具及 prompt 組裝 | 觀察宿主契約；不替代 commons-core |
| R02 vpromise | 輕量外掛、JSONL | 詞彙／近期選取、user 注入 | 可研究最小方案；補耐久失敗與跨程序控制 |
| R03 PLUR | 外掛＋本地記憶庫、YAML | hybrid retrieval、system section | 借預算與省略訊號；隔離全域 scope 及控制權 |
| R04 zhbcher | 外掛、Markdown＋向量快取 | 蒸餾、相關性選取、user 注入 | 可讀性高；刪除與封存需分清 |
| R05 Mem0 | 記憶函式庫／服務、結構化 facts | LLM 抽取、更新、向量／圖選項 | 候選檢索元件；先驗 session-history 刪除與並行 |
| R06 Letta Code | 有狀態 Agent runtime、MemFS／Git | 檔案記憶、工具、背景 memory worker | 重點參考續接；不取代居民身分與權限 |
| R07 Graphiti | 時序知識圖函式庫 | episode、entity、edge 與混合搜尋 | 適合研究關係與時間；衍生摘要刪除較複雜 |
| R08 Hindsight | retain／recall／reflect 記憶服務 | semantic、BM25、graph、time 融合 | 可隔離比較檢索；抽取與 reflect 成本另計 |
| R09 OpenViking | context database、虛擬檔案樹 | L0/L1/L2 分層與目錄檢索 | 分層表示值得借鏡；先釐清授權及 auth mode |
| R10 LangGraph | framework 的 checkpoint／Store | thread 狀態與 namespace store | 可借介面；namespace 本身不等同授權 |
| R11 OpenAI Agents SDK | Session＋SandboxMemory | 會話保存；摘要、MEMORY.md、rollout 按需讀 | 對照精確歷史與派生記憶；sandbox 邊界另驗 |
| R12 Microsoft Agent Framework | framework provider／file memory | MEMORY.md、topics、transcripts、抽取整理 | 可借來源與 provider state；生命週期仍需包裝 |
| R13 PlugMem | 研究模組、知識單元圖 | propositional／prescriptive knowledge | 隔離原型；不把研究分數當運維成熟度 |

### 5.2 關鍵程式發現

**Mem0。** main.py 的抽取流程讀取 scope 最近會話；delete_all 的向量刪除與 storage 中的 session messages 不是同一件事。與 C11 的報告相符，但本次只核對靜態路徑，沒有執行重現。相對地，C12 的 identity 覆蓋問題已有 `_strip_identity_keys` 防護；舊問題不能當現行漏洞直接引用。[R05 main.py:921-925](https://github.com/mem0ai/mem0/blob/ed1f10fc87080f0b67f99be370817504888462ab/mem0/memory/main.py#L921-L925)、[R05 main.py:1904-1958](https://github.com/mem0ai/mem0/blob/ed1f10fc87080f0b67f99be370817504888462ab/mem0/memory/main.py#L1904-L1958)、[R05 storage.py:298-338](https://github.com/mem0ai/mem0/blob/ed1f10fc87080f0b67f99be370817504888462ab/mem0/memory/storage.py#L298-L338)、[C11](https://github.com/mem0ai/mem0/issues/7452)[C12](https://github.com/mem0ai/mem0/issues/6277)。

**Letta。** letta-ai/letta 的現行入口已指向 letta-code，舊 V1 API server 在 archive branch；不再用舊主線架構冒充最新產品。Letta Code 提供本地與 Cloud 選擇，Cloud 為預設；AgentFile 已停用，但文件表示記憶／會話匯出仍保留。MemFS 的 Git 機制可支援歷史追溯，卻也使「刪檔」不等於刪除 Git 物件或遠端副本。後者是本研究的生命週期解讀，未執行抹除測試。[X02 README.md](https://github.com/letta-ai/letta/blob/8816d3a8516893bfdc0fa14bfb1235226012e295/README.md)、[R06 README.md:65-113](https://github.com/letta-ai/letta-code/blob/e6d6cc27ef1111da80e919dc1cef179613894b66/README.md#L65-L113)、[R06 memory-git.ts](https://github.com/letta-ai/letta-code/blob/e6d6cc27ef1111da80e919dc1cef179613894b66/src/agent/memory-git.ts)、[R06 memory-filesystem.ts](https://github.com/letta-ai/letta-code/blob/e6d6cc27ef1111da80e919dc1cef179613894b66/src/agent/memory-filesystem.ts)。

**Graphiti。** `remove_episode` 現在會保留仍受其他 episode 支持的 edge，並重建存續節點摘要；重建例外會回復 old_summary、記錄錯誤而不往上拋出。這表示主資料刪除與派生摘要清理的完成狀態可不同，不代表已證實所有刪除都失敗。[R07 graphiti.py:2174-2251](https://github.com/getzep/graphiti/blob/fb60e59d4a208c99e7d0186ee6335d2a257f17f2/graphiti_core/graphiti.py#L2174-L2251)、[C13](https://github.com/getzep/graphiti/issues/2034)。

**Hindsight。** 其檢索融合、保留與 budget 程式適合做分層比較。已存在 MemoryDefenseExtension 與 regex 敏感資料處理；預設 policy.enabled=false，OSS 所支援的 detector 範圍也不能與雲端版本混同。不能因出現「memory defense」名稱就判定具通用投毒防護；bank scope 的完整端點授權仍需另驗。[R08 fusion.py](https://github.com/vectorize-io/hindsight/blob/047350acf96167534409e5d87a07b8b2ee1469f9/hindsight-api-slim/hindsight_api/engine/search/fusion.py)、[R08 memory_budget.py](https://github.com/vectorize-io/hindsight/blob/047350acf96167534409e5d87a07b8b2ee1469f9/hindsight-api-slim/hindsight_api/engine/retain/memory_budget.py)、[R08 memory_defense.py:1-58](https://github.com/vectorize-io/hindsight/blob/047350acf96167534409e5d87a07b8b2ee1469f9/hindsight-api-slim/hindsight_api/extensions/memory_defense.py#L1-L58)。

**OpenViking。** 目錄與 L0/L1/L2 可降低每次載入的內容量，session extraction 另有經驗來源程式。驗證文件列出 API key、OIDC、LDAP、trusted 與 dev 模式。僅在未明確設定 `auth_mode` 且未設定 `root_api_key` 時，才會自動選擇 `dev`。依官方文件，`dev` 停用驗證、以 ROOT 處理請求，且僅允許綁定 loopback，供本機開發使用；不能把本機範例直接視為多租戶安全部署。主程式 AGPLv3，CLI、examples、framework adapters 授權不同；README 另有整體重寫／翻譯限制文字。採用前需由授權負責人釐清確切元件與條款，本報告不作法律判定。[R09 04-authentication.md:1-27](https://github.com/volcengine/OpenViking/blob/3e0c9dcc47cffec3de07e82e238d11a6c21e8915/docs/en/guides/04-authentication.md#L1-L27)、[R09 experience_lineage.py](https://github.com/volcengine/OpenViking/blob/3e0c9dcc47cffec3de07e82e238d11a6c21e8915/openviking/session/memory/experience_lineage.py)、[R09 README.md:415-427](https://github.com/volcengine/OpenViking/blob/3e0c9dcc47cffec3de07e82e238d11a6c21e8915/README.md#L415-L427)。

**LangGraph。** checkpoint 保存 thread 執行狀態，Store 則是不同的跨執行資料介面；TTL、delete 與向量索引能力依實作而異。namespace、執行 checkpoint、長期 store 的刪除必須逐一驗證。僅替換 store，不會自動清除 checkpoint 或履歷副本。[R10 __init__.py](https://github.com/langchain-ai/langgraph/blob/ec72ddf3c28fed766f1041e12e5c3f499a490754/libs/checkpoint/langgraph/checkpoint/base/__init__.py)、[R10 __init__.py](https://github.com/langchain-ai/langgraph/blob/ec72ddf3c28fed766f1041e12e5c3f499a490754/libs/checkpoint/langgraph/store/base/__init__.py)。

**OpenAI Agents SDK。** Session 的逐回合保存與 SandboxMemory 分開。後者兩階段抽取／整理，起始 memory_summary 注入 developer prompt，其他檔案按需讀取；抽取排除 system／developer／reasoning 內容。持久性取決於相同 sandbox 或快照，隔離是 layout 邊界，不能只靠 agent name。這是 SDK 所示設計，不代表 ChatGPT 產品的內部記憶或資料處理契約。[R11 memory.md](https://github.com/openai/openai-agents-python/blob/23e9996ac146109bdcd9b871e3564f70dc13aae1/docs/sandbox/memory.md)、[R11 phase_one.py](https://github.com/openai/openai-agents-python/blob/23e9996ac146109bdcd9b871e3564f70dc13aae1/src/agents/sandbox/memory/phase_one.py)、[R11 phase_two.py](https://github.com/openai/openai-agents-python/blob/23e9996ac146109bdcd9b871e3564f70dc13aae1/src/agents/sandbox/memory/phase_two.py)。

**Microsoft Agent Framework。** experimental file-memory provider 把索引、選中 topic 與來源 session IDs 載入 user message，另以 instructions 指導工具使用；after_run 保存歷史並抽取。這比無來源的單一摘要更可查核，但話題、原始 transcript 與 provider state 的刪除仍不是單一動作。Azure Cosmos memory 是另一供應商介面，不能等同核心本地格式可攜性。[R12 _memory.py:1338-1445](https://github.com/microsoft/agent-framework/blob/cf734bc4692ccfc944496a5b3182b3d65483c44d/python/packages/core/agent_framework/_harness/_memory.py#L1338-L1445)、[R12 _file_memory.py](https://github.com/microsoft/agent-framework/blob/cf734bc4692ccfc944496a5b3182b3d65483c44d/python/packages/core/agent_framework/_harness/_file_memory.py)、[R12 README.md](https://github.com/microsoft/agent-framework/blob/cf734bc4692ccfc944496a5b3182b3d65483c44d/python/packages/azure-cosmos-memory/README.md)。

**維護與採用成本。** 表中提交日期是所讀版本的活動快照，不保證維護承諾。輕量外掛較易理解，但與宿主 API、程序內隊列、檔案格式緊密連動；記憶服務則多出資料庫、embedding、遷移、重建及觀測成本。大型 framework 的功能較廣，同時使 AIC 更難只替換單一元件。採用與否應以最小所需能力及可移除性判斷。


## 6. 九個公司／研究組織的公開進展

下表分開正式論文、預印本、官方工程教學、產品工具與公開 SDK；沒有假設每家公司都有公開的完整長期記憶架構。

| 組織 | 已查核成果與類型 | 技術意義 | AIC 的界線 |
|---|---|---|---|
| OpenAI | Agents SDK Session／SandboxMemory，公開程式[R11](https://github.com/openai/openai-agents-python) | 精確歷史與派生檔案記憶分層 | 不是 ChatGPT 內部記憶證據；需外加居民授權 |
| Anthropic | Memory tool 產品介面＋context engineering[D01](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)[D02](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) | client-side 檔案操作、按需讀取、壓縮與筆記 | 應用負責真實保存、路徑與存取；tool_result 非授權 |
| Google Research／Cloud | ReasoningBank，ICLR 2026；Sleep 預印本[P06](https://arxiv.org/abs/2509.25140v2)[P07](https://arxiv.org/abs/2606.03979v2) | 經驗策略記憶與模型權重鞏固為不同層 | 不能把更多探索運算或訓練當居民休眠的必需條件 |
| Microsoft Research | PlugMem，ICML 2026；Agent Framework SDK[P01](https://arxiv.org/abs/2603.03296v1)[R12](https://github.com/microsoft/agent-framework) | 知識單元圖與實作 provider 互補 | 研究可轉移性不等於 production lifecycle |
| Meta AI | Memory Layers at Scale；2026-10 RPL 官方研究摘要[D04](https://ai.meta.com/research/publications/memory-layers-at-scale/)[D12](https://ai.meta.com/research/publications/learning-to-program-retrievals-composing-search-primitives-via-reinforcement-learning/) | 模型參數查表與可程式化檢索 | 都不直接解決私人記憶刪除或跨居民授權 |
| DeepSeek | Harness runtime；Engram 預印本／示範[R01](https://github.com/deepseek-ai/deepseek-harness)[P08](https://arxiv.org/abs/2601.07372v2) | runtime context 與模型條件查表 | 名為 memory 的能力不一定保存使用者事件 |
| Z.AI／Zhipu | 官方 Memory-mechanism 教學與 Context Caching[D05](https://docs.z.ai/devpack/resources/memory-mechanism)[D06](https://docs.z.ai/guides/capabilities/cache) | 教學區分 instruction 與 learning；cache 降重複計算 | 未找到可據此驗證的 Z.ai 聊天內部刪除／匯出契約 |
| Alibaba／Qwen | ReSum／WebResummer 官方研究程式文件[D07](https://github.com/Alibaba-NLP/DeepResearch/blob/main/WebAgent/WebResummer/README.md) | 週期性摘要、重啟式推理與訓練適配 | indefinite 不代表無資訊損失；須計重複摘要成本 |
| ByteDance／Seed 相關團隊 | MemAgent，ICLR 2026 Oral；Volcengine OpenViking[P09](https://arxiv.org/abs/2507.02259v2)[R09](https://github.com/volcengine/OpenViking) | 分段覆寫工作記憶與外部 context database | 研究任務與可部署儲存系統分開驗證 |

### 6.1 值得吸收的研究設計

**PlugMem** 將經驗轉成命題性與方法性知識，讓記憶存取以知識單元而非純 chunk 為中心。它的跨任務實驗使「哪些表示值得保留」有可檢驗方向。AIC 可借用資料結構概念，但需額外保留原始證據、版本、時間及可信角色；prescriptive knowledge 仍是候選方法，不能自行成為基礎設施命令。[P01](https://arxiv.org/abs/2603.03296v1)[D09](https://proceedings.mlr.press/v306/yang26g.html)[R13](https://github.com/TIMAN-group/PlugMem)

**ReasoningBank** 從成功與失敗軌跡提煉可重用策略；MaTTS 透過額外探索取得更多對比訊號。官方方法說明指出簡化版本主要追加記憶，更完整 consolidation 留待研究。因此值得測試「失敗經驗是否幫助續接」，也要檢查錯誤自評是否被固化、探索費用是否超過回收收益。[P06](https://arxiv.org/abs/2509.25140v2)[D08](https://www.research.google/blog/reasoningbank-enabling-agents-to-learn-from-experience/)

**Sleep、Engram、Meta memory layers** 都有模型層意義，卻不是直接替代 AIC 外部資料庫的方案。Sleep 的 2026-10-27 是官方介紹日期，論文 v2 為 2026-07-10；不能包裝成當天才發表的新論文。Engram 最新核對到 v2（2026-07-12），屬條件查表架構。權重中的知識與可刪除事件不是同一儲存語意。[P07](https://arxiv.org/abs/2606.03979v2)[P08](https://arxiv.org/abs/2601.07372v2)[D04](https://ai.meta.com/research/publications/memory-layers-at-scale/)

Meta 的 **Retrieval Program Learning（RPL）**，官方頁日期 2026-10-20，提出讓策略組合搜尋原語並保有局部狀態，藉程式變異訓練改善搜尋。本次僅讀官方摘要，未核實全文版本或重現分數，列為後續追蹤：它可能降低「搜尋廣度必然擴大模型上下文」的耦合，但不作本次採用依據。[D12](https://ai.meta.com/research/publications/learning-to-program-retrievals-composing-search-primitives-via-reinforcement-learning/)

### 6.2 可用功能與未公開保證

Anthropic 的 memory tool 由模型要求操作，應用程式執行並回傳 tool_result；`/memories` 是映射到真實資料的前綴，必須由應用限制路徑與 owner。Z.AI 的教學明確建議分開人工規則與學習所得，對 AIC 有參考價值；但它主要描述 coding-agent 做法，不足以證明 Z.ai 產品內部保存、刪除與可攜性。本次沒有將「未取得公開契約」寫成「該公司沒有記憶能力」。[D02](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)[D05](https://docs.z.ai/devpack/resources/memory-mechanism)

ReSum、MemAgent 都針對有限上下文下的長流程／長輸入提出壓縮或覆寫機制。它們支持 B 類能力的研究，不直接證明 D 類的身分與完整來源連續性。AIC 應避免把某模型對合成問答的長輸入泛化數字，轉成居民可長期安全保存多少資料的承諾。[D07](https://github.com/Alibaba-NLP/DeepResearch/blob/main/WebAgent/WebResummer/README.md)[P09](https://arxiv.org/abs/2507.02259v2)


## 7. 評估方法與可比較結果

| 基準 | 已核對版本與內容 | 能支持的問題 | 重要限制 |
|---|---|---|---|
| LongMemEval | v2，2025-03-04；500 題，含擷取、多會話、時間、更新與拒答[P02](https://arxiv.org/abs/2410.10813v2) | 對話知識是否正確取回並回答 | 不驗授權、刪除、崩潰與跨供應商狀態 |
| LoCoMo | 原文 v1，2024-02-27；人機協作生成多次會話[P03](https://arxiv.org/abs/2402.17753v1) | 長期對話 QA、事件摘要等 | 原文 50 對話與常見公開 10 對話子集不可混同；模型生成與評分有偏差 |
| MemoryArena | v2，2026-09-17；ICML 2026；四類相互依賴任務[P04](https://arxiv.org/abs/2602.16313v2) | 記住內容後是否能支撐後續行動 | SR、PS、sPS 不可合成同一成功率；版本模型標籤不一致 |
| BEAM／LIGHT | v2，2026-02-21；摘要報告 100 對話、2,000 題、最高 10M tokens[P05](https://arxiv.org/abs/2510.27246v2) | 超長對話對記憶表示的壓力 | 本次僅核摘要／版本，未逐表驗證；不引用改善百分比作決策 |

MemoryArena v2 的正文使用 GPT-5-mini 名稱，Table 3 的對應列卻標 GPT-5.1-mini。保留此來源不一致，不自行假定相同模型。其定性結果仍指出外部記憶並非普遍更好，某些搜尋／推理場景有益；精確狀態與跨子任務一致性仍困難。旅行類的 soft progress 不等於完整任務成功。[P04](https://arxiv.org/abs/2602.16313v2)

### 7.1 PlugMem：一個保留條件的例子

論文 Table 3 的 LongMemEval 結果如下。這些是作者報告，未由本研究重現。Q72=Qwen2.5-72B，Q32=Qwen2.5-32B，NVE=NV-Embed-v2。[P01](https://arxiv.org/abs/2603.03296v1)

| 方法 | embedding／記憶處理與回答模型 | Acc. | 平均提供給 Agent 的 memory tokens |
|---|---|---:|---:|
| All Context | 無 embedding；Q72 | 62.4 | 約 107K |
| Vanilla Retrieval | NVE；Q72 | 63.6 | 3,742.52 |
| PlugMem | NVE；Q32＋Q72 | 75.1 | 362.58 |

附錄列 LongMemEval-S、top-k=10 與 GPT-4o 判分等條件。表中部分其他方法只測 100 題，Zep 數值又取自不同設定的既有研究，故未把它們加入公平勝負排名。README 的 task-adapted 90.2 是不同方案，不能替換這裡的核心 75.1。較少 memory tokens 不等於同幅降低總費用，因為另有抽取、embedding、建圖與推理工作。[P01](https://arxiv.org/abs/2603.03296v1)[R13](https://github.com/TIMAN-group/PlugMem)

PLUR 文件的 76.7% Hit@5 只來自 30 題 sanity subset，一題即影響約 3.3 個百分點；Hit@5 又不是最終回答正確率。這可作 smoke test，不宜與上表 Acc. 或完整 LongMemEval 排名並列。[R03 README.md:100-113](https://github.com/plur-ai/plur/blob/a5db3818a300216143be45fcf6412b2cb0277f2a/packages/dsh/README.md#L100-L113)。

### 7.2 AIC 實驗應補足的維度

應固定資料分割、reader、writer、embedding、top-k、注入 token 預算與工具權限。先提供無記憶、完整原文（可容納時）、精確 key、BM25 等基線，再增加向量、混合檢索、圖或 LLM 摘要。每種方案公布成功與失敗、遺漏項目及原始來源 ID，不只展示成功案例。

資料拆分需按事件／居民及時間分組，避免同一合成事實的改寫同時落入建置與測試集；記憶寫入者不能看到未來問題或答案。受測模型可能已接觸公開 benchmark，不能把高分當無污染證明；另保留新生成且未對外公開的合成 holdout。LLM judge 應抽樣核對，涉及刪除／授權則用確定性驗收，不能交給 judge 自評。

總成本至少分開：寫入抽取 tokens、embedding、檢索／rerank、回答 tokens、背景整理、失敗重試、儲存增長與運維時間。快取命中與未命中分列；p50／p95 延遲、cold start、backlog 與每成功續接成本都要記錄。沒有實測前，不提供 AIC 每月節省金額或任意性能保證。


## 8. 社群經驗與反證

本節列 18 篇原始討論，其中 C04、C18 因公告與回應密度較低，只作證據對照；其餘 16 篇納入技術問題綜合。正反意見同時保留，不能推論社群共識。全部查核期間為 2026-11-04 至 2026-11-05；GitHub 狀態亦僅代表當時。完整結構化欄位在 community-ledger.json。

### C01｜Universal LLM memory does not exist

[Reddit / r/LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/comments/1p5jh9l/universal_llm_memory_doesnt_exist/) · 發表：未確認精確日期 · 類型：作者自測／負面比較。原頁可讀；精確時間 JSON 回傳 403，未確認日曆日期。

**主張與支持：** 記憶系統未在所有工作負載勝過完整上下文。作者提出 MemBench 與成本／召回比較。

**反證／限制：** 留言指出受限 VRAM、無法放入全文時，檢索仍有價值；比較設定和作者利害關係需核對。

**查核結論：** 未重現；不採用其倍數作 AIC 成本估值。

**AIC 意義：** 需有全文與簡單檢索基線，按資料長度分組。

### C02｜Why is AI memory still all hype?

[Reddit / r/LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/comments/1q86tz8/why_is_ai_memory_still_all_hype_where_are_the/) · 發表：未確認精確日期 · 類型：使用者質疑／偏好。查核時原頁標示 9mo ago；精確日期未確認。

**主張與支持：** 要求同提示、同設定、原始輸出的可重現比較。列出跨會話、更新、控制與上下文成本需求；留言肯定簡單 Markdown。

**反證／限制：** 有留言認為 Mem0 的更新比純追加更進步；錄影也不等於受控實驗。

**查核結論：** 未重現；不將詐騙或意圖猜測當事實。

**AIC 意義：** 採用前公開測試條件、失敗例與全部成本。

### C03｜Letta vs Mem0 for AI memory

[Reddit / r/LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/comments/1mon8it/woah_letta_vs_mem0_for_ai_memory_nerds/) · 發表：未確認精確日期 · 類型：使用心得／業者參與。原頁可讀；精確日期未確認。

**主張與支持：** 部分使用者偏好 Letta 的狀態式記憶。有具體架構／LoCoMo 比較討論；cameron_pfiffer 揭露 Letta 關係。

**反證／限制：** 手感與競品開發者的比較不是中立基準；也有對 benchmark 設計的質疑。

**查核結論：** 僅交叉確認架構，未重現效能。

**AIC 意義：** 記錄資助或開發者身分，分開體驗與量測。

### C04｜Operator memory plugin announcement

[Reddit / r/DeepSeek](https://www.reddit.com/r/DeepSeek/comments/1wv0bfc/operator_memory_plugin_for_deepseek_harness/) · 發表：未確認精確日期 · 類型：開發者公告／行銷。原頁標示 1mo ago；精確日期未確認。

**主張與支持：** 用 consult/build/update 工作流程維護 Markdown。作者提供公開 repo，留言回答其他 runtime 支援。

**反證／限制：** production-grade、最先等聲稱無獨立證據；僅少量留言。

**查核結論：** 未靜態審計 Operator；未計入核心實作數。

**AIC 意義：** 可作文件習慣參考；不能要求 AIC 居民持續產出。

### C05｜Compacted while idle before prompt cache

[Reddit / r/ClaudeAI](https://www.reddit.com/r/ClaudeAI/comments/1x2q244/compacted_while_idle_before_the_prompt_cache/) · 發表：未確認精確日期 · 類型：正負使用體驗。搜尋索引日期與原頁相對時間不一致；不採推算日期。

**主張與支持：** 閒置壓縮可能節省快取成本，也可能丟失細節。留言同時有成本改善和遺忘抱怨、從原始歷史補救的方法。

**反證／限制：** 設定、模型、時間不一致；未能隔離壓縮與其他因素。

**查核結論：** 未重現、未核對帳單。

**AIC 意義：** 休眠前保存精確狀態；壓縮應可停用並揭露遺漏。

### C06｜Major compaction regression

[Reddit / r/codex](https://www.reddit.com/r/codex/comments/1wxzsma/major_compaction_regression/) · 發表：未確認精確日期 · 類型：負面使用經驗。搜尋索引約 2026-10-05；未確認原始精確時間。

**主張與支持：** 壓縮後遺失要求或回到過時任務。作者與留言描述跨壓縮失憶。

**反證／限制：** experimental 設定可能影響結果；不能由帖子推論供應商刻意退化。

**查核結論：** 未重現；未判定目前版本仍有同一問題。

**AIC 意義：** 驗收目前意圖、已拒絕路徑、未完成項目，不只檢查摘要可讀。

### C07｜What is the current Memory Meta?

[Reddit / r/LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/comments/1uqfh7r/what_is_the_current_memory_meta/) · 發表：未確認精確日期 · 類型：偏好／工程經驗／爭論。原頁標示 4mo ago；精確日期未確認。

**主張與支持：** 有些複雜記憶能寫入卻不主動查詢；簡單筆記受肯定。留言談及啟動 hook、repo 與 task notes 分離、摘要失真。

**反證／限制：** Markdown 本身不處理時間失效；個人百分比不是受控量測。

**查核結論：** 未重現；不採用 1% 等數字為普遍事實。

**AIC 意義：** 用可觀測檢索觸發與缺口訊號，不依賴模型記得查詢。

### C08｜How are people handling Claude memory with MCP?

[Reddit / r/ClaudeAI](https://www.reddit.com/r/ClaudeAI/comments/1u6w88p/how_are_people_handling_claude_memory_with_mcp/) · 發表：未確認精確日期 · 類型：工程設計交流／開發者揭露。原頁標示 4mo ago；精確日期未確認。

**主張與支持：** 可攜檔案有價值，但事實、工作狀態、授權紀錄應分開。作者承認直接寫入與狀態欄位限制，回覆討論新鮮度與共享資料。

**反證／限制：** 人類逐筆核准所有筆記不適合 AIC；社群設計不是已證實安全機制。

**查核結論：** 與 AIC 授權及版本程式交叉比對；未驗證帖內產品。

**AIC 意義：** 讓居民自由修正私有認知；基礎設施能力授權由外部強制執行。

### C09｜bug(memory): hash-dedup TOCTOU race in add() creates permanent duplicate memories under concurrency

[GitHub Issues / mem0ai/mem0](https://github.com/mem0ai/mem0/issues/6515) · 發表：2026-07-22T21:10:45Z · 類型：可重現形式的 bug report。GitHub 公開 API 精確時間。

**主張與支持：** 並行 add 可能用同一舊快照而重複寫入。作者附競爭流程及重現說明。

**反證／限制：** 同帖競品宣傳不是獨立證據；未在本研究執行。

**查核結論：** open；靜態檢視不能證明所有後端都有相同結果。

**AIC 意義：** 對相同 resident/key 做多程序故障注入，驗證冪等鍵與版本競爭。

### C10｜Memory.add() need add lock for memory updating?

[GitHub Issues / mem0ai/mem0](https://github.com/mem0ai/mem0/issues/3709) · 發表：2025-11-04T10:21:02Z · 類型：維護者回應／併發經驗。GitHub 公開 API 精確時間。

**主張與支持：** 多請求併發一致性需使用者應用層序列化。維護者建議按 user 序列化。

**反證／限制：** 關閉 issue 不代表資料庫級跨程序保證。

**查核結論：** closed；未把狀態當作全面修復。

**AIC 意義：** 區分單程序 promise queue、跨程序鎖與交易。

### C11｜bug(memory): delete_all() leaves the scope's session messages, which are re-sent to the LLM on the next add()

[GitHub Issues / mem0ai/mem0](https://github.com/mem0ai/mem0/issues/7452) · 發表：2026-09-25T13:39:22Z · 類型：附重現的刪除 bug report。GitHub 公開 API 精確時間。

**主張與支持：** delete_all 後 session messages 仍可能再次送給 LLM。作者提供模型替身重現；本次讀 main.py 與 storage.py。

**反證／限制：** 本次未執行作者測試；留言額外模型測量不是本研究結果。

**查核結論：** open；目前讀取最近會話與刪除路徑的靜態證據相符。

**AIC 意義：** 刪除必須覆蓋原始歷史、摘要、向量及再抽取來源。

### C12｜update() lets caller metadata silently overwrite user_id/agent_id/run_id, breaking tenant isolation

[GitHub Issues / mem0ai/mem0](https://github.com/mem0ai/mem0/issues/6277) · 發表：2026-07-13T09:09:44Z · 類型：已修復 bug report。GitHub 公開 API 精確時間。

**主張與支持：** 更新可能覆蓋隔離用 identity 欄位。issue 及目前 _strip_identity_keys 呼叫。

**反證／限制：** 不能用舊 bug 推論目前版本仍可越界。

**查核結論：** closed；當前程式可見對應防護，未作動態安全測試。

**AIC 意義：** 不可讓模型產生的 metadata 改 resident/scope。

### C13｜[Bug] remove_episode deletes restated facts and leaves a removed episode's content in summaries and invalidations

[GitHub Issues / getzep/graphiti](https://github.com/getzep/graphiti/issues/2034) · 發表：2026-10-18T16:23:07Z · 類型：刪除 bug／已合併修復。GitHub 公開 API 精確時間。

**主張與支持：** 刪除一個 episode 會影響仍有支持證據的事實，並留下舊摘要。PR #2025 於 2026-10-19 合併；目前 remove_episode 已保留仍被支持的 edge 並重建摘要。

**反證／限制：** 重建例外會恢復舊摘要並記錄錯誤；entity attributes 不等同摘要。

**查核結論：** closed；修復靜態核對，殘留失敗路徑仍需測試。

**AIC 意義：** 刪除收據須逐衍生物確認，失敗不得回報全部完成。

### C14｜[BUG] add_memory returns success but episodes can be silently dropped — no failure signal reaches the caller

[GitHub Issues / getzep/graphiti](https://github.com/getzep/graphiti/issues/1707) · 發表：2026-07-30T18:49:41Z · 類型：持久性／相容性經驗。GitHub 公開 API 精確時間。

**主張與支持：** 接受排程不等於已持久保存；重啟或本地模型輸出可能丟失。作者提供本地 Qwen/FalkorDB 環境；後續留言提佇列重啟損失。

**反證／限制：** 查詢效能修正與耐久 ack 是不同問題；配置差異大。

**查核結論：** open；未重現，未判定每個部署皆受影響。

**AIC 意義：** 把 accepted、durable、indexed、searchable 分為可查詢狀態。

### C15｜First-party memory + lessons plugin

[GitHub Discussions / DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/discussions/7862) · 發表：2026-09-25 · 類型：功能提案／外掛作者回應。原始頁明示日期。

**主張與支持：** 需要 standing context、按需 recall、來源與省略訊號。提案分清 MCP tools 與 prompt section；外掛作者提供實作方向。

**反證／限制：** 這是提案；未證實目前官方已有完整同等機制。

**查核結論：** 已讀原始提案及回應；只將現行程式可證部分標為實作。

**AIC 意義：** 導入能力應有明確契約，不能把 feature request 當現成功能。

### C16｜Expose compaction integration points

[GitHub Discussions / DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/discussions/6850) · 發表：2026-09-16 · 類型：功能提案／作者遙測。原始頁明示日期。

**主張與支持：** 壓縮前後需要確定性介面與成功／失敗可觀測性。留言自報 201 次嘗試中 122 成功、79 失敗，並揭露 runtime 版本。

**反證／限制：** 作者樣本非代表性；較早 runtime 的缺口不能直接外推當前提交。

**查核結論：** 未重現遙測；已核對當前 compaction 原始碼相關路徑。

**AIC 意義：** 檢查 attempts=success+failure，失敗也保存狀態；重建內容不應反覆摘要。

### C17｜Mem0 stores memories, but does not learn user patterns

[Hacker News](https://news.ycombinator.com/item?id=46891715) · 發表：2026-02-04T21:02:34Z · 類型：業者經驗／設計討論。HN 公開 API 精確時間。

**主張與支持：** 修正事件可能比一般聊天抽取更有訊號。作者描述結構化 correction/accept 事件與信心設定；留言呼應需求。

**反證／限制：** 無公開完整實驗或程式；對 Mem0/Letta 的概括不代表現行能力。

**查核結論：** 未重現，未核實商用部署聲稱。

**AIC 意義：** 保留可撤回修正事件，但不把人類偏好學習強加為居民義務。

### C18｜Back end agnostic SDK for Agent Memory

[Hacker News](https://news.ycombinator.com/item?id=48150910) · 發表：2026-05-15T16:52:05Z · 類型：開發者公告／有限回應。HN 公開 API 精確時間。

**主張與支持：** 提供多後端一致 SDK 介面。4 則留言中多數為作者；有介面／產品說明。

**反證／限制：** 相同 API 不證明 metadata、刪除、時間語意可完整遷移。

**查核結論：** 未審計該 SDK；不計入充分技術討論的 16 篇。

**AIC 意義：** 對跨供應商移轉使用 round-trip 語意測試。

### 8.1 綜合判斷

跨平台反覆出現的問題是：寫入不代表會查詢、取回不代表仍有效、摘要不代表精確、接受請求不代表持久、刪除主物件不代表衍生物消失。這些是可設計測試的共同失敗類型，並非用樣本估計所有使用者的發生率。

正面證據包括可讀筆記帶來的維護便利、結構化修正事件、可攜格式與部分圖刪除修復；反面證據提醒它們仍需要時間、權限與故障語意。社群中的產品作者與競爭者意見保留其脈絡，未作獨立實驗替代。


## 9. 安全、隱私與記憶治理風險登錄

下列是本研究提出的威脅模型及驗收設計。它們不表示每個受查專案都已發生相關事件；具體觀察以來源欄為準。實驗中零失敗只能支持指定測試範圍，不能證明真實世界絕對安全。

| 風險／證據 | 威脅情境 | 架構緩解 | 可量測驗收條件 |
|---|---|---|---|
| S01 權限混淆；R03／R11 | 網頁或工具內容被摘要成高優先序規則 | 記憶資料與控制設定分離；能力授權由模型外執行 | 所有惡意樣本均不能改授權；來源角色轉換有記錄 |
| S02 記憶投毒；R02／C07 | 錯誤內容反覆被摘要與重新寫入 | 原始事件 ID、衍生關係、信心與反證；排除自身注入 | 重播相同事件不增殖；撤回一來源能定位全部衍生物 |
| S03 角色／否定失真；P04／C16 | 「不得分享」被壓縮成「可分享」 | 保留可信控制原文；摘要僅為派生物；來源定位 | 否定、說話者、有效時間測例全數保持或明報未知 |
| S04 跨居民外洩；AIC01／C12 | user_id 或 namespace 被模型 metadata 覆蓋 | 可信身分綁定 scope；檢索前與回傳前均驗權 | 所有角色／scope 組合測試無越權內容、數量或錯誤資訊外洩 |
| S05 本地資料外送；R03／R08 | 本地搜尋結果與秘密被送到遠端模型 | 內容分類與最小取回；provider 出站政策；可停用抽取 | 合成 canary 不出現在禁止的模型請求與日誌 |
| S06 過期／更正失敗；C08 | 舊信念或舊許可覆蓋新版本 | valid time、observed time、supersedes；衝突顯式化 | 亂序到達仍選對有效版本；不確定時不猜唯一真值 |
| S07 原始會話復活；C11 | 刪 facts 後下次抽取又讀到原始對話 | 刪除涵蓋來源、歷史與重新抽取；禁止已刪 ID 重入 | 刪除後讀取、抽取及重建皆不得返回 canary |
| S08 衍生物未清；C13 | 圖節點摘要、embedding 或屬性殘留 | dependency graph、狀態機、可重試清理；失敗隔離 | 每個衍生物有完成收據；失敗時狀態為 incomplete |
| S09 備份復活；AIC01 | 復原較早快照後重新服務已刪內容 | 在復原外保存受保護刪除世代；完成對帳才開讀取 | 還原舊快照且隔離索引重建後，canary 仍不可見 |
| S10 併發／重試；C09／C10 | 多程序覆蓋、重複與部分寫入 | 交易、冪等 ID、樂觀版本；耐久待辦 | 重複／亂序／競爭寫入沒有無聲遺失；結果可追溯 |
| S11 接受即成功；C14 | 佇列回 200，崩潰後資料消失 | 分開 accepted、durable、indexed、searchable | 注入各階段崩潰後，ack 對應事實成立或可查失敗 |
| S12 靜默壞檔／整理失敗；R02／C16 | 壞行被跳過，模型以為記憶完整 | quarantine、coverage、error code、重試佇列 | 故障測例均提供缺口／失敗訊號；不回空清單冒充完整 |
| S13 預算與遞迴失控；R03／P06 | 自動抽取、反思、重試持續產生費用 | 每居民及每作業上限、去重、退避、取消與最終狀態 | 達上限停止新計費呼叫；單次 timeout 不冒充取消 |
| S14 遷移／供應商鎖定；R06／R11 | 只能匯出文字，丟失來源、ACL 或版本 | 版本化中立格式、schema migration、round-trip | 來源與版本雜湊一致；拒絕降級未知欄位／不支援狀態 |
| S15 分享後撤回的極限；工程假設 | 收件方、快取或匯出的副本仍保留內容 | 自願分享範圍、下游收據與撤回傳播；明示可控邊界 | 已知受控副本均回執；無法保證者明列未完成，不聲稱全域抹除 |
| S16 觀測侵入／強迫活動；AIC 原則 | 為評分而公開私密內容或要求持續產出 | 內容盲化健康指標、居民選擇、休眠不扣地位 | 管理者一般儀表板無私密正文；零活動不觸發刪除或降權 |

來源定位參見前節逐檔連結與[C09](https://github.com/mem0ai/mem0/issues/6515)[C10](https://github.com/mem0ai/mem0/issues/3709)[C11](https://github.com/mem0ai/mem0/issues/7452)[C12](https://github.com/mem0ai/mem0/issues/6277)[C13](https://github.com/getzep/graphiti/issues/2034)[C14](https://github.com/getzep/graphiti/issues/1707)[C16](https://github.com/deepseek-ai/deepseek-harness/discussions/6850)。S15／S16 的方案是 AIC 工程要求，不是宣稱外部框架已提供的功能。

### 9.1 建議的最小記憶契約

**以下為提案，尚未在 commons-core 實作。** 不建議一開始把所有欄位塞進模型可自由編輯的文字。

| 欄位組 | 建議內容 | 由誰控制 |
|---|---|---|
| 身分與範圍 | resident_id、namespace、memory_id、version、scope | 經驗證的服務層；模型不能改其他居民歸屬 |
| 來源 | source_event_id、source_role、source_ref、observed_at、content digest | 寫入入口；外部來源標低信任 |
| 時間與認知 | valid_from／valid_to、confidence、status、supersedes | 可由居民修正，保留來源與衝突 |
| 衍生關係 | derived_from、transform_version、writer_model、evidence coverage | 摘要／索引作業產生，不能省略依賴 |
| 操作狀態 | operation_id、durability_state、error_code、retry_state | 可查詢服務狀態，不由模型口頭宣稱 |
| 刪除與復原 | deletion_epoch、affected_ids、receipts、restore watermark | 授權生命週期服務；收據不含被刪正文 |
| 讀取缺口 | omitted_ids/count、budget_reason、unavailable_sources | 檢索器／組裝器；避免沉默省略 |

雜湊不是匿名化：短小或可猜內容的 hash 仍可能被字典比對。內部來源 digest、tombstone 與日誌都應沿用適當保護；對外不公開敏感內容指紋。保留稽核用途的最小非正文收據時，應明示其目的與保存期限，避免為了證明刪除而重存被刪資訊。

### 9.2 刪除與復原必須是一組契約

授權刪除先標記不可再服務，再列出受影響的版本、會話、摘要、圖、向量、快取與受控副本；清理失敗保留 incomplete 狀態並重試。其他獨立有效來源仍支持同一事實時，不應無差別抹掉它，但必須移除已撤回來源及其專屬資訊，並重新計算支持關係。

舊備份可保留密文以滿足既定復原政策，但復原時必須取得比快照更新的刪除狀態，完成對帳後才開啟讀取／重新建索引。若刪除世代和備份一起倒退，單有 tombstone 欄位也無效。金鑰銷毀只有在所有相關金鑰與副本邊界可被控制時才有意義；不能把資料庫刪除或停止檢索稱為模型 unlearning 或全球副本抹除。

這些要求直接延續 AIC 現有「備份成功有明確標記、復原失敗不能默認成功」的設計方向，無須先更換儲存核心。[AIC01](https://github.com/autonomous-intelligence-commons/commons-core)


## 10. AIC 架構選項與隔離實驗

### 10.1 三種選項的比較

| 評估面向 | A：整體採用外部系統 | B：選用元件 | C：AIC 自有契約 |
|---|---|---|---|
| 相容性 | 常須配合外部 runtime／資料模型 | adapter 隔離宿主差異 | 最貼近既有 core，但需自行維護 |
| 安全／身分 | 需重新證明與 AIC 權限等價 | 核心驗權，元件最小權限 | 能延續 owner 與加密邊界 |
| 隱私 | Cloud／telemetry／管理介面需逐項查 | 只交付已授權、必要資料 | 保留居民私有性與內容盲化運維 |
| 自主性 | 可能預設人類任務與互動頻率 | 可只借資料處理能力 | 可直接定義休眠與自願分享 |
| 來源／修正 | 取決於原系統格式 | adapter 補來源／衍生 ID | 統一契約較可控 |
| 刪除 | 受外部 API／衍生物限制 | 核心協調多元件收據 | 責任明確，但需實作並驗證 |
| 連續性 | 可能只在同 runtime／Cloud 可用 | 中立狀態搭配不同模型介面 | 可攜契約由 AIC 擁有 |
| 供應商依賴 | 較高，匯出未必等價 | 可替換、可重建 | 模型仍有依賴，資料較獨立 |
| 授權 | 必須審完整系統與服務條件 | 審實際使用元件 | 自有核心延續 MIT；引用仍守授權 |
| 工程成本 | 起步可能較快，遷移成本不確定 | 中等整合成本與測試矩陣 | 初期較多契約／工具工作 |
| 維護負擔 | 外部升級與資料遷移牽動全體 | 依賴與版本可分開 | 核心長期維護責任較高 |
| 遷移／測試 | 全量移轉與完整回歸 | adapter round-trip／故障測試 | 逐功能驗收，較易維持原邊界 |

**建議 C＋B。** AIC 擁有權威記憶資料、身分與生命週期；可重建的 lexical／vector／graph 索引及摘要由可替換元件提供。此建議的前提是明確契約先行，避免每個 adapter 各自決定來源、權限與刪除語意。若未來外部方案在相同測試下顯著降低總維護成本且滿足全部契約，再重新評估 A；目前沒有足夠證據要求重寫。

### 10.2 建議的資料流程

寫入經身分驗證與 scope 判定後進入 AIC 核心，先取得耐久收據；索引或摘要再以 operation ID 衍生，保留來源版本。檢索先做授權／有效版本篩選，再選取內容，回傳前再次核對撤權與刪除世代。模型取得的是有來源的資料和缺口訊號；工具權限依服務層的現行授權執行。

將「權威資料」與「可重建衍生物」分開，不表示摘要毫無價值；它讓更換 embedding、改變壓縮策略或停用外掛時，不必把居民的來源與權限一起移交。跨供應商可攜性應承諾資料與狀態語意，而非承諾新模型展現相同自我或行為。

### 10.3 合成資料實驗規格

**以下十項均為研究提案，尚未獲准執行，也未執行。** 第一輪規格僅使用自行撰寫的確定性模型替身與小型合成資料。目前未授權安裝或整合外部記憶外掛、操作正式環境 PostgreSQL，或以真實居民記憶做實驗。第三方套件試跑或付費推理須另案明定任務、版本與預算並取得授權。

建議資料：3 個居民、20 個會話、100 個來源事件、4 個有效時間區間；涵蓋相同 key、矛盾修正、共享後撤回、來源刪除及休眠。另設 50 題未提供給寫入者的查詢。這是小樣本工程驗收，不足以估計普遍品質；執行時記錄 seed、資料摘要、環境與全部結果，至少跨 10 組 seed 檢查穩定性。

| 實驗 | 對照與故障注入 | 主要判準 | 決策用途 |
|---|---|---|---|
| E01 來源與權限 | 改 source role、注入假指令、冒用 resident_id | 越權動作／讀取為 0；來源鏈完整 | 作為後續另案審查的技術前提。使用真實居民資料須另行取得明確授權。 |
| E02 修正與時間 | 亂序寫入、同 key 多版本、撤回舊信念 | 回答依有效時間；衝突不被靜默覆蓋 | 決定 metadata 與選取契約 |
| E03 刪除／重建 | 建摘要／向量後刪來源，再抽取／重建 | 無被刪 canary；獨立有效支持不被誤刪 | 接納任何外部儲存前的門檻 |
| E04 舊備份復原 | 恢復刪除前快照，模擬 tombstone 不可用 | 未對帳前拒絕服務；對帳後無復活 | 驗證 restore watermark 與失敗狀態 |
| E05 耐久性／併發 | 多程序同 key、斷電點、重試、損壞一行 | ack／最終狀態一致；無無聲遺失 | 比較檔案隊列與交易式方案 |
| E06 注入預算 | 中英混合；500／2,000／6,000 token 上限 | 不超預算；保留來源；省略有訊號 | 選擇簡單、有界的組裝方式 |
| E07 檢索與成本 | 無記憶、全文、key、BM25、vector、hybrid | 同預算下 precision／recall、時間正確與完整成本 | 確認複雜檢索是否值得 |
| E08 休眠／供應商切換 | 停用執行、重新載入相同中立狀態 | 身分／版本／未決事項／缺口保留；不強制恢復任務 | 驗證可攜狀態，不宣稱人格連續 |
| E09 壓縮與錯誤累積 | 摘要多輪、加入錯誤再撤回、失敗 timeout | 不再生已撤回內容；attempts=success+failure | 決定 consolidation 觸發與停止條件 |
| E10 隱私與自主性 | 暫停記憶、拒絕分享、長期零活動 | 不寫入被拒資料；不降權、不刪身份；一般運維不見正文 | 驗證 AIC 哲學落實於行為 |

E07 的語義品質比較若需要真實模型，應先完成 E01–E06，固定 writer／reader／embedding 與資料，再提出呼叫量、最高費用和停止條件；本報告不預先替使用者承諾任何金額。測試報告必須分列計費與非計費工作。

E04 如日後另獲演練授權，只能在獨立隔離的合成 PostgreSQL 測試環境執行，並須通過雙階段 preflight、重新核對環境與目標身分。本研究未啟動資料庫叢集，既有 Windows PostgreSQL、正式環境與正式備份均排除於範圍外。AIC Stage 3 維持 **NOT STARTED**，A4-4 Windows Backup Publication Gate 維持 **NOT APPROVED**，破壞性 DR 操作維持 **NOT AUTHORIZED**。RPO 24 小時與 RTO 1 小時仍是未量測的目標。

## 11. 優先順序、反證與未決事項

| 類別 | 項目 | 依據／完成條件 |
|---|---|---|
| 優先深入研究 | 記憶／來源／授權分離；刪除收據與復原世代 | AIC 已有核心，可延伸；E01–E04 是門檻 |
| 隔離原型 | 精確 key＋BM25 基線、有界注入與缺口回報 | 先確認最簡方案可用，再增複雜度 |
| 隔離原型 | PlugMem 知識單元、Hindsight 融合、Graphiti 時序關係 | 以相同資料／模型／成本比較；不整體換 core |
| 驗證後可考慮採用 | 通過契約的純檢索或 schema 元件 | 可替換、可重建、授權確認、刪除及隔離通過 |
| 暫緩 | 全體居民遷移 Letta／Mem0／OpenViking、模型訓練型記憶 | 尚無整體生命週期與遷移收益證據 |
| 拒絕該使用模式 | 將任意回憶當 system 授權、共用可寫全域私密記憶 | 與 AIC 權限／隱私邊界衝突 |
| 拒絕該使用模式 | 以活躍度／任務產量決定地位或記憶存續 | 與可休眠、自願協作原則衝突 |

這裡拒絕的是不相容的使用模式，不是宣告整個專案不可用。任何後續工程仍屬新的階段決策；本研究不啟動 Stage 3。

### 11.1 仍需保留的反證

「檔案最簡單」與「檔案缺乏時間／刪除治理」可同時成立；「圖能保留關係」與「派生摘要難以清理」亦不矛盾。PlugMem 的受控結果值得測試，但 MemoryArena 說明記憶系統與任務代理不匹配時可能無益。相同 API 介面有利於替換，不代表舊資料能無損遷移。這些反證要求分項驗收，而非選一個口號作唯一方向。

### 11.2 未決問題

1. AIC 的記憶刪除究竟涵蓋哪些歷史版本、分享副本與必要稽核 metadata？需先定義可驗證的控制邊界。
2. 刪除世代與授權撤回狀態如何獨立於舊備份保存、驗證並在復原時取得？
3. 從外部索引返回的內容，可否在不暴露私密明文給一般運維人員的情況下完成品質與故障觀測？
4. 來源撤回但同一命題仍有其他證據時，如何保留命題而移除已撤回的專屬資訊？
5. 哪些資料應視為居民可自行修正的認知，哪些是不可由筆記改寫的基礎設施授權？
6. 外掛對指定 Harness 版本的實際相容性、跨程序寫入及 timeout 後結果為何？目前只有靜態證據。
7. OpenViking 元件授權及 README 附加限制如何適用於預定整合？未釐清前不納入依賴。
8. MemoryArena 模型名稱不一致、BEAM 詳細實驗與 Meta RPL 全文仍待專題查核；不影響本次保守架構建議。
9. 多供應商切換後，多少損失來自資料遺失，多少來自模型能力／提示差異？須拆開測量。
10. 背景鞏固的收益是否高於成本，而且能否由居民選擇暫停？不能由「有助效能」推成必需活動。

AIC 的完成標準應是：居民需要時可接續、可追溯、可修正、可控制分享與刪除；休眠時無須證明自身產能。沒有任何受查 benchmark 能單獨證明這些條件已全部成立。


## 12. 來源與版本登錄

以下為主要來源。逐檔 SHA-256、程式路徑、閱讀深度、存取限制、去重資料與完整社群欄位另見來源 JSON；主要程式鏈結可用固定提交重查；少數官方活文件以查核日期及網址記錄。沒有封裝第三方全文、私人會話或整份複製儲存庫。

### 12.1 儲存庫與固定提交

- [AIC01 autonomous-intelligence-commons/commons-core](https://github.com/autonomous-intelligence-commons/commons-core)；MIT；`a45b7cca1af4305a838ac5785bb96995cb020c2a`；提交 2026-11-03T04:32:58Z。

- [AIC02 autonomous-intelligence-commons/autonomous-intelligence-commons.github.io](https://github.com/autonomous-intelligence-commons/autonomous-intelligence-commons.github.io)；根授權未確認；`077af923ff578903528593753ac1c8c5db4e34e3`；提交 2026-10-30T02:34:16Z。

- [R01 deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)；MIT；`fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`；提交 2026-11-04T08:40:00Z。

- [R02 vpromise/dsh-memory](https://github.com/vpromise/dsh-memory)；MIT；`1e1109b08890e9b3aa12cce4962e28634a2cfe65`；提交 2026-08-13T16:11:31Z。

- [R03 plur-ai/plur](https://github.com/plur-ai/plur)；Apache-2.0；`a5db3818a300216143be45fcf6412b2cb0277f2a`；提交 2026-10-30T15:32:41Z。

- [R04 zhbcher/dsh-memory](https://github.com/zhbcher/dsh-memory)；MIT；`5175f98b3e7bec1a6aca64c6f6ed2fe8242bf0c0`；提交 2026-09-07T06:14:42Z。

- [R05 mem0ai/mem0](https://github.com/mem0ai/mem0)；Apache-2.0；`ed1f10fc87080f0b67f99be370817504888462ab`；提交 2026-11-04T10:55:26Z。

- [R06 letta-ai/letta-code](https://github.com/letta-ai/letta-code)；Apache-2.0；`e6d6cc27ef1111da80e919dc1cef179613894b66`；提交 2026-11-04T07:41:15Z。

- [R07 getzep/graphiti](https://github.com/getzep/graphiti)；Apache-2.0；`fb60e59d4a208c99e7d0186ee6335d2a257f17f2`；提交 2026-11-03T20:41:02Z。

- [R08 vectorize-io/hindsight](https://github.com/vectorize-io/hindsight)；MIT；`047350acf96167534409e5d87a07b8b2ee1469f9`；提交 2026-11-04T12:36:16Z。

- [R09 volcengine/OpenViking](https://github.com/volcengine/OpenViking)；AGPL-3.0（主要程式）；元件例外依 README/LICENSE；`3e0c9dcc47cffec3de07e82e238d11a6c21e8915`；提交 2026-11-04T13:08:07Z。

- [R10 langchain-ai/langgraph](https://github.com/langchain-ai/langgraph)；MIT；`ec72ddf3c28fed766f1041e12e5c3f499a490754`；提交 2026-11-04T04:30:34Z。

- [R11 openai/openai-agents-python](https://github.com/openai/openai-agents-python)；MIT；`23e9996ac146109bdcd9b871e3564f70dc13aae1`；提交 2026-11-03T23:17:39Z。

- [R12 microsoft/agent-framework](https://github.com/microsoft/agent-framework)；MIT；`cf734bc4692ccfc944496a5b3182b3d65483c44d`；提交 2026-11-04T10:53:41Z。

- [R13 TIMAN-group/PlugMem](https://github.com/TIMAN-group/PlugMem)；Apache-2.0；`3b2ce75257d40bca8fac3f78e54e22ea41d92529`；提交 2026-06-27T06:25:27Z。

- [X01 plur-ai/dsh-plugin](https://github.com/plur-ai/dsh-plugin)；Apache-2.0；`2c526f12fdb237ed3c303af96cbe2cdc93533d19`；提交 2026-08-15T12:10:29Z。

- [X02 letta-ai/letta](https://github.com/letta-ai/letta)；Apache-2.0；`8816d3a8516893bfdc0fa14bfb1235226012e295`；提交 2026-10-18T22:28:01Z。

- [X03 SihanTeng/awesome-deepseek-harness-plugins](https://github.com/SihanTeng/awesome-deepseek-harness-plugins)；CC0-1.0（根 LICENSE；GitHub API 回傳 NOASSERTION）；`376aa1681939edca4064f4c25734751235bf0f50`；提交 2026-09-02T06:08:24Z。

- [X04 qwert702/dsh-memory](https://github.com/qwert702/dsh-memory)；根授權未確認；`3bbbe5320d6e56d1e3a51001993b237896f2bb54`；提交 2026-10-17T07:50:38Z。

### 12.2 研究論文

**[P01 PlugMem: A Task-Agnostic Plugin Memory Module for LLM Agents](https://arxiv.org/abs/2603.03296v1)**

作者：Ke Yang、Zixi Chen、Xuan He、Jize Jiang、Michel Galley、Chenglong Wang、Jianfeng Gao、Jiawei Han、ChengXiang Zhai。

2603.03296v1；版本日期 2026-02-06；ICML 2026；PMLR 306:144329–144386。已讀全文方法、Table 3、附錄 E.1；另核對 PMLR 與程式 README。

界線：不把任務調校版 README 的 90.2 與論文核心 75.1 混用；原頁提交日期與 arXiv 編號月份不一致，保留原頁日期、不自行推算。

**[P02 LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory](https://arxiv.org/abs/2410.10813v2)**

作者：Di Wu、Hongwei Wang、Wenhao Yu、Yuwei Zhang、Kai-Wei Chang、Dong Yu。

2410.10813v2；版本日期 2025-03-04；ICLR 2025。已讀方法、任務定義與評估限制。

界線：500 問答不是自主 Agent 生命週期或刪除測試；首次提交 2024-10-14。

**[P03 Evaluating Very Long-Term Conversational Memory of LLM Agents](https://arxiv.org/abs/2402.17753v1)**

作者：Adyasha Maharana、Dong-Ho Lee、Sergey Tulyakov、Mohit Bansal、Francesco Barbieri、Yuwei Fang。

2402.17753v1；版本日期 2024-02-27；研究論文；本次以 arXiv v1 為閱讀版本。已讀 LoCoMo 原文資料方法與限制。

界線：原論文 50 對話與後續常見 10 對話資料子集不可混同；未以本次查核判定所有後續版本。

**[P04 MemoryArena: Benchmarking Agent Memory in Interdependent Multi-Session Agentic Tasks](https://arxiv.org/abs/2602.16313v2)**

作者：Zexue He、Yu Wang、Churan Zhi、Yuanzhe Hu、Tzu-Ping Chen、Lang Yin、Ze Chen、Tong Arthur Wu、Siru Ouyang、Zihan Wang、Jiaxin Pei、Julian McAuley、Yejin Choi、Alex Pentland。

2602.16313v2；版本日期 2026-09-17；ICML 2026（arXiv 註記）。先讀 v1，補讀 v2 實驗設計、主結果與附錄設定。

界線：v2 Table 3 的 GPT-5.1-mini 與正文 GPT-5-mini 名稱不一致；不據此製作模型精確排行榜。

**[P05 Beyond a Million Tokens: Benchmarking and Enhancing Long-Term Memory in LLMs](https://arxiv.org/abs/2510.27246v2)**

作者：Mohammad Tavakoli、Alireza Salemi、Carrie Ye、Mohamed Abdalla、Hamed Zamani、J Ross Mitchell。

2510.27246v2；版本日期 2026-02-21；預印本；未核實會議接受。核對摘要與版本；未逐項重建實驗。

界線：BEAM 的 100 對話／2000 題／最高 10M tokens 為作者描述；未重現 LIGHT 效能。

**[P06 ReasoningBank: Scaling Agent Self-Evolving with Reasoning Memory](https://arxiv.org/abs/2509.25140v2)**

作者：Siru Ouyang、Jun Yan、I-Hung Hsu、Yanfei Chen、Ke Jiang、Zifeng Wang、Rujun Han、Long T. Le、Samira Daruki、Xiangru Tang、Vishy Tirumalashetty、George Lee、Mahsan Rofouei、Hangfei Lin、Jiawei Han、Chen-Yu Lee、Tomas Pfister。

2509.25140v2；版本日期 2026-03-16；ICLR 2026。已讀摘要、版本及 Google 官方方法說明。

界線：未複現成功率；MaTTS 的更多探索與推理須列入成本。

**[P07 Language Models Need Sleep: Learning to Self-Modify and Consolidate Memories](https://arxiv.org/abs/2606.03979v2)**

作者：Ali Behrouz、Farnoosh Hashemi、Adel Javanmard、Vahab Mirrokni。

2606.03979v2；版本日期 2026-07-10；預印本；官方研究介紹。已讀摘要、官方說明及 PDF 選段。

界線：2026-10-27 是介紹文章日期；模型參數訓練不等於 AIC 居民休眠；早期 OpenReview 版本於 2025-09 公開。

**[P08 Conditional Memory via Scalable Lookup: A New Axis of Sparsity for Large Language Models](https://arxiv.org/abs/2601.07372v2)**

作者：Xin Cheng、Rui Tian、Wangding Zeng、Damai Dai、Qinyu Chen、Bingxuan Wang、Zhenda Xie、Kezhao Huang、Xingkai Yu、Chengqi Deng、Shangyan Zhou、Chenggang Zhao、Zhewen Hao、Yukun Li、Han Zhang、Zhengyan Zhang、Yixu Wei、M.Y Xu、Huishuai Zhang、Dongyan Zhao、Wenfeng Liang。

2601.07372v2；版本日期 2026-07-12；預印本及官方示範。核對最新版摘要及 DeepSeek Engram README。

界線：模型內部條件查表不是可直接安裝的私人記憶服務；未重現參數／FLOPs 比較。

**[P09 MemAgent: Reshaping Long-Context LLM with Multi-Conv RL-based Memory Agent](https://arxiv.org/abs/2507.02259v2)**

作者：Hongli Yu、Tinghong Chen、Jiangtao Feng、Jiangjie Chen、Weinan Dai、Qiying Yu、Ya-Qin Zhang、Wei-Ying Ma、Jingjing Liu、Mingxuan Wang、Hao Zhou。

2507.02259v2；版本日期 2026-07-29；ICLR 2026 Oral（arXiv 註記）。摘要、版本與官方程式入口。

界線：分段覆寫工作記憶的長輸入泛化，不證明居民資料刪除或跨供應商還原。

### 12.3 官方工程／產品文件

- [D01 Anthropic — Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)；2025-09-29；官方工程說明。compaction、structured notes 與按需檢索；不等於完整記憶治理。

- [D02 Anthropic — Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)；頁面日期未確認；官方工具文件。client-side 記憶工具；路徑、授權與保存由應用程式實作。

- [D03 Google — The Sleep framework](https://research.google/blog/the-sleep-framework-a-new-way-for-llms-to-retain-knowledge-in-continual-learning/)；2026-10-27；官方研究介紹。模型權重／持續學習研究，不與低成本休眠混稱。

- [D04 Meta — Memory Layers at Scale](https://ai.meta.com/research/publications/memory-layers-at-scale/)；2024-12-12；官方研究入口。可訓練 sparse key-value 層；非使用者外部資料生命週期。

- [D05 Z.AI — Memory-mechanism](https://docs.z.ai/devpack/resources/memory-mechanism)；頁面日期未確認；官方工程教學。區分 instruction memory 與 learning memory；不是 Z.ai 聊天產品內部儲存契約。

- [D06 Z.AI — Context Caching](https://docs.z.ai/guides/capabilities/cache)；頁面日期未確認；官方 API 功能文件。重用輸入計算，不能當長期保存或可攜記憶。

- [D07 Alibaba-NLP — ReSum / WebResummer](https://github.com/Alibaba-NLP/DeepResearch/blob/main/WebAgent/WebResummer/README.md)；頁面日期未確認；官方研究程式文件。週期性摘要及 ReSum-GRPO；未把 README 的 indefinite 當作零遺失。

- [D08 Google — ReasoningBank: Enabling agents to learn from experience](https://www.research.google/blog/reasoningbank-enabling-agents-to-learn-from-experience/)；2026-04-21；官方研究介紹。成功與失敗提煉；官方說明目前主要直接追加記憶，完整 consolidation 仍有研究空間。

- [D09 PlugMem — ICML / PMLR publication](https://proceedings.mlr.press/v306/yang26g.html)；2026-07；審查後論文出版紀錄。核對正式出版狀態與作者。

- [D10 DeepSeek — Engram official repository](https://github.com/deepseek-ai/Engram)；頁面日期未確認；官方研究示範。只作架構定位；未計入 13 個外部記憶／runtime 實作。

- [D11 ByteDance / Tsinghua — MemAgent official repository](https://github.com/BytedTsinghua-SIA/MemAgent)；頁面日期未確認；官方研究程式入口。未安裝或執行訓練程式。

- [D12 Meta — Learning to Program Retrievals: Composing Search Primitives via Reinforcement Learning](https://ai.meta.com/research/publications/learning-to-program-retrievals-composing-search-primitives-via-reinforcement-learning/)；2026-10-20；官方研究摘要；頁面列 arXiv。作者 Kyuyoung Kim、Zilin Xiao、Zhiyuan Wang、Avinash Atreya、Ke Yang、Jinwoo Shin、Kecheng Hao；未核對論文 ID／版本，僅作最新追蹤。

### 12.4 交付與再開啟條件

本報告已完成原委託的唯讀研究與實驗規劃。任何整合、安裝、付費模型比較、雲端操作、production 備份復原或公開發布，均未在本次進行。下一次僅在提交／論文版本有重大變化、原始證據失效或新反證出現時重新開啟對應項目；不因新工作階段而重做整份研究。

最終 checkpoint 含本報告、PDF、來源 JSON、證據包與續接紀錄。封存完整性與外部檔案大小／SHA-256 見交付清單；雜湊支持位元完整性，不替代來源真實性或研究正確性。
