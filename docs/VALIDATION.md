# 驗證紀錄

## 操作面板驗證（2026-10-14）

- 完整 unittest：29 項通過（原16項 + 面板10項 + 儲存3項）。面板測試包含並行啟動僅產生一個子程序、停止正確 runtime、明確 resume、舊報表非運行狀態、失敗退出、程序鎖、關閉清理、bounded log、HTTP Host/Origin/token/body限制、停止狀態優先與 demo/paper 審查資料分離。
- Windows 儲存測試：暫時拒絕兩次後成功發布完整 JSON；持續拒絕6次後拋錯、舊快照不變；非Windows錯誤立即拋出。直接覆寫與目的檔刪除均未引入。
- wheel 建置成功，檢查 archive 確認包含 index.html、app.js、style.css 三個面板資產；`git diff --check` 通過。
- 瀏覽器實際操作：離線示範完成399次模擬成交；再次示範後點停止，立即清除模擬掛單並顯示停止。公開行情啟動／恢復確認視窗和勾選流程可用，資料更新／日誌／成交表正常，前端沒有 console error。
- 響應式驗證：375px viewport override；桌面 app 縮放下 CSS viewport 約312px，頁面寬度300px，沒有整頁橫向溢出；表格可在自己容器內水平捲動。已重設 viewport。
- 真實公開行情＋每秒面板 polling 同時執行：修正前可重現 Windows reader 阻擋 replace；修正後 logger 可見重試成功，連續處理620筆事件。之後一筆來源延遲2250ms的 trade 超過原2000ms門檻，依既有風控停機；另一次連線停流觸發 stale_market_data。未放寬風控、未宣稱長期穩定或收益有效性，未啟用真實下單。

此面板是本機工具；尚未驗證多使用者／遠端部署或長時間 soak。關閉瀏覽器分頁不等於停止服務，詳見 OPERATIONS。

日期：2026-10-14，Windows，Python 3.12.10，CCXT 4.5.87。

## 已執行

- `python -m unittest discover -s tests -v`：16 項通過。涵蓋 Decimal tick 取整、post-only 價格、部分成交／排隊／費用／去重、spot 預算、projected exposure、延遲送達、kill、資料過期、daily loss、drawdown、restart、無效資料、狀態損壞、配置、單 writer、rate limit、無私人 API，以及離線凍結串流與 close cleanup。
- `python -m trd_bot demo --runtime runtime/verified-demo --steps 2000`：4,000 筆合成 book/trade events，399 次模擬部分／完整成交事件；費用 24.00507387 USDT、PnL -12.80369387 USDT，結束無掛單。
- `python -m trd_bot replay --input runtime/verified-demo/events.jsonl --runtime runtime/verified-replay`：cash/base/fills/fees/equity/PnL 與 demo 一致；時間量測因硬體排程不同不應要求一致。
- 公開行情 smoke：`.venv/Scripts/python -m trd_bot paper --runtime runtime/binance-verified --seconds 15`，325 筆真實公開事件，errors=0，halt=null，fills=0，退出後掛單為零。初始 CCXT REST 快照無 timestamp，被略過；之後有效 WebSocket book 正常啟動。未使用金鑰。
- `.venv/Scripts/python -m pip check`：沒有依賴衝突。
- `git diff --check`：通過。

Windows sandbox 內的 asyncio 測試曾因事件迴圈底層限制停滯；已清理該測試程序，完整測試在允許事件迴圈的環境通過。無背景行情程序持續執行。

## 尚未驗證／不可推論

沒有 Docker runtime，因此未建置容器；systemd 範本未在 Linux／VPS 上啟動。沒有長時間 soak、故障注入到真實交易所、真實下單／testnet fills、收益有效性或正式 L2/L3 queue 校準。15 秒 smoke 的零成交既不是成功策略證據，也不是錯誤；短時段與可見排隊模型通常不足以成交。

所有記錄／帳戶數據在 gitignored runtime，避免把大量行情或執行狀態當成來源碼提交。預設初始 BTC 亦是模擬資產；公開 smoke 的 PnL 含其市價變動，不能誤認為策略成交收益。
