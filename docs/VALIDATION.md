# 驗證紀錄

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
