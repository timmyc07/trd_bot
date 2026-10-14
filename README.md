# trd_bot

Binance BTC/USDT 公開行情驅動的 **paper-only 做市 bot**。Python 3.12，沒有交易金鑰或私人下單 API。重用 CCXT Pro 處理行情訂閱與 order book 同步，策略採 microprice + inventory skew。

本專案是低延遲交易研究初版，不是真正共置 HFT，也沒有保證獲利。附件是設計參考，裡面的 AI 角色指令與未驗證收益／延遲宣稱未被當成實作事實。

## 先跑起來

在 repo 根目錄執行。離線 demo、replay、測試僅需要 Python 標準函式庫：

```powershell
python -m trd_bot demo --runtime runtime/demo-1 --steps 2000
python -m trd_bot report --runtime runtime/demo-1
python -m trd_bot replay --input runtime/demo-1/events.jsonl --runtime runtime/replay-1
python -m unittest discover -s tests -v
```

每次 demo/replay 使用新的 runtime；再次使用既有目錄會保留停機狀態，不會重播並重複計算資產。Demo 是固定 seed 合成行情，只驗證機械運作，不能證明策略有效。

## 真實行情、本機模擬成交

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-feed.txt
.\.venv\Scripts\python -m trd_bot paper --runtime runtime/paper --seconds 60
.\.venv\Scripts\python -m trd_bot report --runtime runtime/paper
```

`--seconds 0` 持續執行，Ctrl+C 停機。只讀取公開正式市場資料，所有資金／委託／成交都是本機模擬；不是交易所 testnet 成交。Binance 地區限制、DNS、時鐘偏差或網路問題會阻止連線；程式會記錄錯誤並停機，沒有自動切換市場或真實下單。

輸出：`events.jsonl` 可回放行情、`fills.jsonl` 模擬部分成交、`state.json` 權威帳戶狀態、`health.json` 最新報表。日誌輸出至 stderr，Linux 可由 journald 收集。報表包含 equity、總／當日 PnL、費用、目前 drawdown、庫存、停機原因與決策 p50/p99。

## 設定與風控

編輯 `config.json`，價格／數量使用字串 Decimal。預設 10,000 USDT + 0.005 BTC，單筆 0.001 BTC，庫存上限 0.01 BTC、名目曝險 1,500 USDT、每日虧損 50 USDT、drawdown 100 USDT。10 bps 手續費只是明確的研究假設，不代表你的帳戶費率；需依實際 maker fee 調整。

- Spot 預算：不允許負現金或做空；買單含費用預留，賣單不得超過現貨持倉。
- 下單量與未成交數量一併檢查，最多每側一張模擬委託。
- 限制市價計算的曝險、價差異常與行情過期；行情停止時 watchdog 仍運作。
- 每日風控以 UTC 日界、mark-to-market 權益計算，含浮動損益和費用。Drawdown 自啟動以來的權益高點計算。
- Halt 原因和帳戶持久化；所有重啟均需審查，損失基準不會因 resume 被清零。
- 行情跳空仍可能使權益穿越限額；這些是停止條件，不是虧損金額保證。

立即停機（paper runtime 目錄）：

```powershell
New-Item runtime/paper/KILL -ItemType File
```

停機會清除全部模擬委託，持倉繼續保留。復原詳見 [維運文件](docs/OPERATIONS.md)。

## 架構與限制

公開 CCXT book/trade streams → 有界 asyncio queue → 資料驗證 → Decimal 做市策略／風控 → 本機 queue-aware 部分成交 → 原子帳戶儲存／報表。AI 僅適合讀報表、研究及提出變更，不放進成交決策流程。本版沒有 Grok 平台整合、外部通知、其他策略、自動調參或實盤 adapter。

成交需要正確 aggressor side、委託生效後的交易時間、價格達到委託且已觀察成交量耗盡可見排隊量；不單憑價格碰到就填單。模型計入固定進場延遲及費用，但排隊量以報價時可見深度近似，未追蹤隱藏流動性、精確交易所排序、完整 cancel latency、網路回應延遲、市場衝擊；撤單為本機立即生效，不能作為實盤精確成交率證據。沒有額外虛構滑價，成交價固定為模擬限價。

同步磁碟持久化優先確保研究帳戶可恢復，因此本版不宣稱達到附件的微秒效能。決策延遲只量測 process 內驗證／策略／風控與部分成交工作，通常不含最後的 state/health 寫入或網路；成交與 halt 自帶的儲存可能計入。不是 end-to-end latency。

更完整說明：[研究與三重審查](docs/RESEARCH.md)、[維運／部署](docs/OPERATIONS.md)、[驗證紀錄](docs/VALIDATION.md)。
