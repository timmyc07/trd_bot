# 維運與部署

## 本機操作面板

`launch-panel.cmd` 或 `python -m trd_bot dashboard --open-browser`。預設 URL `http://127.0.0.1:8765`，若 port 已被其他服務使用，可指定 `--port 8766`。僅綁定 IPv4 loopback，請使用印出的 127.0.0.1 URL，不使用 localhost 別名；請勿將此標準函式庫 server 代理到公網。

面板以同一 Python interpreter、固定參數陣列與 `shell=False` 呼叫原 CLI；公開行情帳戶固定為 `runtime/dashboard/paper`，demo 使用全新目錄。原 `engine.lock` 繼續防止多 writer，面板不會自動移除程序鎖、不編輯交易參數、不重設帳戶、不自動恢復。

「開始公開行情模擬」只可啟動新帳戶。既有帳戶需按「審查後恢復」，確認停機原因、該帳戶日誌與餘額後勾選確認。此操作才會移除 KILL，執行既有 `resume --acknowledge-risk` 並啟動 paper。若仍超過損失／曝險限制，核心仍會再次停機。

「停止」建立目前執行帳戶的 KILL，包含 demo；面板顯示停止中直到子程序退出。若尚在初始 `load_markets()` 階段，最多需等約 30 秒。關閉分頁僅關閉畫面；面板 terminal Ctrl+C 則停止 HTTP server，寫入 KILL 並等子程序清理，40 秒後仍未退出才強制終止並保留狀態／鎖供審查。直接強制關閉 terminal/斷電無法保證 cleanup，需依原復原流程確認程序與鎖。

報表每秒輪詢，程序存活與行情新鮮度分開顯示；保存的舊報表不會冒充運行狀態。圖表只包含本頁開啟期間最多 120 個公開行情樣本，不是完整歷史。合成示範僅在完成時產生 health，執行期間可展開日誌查看進度。`panel.log` 追加程序輸出；畫面最多讀取尾端 32 KiB／40 行，記得定期封存磁碟資料。

所有路由驗證 Host；變更狀態須 JSON POST、同源 Origin 與每次服務啟動隨機 token。靜態路由白名單不公開 repo 原始檔，log/trade 以 textContent 呈現，沒有 CDN 或外部前端依賴。此設計適合可信使用者的本機研究，不提供多使用者登入或遠端託管。

Windows 讀取報表時可能短暫阻擋原子替換。共用 `atomic_json` 僅對 Windows replace 的 PermissionError 做首次嘗試 + 5 次重試，共等待最多 310ms，並記錄 logger；持續拒絕仍拋錯停機、保留舊快照，可能留下 `.tmp` 供調查。沒有刪除目的檔或改成直接覆寫。此同步等待可能增加資料延遲，原本 2 秒行情門檻保持不變；連線延遲超標時面板顯示停機，不能當作程式故障而直接放寬限制。

## 本機停止與復原

1. 查看 `health.json`、stderr/journald 和 `state.json`。`heartbeat_utc` 是最後一次報表時間，不代表行情一定健康；同時檢查 last_book_ms、halt_reason。
2. `KILL` 位於指定 `--runtime` 裡；移除前確認事故原因。halt 清除模擬掛單，不平倉、不重置持倉／權益。
3. `engine.lock` 防止同一 runtime 多 writer。正常退出會移除。強制終止留下鎖時，先確認 PID 和程序確實已停止，才刪除該檔。
4. 公開行情模式復原：

```powershell
python -m trd_bot resume --runtime runtime/paper --acknowledge-risk
python -m trd_bot paper --runtime runtime/paper --seconds 60
```

resume 寫入一次性 approval，保留現金、持倉、日損基準、高水位。若損失或曝險仍超標，下次立即停機。設定變更須開新 runtime，不能暗中更改原帳戶風控。demo/replay 不支援游標恢復，每次用新目錄。

狀態缺欄位、JSON 破損或 schema/config 不符合時拒絕啟動；不要手動刪 state 假裝恢復。保留備份供調查；若開新研究實驗，另指定 runtime 並承認它是新帳戶。

## Linux systemd（範本，未執行遠端部署）

先建立專用 trdbot 使用者與 `/srv/trd_bot` checkout，確認 repo 與 venv 可讀，只有 runtime 可由服務帳戶寫入。

```bash
cd /srv/trd_bot
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-feed.txt
sudo install -o root -g root -m 644 deploy/trd-bot.service /etc/systemd/system/trd-bot.service
sudo systemctl daemon-reload
sudo systemctl start trd-bot
journalctl -u trd-bot -f
```

`Restart=no` 刻意不自動重啟；崩潰或風控都需人審查。首次啟動前校準 NTP、確認 Binance 服務可及、配置磁碟容量監控與日誌保存。`events.jsonl` 持續增長，需操作人員停止服務後封存到獨立儲存，不能讓磁碟無限制增長。Linux 範本不代表已完成雲端／長時間驗證。

## Docker（範本）

```bash
docker build -t trd-bot .
docker volume create trd-bot-runtime
docker run --rm --name trd-bot -v trd-bot-runtime:/app/runtime trd-bot
```

以非 root 執行；既有 volume 或 bind mount 須讓容器 bot 使用者有寫入權限。維護時先停容器再對資料 volume 做 resume／狀態檢查。不得設定會繞過人工審查的容器重啟流程。

## 只讀操作層

AI/人員可讀 health/report 並提出參數變更；本版未建立外部訊息或排程。建議監看 heartbeat 過期、halt、庫存達 80%、daily PnL 接近限額、錯誤增加與磁碟容量。不自動 resume、不直接改設定。

`state.json` 是權威帳戶快照。`fills.jsonl` 為診斷紀錄，與快照不是跨檔案交易式寫入，強制斷電可能導致最後一筆 journal 與 snapshot 不一致。決策 latency samples 僅保留最近 10,000 筆，完整錄製存於事件檔。更高吞吐與正式帳本需要後續獨立工程／審查。
