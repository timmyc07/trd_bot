# 維運與部署

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
