# V2 本機維運

本版是有限時間的公開行情模擬研究，使用 Python 3.12+，不需要 Docker、systemd、雲端或全天候服務。先依 [README](../README.md) 建立 venv 並安裝鎖定依賴。此文件不代表已完成公開連線或遠端部署驗證；實際結果見 [VALIDATION](VALIDATION.md)。

## 帳戶與資料隔離

V2 使用 `config.json` 的虛擬 50 USDT／0 BTC 預設值，CLI 範例分別保存到 `runtime/v2-demo`、`runtime/v2-paper`、`runtime/v2-replay`、`runtime/v2-compare`。面板公開帳戶固定為 `runtime/dashboard/v2-paper`，demo 每次建立新目錄。

舊版 10,000 USDT 帳戶及 `runtime/dashboard/paper` 等資料原樣保留；沒有隱式移轉、補資金或清除歷史。不要將舊 JSON 狀態複製到 V2 假裝恢復。設定、資料來源或 schema 不符時拒絕開啟；新研究設定使用新的 runtime，並視為獨立實驗。

`ledger.sqlite3` 是權威來源，交易一致更新現金、BTC、預留款、模擬訂單、已處理事件 ID、淨值峰值、停機與稽核紀錄。初始化標記 `.ledger-initialized` 也須保留，缺少 DB 或標記會拒絕開啟。`health.json`、`fills.jsonl`、JSON／CSV 報告只是投影；禁止編輯這些檔案解除風控。正常重啟也會對帳、取消遺留未成交部分並要求人工審查，不會靠重啟重置資產或風控。

## 停止與人工解鎖

正常有限執行結束或任何風控／資料錯誤均停止新意圖、取消本機未成交部分並釋放剩餘預留款，保留持倉與原因。不自動清倉，也不送出任何外部下單或取消指令。

公開帳戶執行時可按 Ctrl+C，或在其 runtime 建立 `KILL`：

```powershell
New-Item runtime/v2-paper/KILL -ItemType File
```

一般終端機：

```bash
touch runtime/v2-paper/KILL
```

復原前先確認原程序已停止，查看 `report.json`、`health.json`、程序日誌及 SQLite 稽核紀錄。`heartbeat_utc` 僅表示報表發布時間，須一起看 `data_fresh`、`last_book_ms`、`halt_reason`；舊估值不代表當下行情或回撤安全。風控因價格跳動或費用造成的實際損失不會因取消委託消失。

確認原因後執行明確的本機人工 reset，再另行開始有限公開行情：

```powershell
python -m trd_bot reset --runtime runtime/v2-paper --acknowledge-risk
python -m trd_bot paper --runtime runtime/v2-paper --seconds 60
```

`resume` 是 `reset` 的相容別名。reset 仍需公開行情連線，檢查最新資料、規則與帳本後才寫入一次性核准；過程不交易。reset 保留現金、持倉、處理過的事件 ID、權益峰值及稽核，不放寬規則。檢查失敗就維持停止；仍達回撤限制時不能藉此清零。恢復網路連線不會自動核准。

demo／replay 不提供中途游標恢復；重跑請用新 runtime。`engine.lock` 防止同一目錄多 writer。正常結束移除；強制關閉程序或斷電可能留鎖，確認 PID 與程序確實停止後才可移除。SQLite 損壞、結構缺欄或對帳失敗時保留現場，不刪 DB 重開帳戶掩蓋故障。備份時先停止 writer，再一致備份整個 runtime，包括可能存在的 SQLite journal／WAL 檔案。

## 錄製、容量與報告

公開行情預設 60 秒，允許指定有限時長，不支援 `--seconds 0`。預設 `recording_max_bytes=10000000`、`retention_max_bytes=100000000`；錄製達上限停止，不悄悄丟事件或刪歷史騰空間。保留計算會納入 runtime 父目錄下的事件檔案，請將不同研究分組放在清楚的父目錄。

容量不足時先停止程序，封存不再使用的完整 runtime 到另一儲存位置，再開始獨立錄製。事件檔案限制不是整個磁碟限額；SQLite、權益序列、CSV、比較情境與 `panel.log` 也占用空間，仍需檢查磁碟容量。不要在執行中截斷事件檔或搬移 DB。

資料缺漏、亂序、無時間戳、深度序號缺口、連線失敗或有界佇列滿載都會停機並記錄原因。獨立 asyncio watchdog 使用 monotonic clock，在沒有下一筆行情時也會要求停機；帳本工作與有界資料佇列不能阻止它設置停機訊號，但不是精確時間保證。

離線 `events.jsonl`、公開每次獨立 `events-<session>.jsonl` 保存時間、來源、接收與連線狀態；`rules.json` 保存 exchangeInfo 及取得時間。公開流只對外輸出有界 20 檔深度，不代表完整 L2。錄製中斷與最後一筆停止事件未能寫入等情況，需一起檢查報告的資料覆蓋／缺口與 SQLite 稽核。

`report.json` 與 `trades.csv`、`equity.csv`、`rejections.csv` 不加入當下產生時間，供相同資料／版本／設定的確定性比較；`health.json` 則包含實際 heartbeat。報告各檔不是跨檔案單一交易；輸出 I/O 失敗時以 SQLite 為準，不把部分 CSV 當完整交付。

`compare` 固定切分開發／保留時段，再比較不交易、基準、較高費用與較保守成交模型，不進行參數搜尋。重播合成資料仍標示非歷史績效。穿價情境是成交證據門檻，不是額外從限價扣一次滑價費用；估值中的手續費也不可再次重複扣除。

## 本機面板

執行 `launch-panel.cmd`，或：

```powershell
python -m trd_bot dashboard --open-browser
```

預設 URL 為 `http://127.0.0.1:8765`；port 衝突可用 `--port 8766`。使用印出的 IPv4 loopback URL，不使用 localhost 別名。不要將本機標準函式庫 server 代理到公網；沒有多使用者登入或遠端託管功能。

面板用同一 Python interpreter、固定參數陣列及 `shell=False` 呼叫 V2 CLI。公開行情每次執行 60 秒；既有帳戶使用「審查後恢復」，需確認該公開帳戶的停機原因與日誌，勾選確認後才執行 reset 並啟動新的有限公開錄製。面板不自動刪鎖、重設資產或恢復。

「停止」建立目前程序的 KILL；畫面保持停止中直到子程序退出。關閉瀏覽器分頁不會結束 bot；在面板 terminal 按 Ctrl+C 會停止 HTTP server、寫入 KILL 並等子程序清理，40 秒仍未結束才強制終止。直接強制關閉 terminal 或斷電可能留鎖／未完成委託，必須按前述對帳流程恢復。

報表每秒輪詢，程序存活與行情新鮮度分別顯示。圖表只保存本頁期間最多 120 個行情觀測，並非完整歷史。畫面僅讀取尾端日誌；完整程序輸出追加至 `panel.log`。需自行封存日誌，未設背景定期監控。

所有路由驗證 Host；修改狀態須 JSON POST、同源 Origin 與每次服務啟動隨機 token。靜態路由允許清單不公開 repo 檔案；成交與日誌用 textContent 顯示，無外部前端 CDN。面板 JSON 投影使用原子替換；Windows 短暫分享鎖最多重試等待 310ms，永久 I/O 拒絕仍失敗，不改成直接覆寫。

## 舊部署資料

repo 既有 Dockerfile 與 systemd 範本屬初版部署資料，不是 V2 本機 MVP 的必要安裝方式，也沒有因此執行部署。舊版無限執行配方不適用 V2 的有限錄製要求；本次不提供新的部署步驟、定期任務或自動恢復設定。

## 分次錄製與重播

公開帳戶恢復後建立新的 session 事件檔，並保存同名 `.initial.json` 一致起始快照；禁止將多次錄製串接後假裝單次回測。快照包含當次開始前的資產、峰值、去重 ID 與稽核，只能初始化全新的 replay 帳本，不能用來重設公開帳戶。重播時連同快照保留在同目錄，CLI 會自動讀取；公開規則優先使用事件檔首筆記錄，不從後續 cache 引入未來資訊。報告的 session 覆蓋區間與累計事件數分開。

`compare` 每個固定情境以新的50USDT帳戶比較，不使用先前 session 資產快照，不在保留集調參。錄製事件及起始快照均納入保留容量檢查；達上限會停機，不自動刪除資料。
