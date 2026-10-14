# trd_bot：Binance 現貨模擬交易研究 MVP

使用 Binance BTCUSDT 公開行情研究固定規則策略，初始帳戶只有 **虛擬 50 USDT／0 BTC**。所有委託、資產及成交均在本機模擬，不需要 API key，沒有帳戶、實盤或測試網下單功能。不承諾獲利，也不宣稱高頻交易能力。

新版 V2 分離 data、strategy、risk、paper_execution、report，以 SQLite 一致保存帳本與生命週期。原有面板保留為本機操作介面；本次沒有新增完整網站、多 LLM 平台、部署或定期監控。

## 安裝

本專案支援 Python 3.12+，實際驗證使用 Python 3.12。在 repo 根目錄執行，依賴鎖定於 requirements 檔案：公開行情使用 `aiohttp==3.14.5`，離線測試使用 `pytest==9.1.1`，其實測傳遞依賴版本由 `requirements-lock.txt` 約束。離線 demo／replay 引擎本身使用標準函式庫。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m trd_bot demo --runtime runtime/v2-demo --steps 600
.\.venv\Scripts\python -m trd_bot report --runtime runtime/v2-demo
.\.venv\Scripts\python -m pytest -q
```

一般終端機：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m trd_bot demo --runtime runtime/v2-demo --steps 600
.venv/bin/python -m trd_bot report --runtime runtime/v2-demo
.venv/bin/python -m pytest -q
```

demo 採固定 seed 合成事件與版本化規則 fixture，報告標示「合成資料／非歷史績效」。這只驗證機制，不能證明策略有效。demo、replay、compare 每次使用新的 runtime，避免重複計算已處理資料。

## 公開行情、重播與比較

下列範例使用已啟用的 venv；未啟用時請將 `python` 換成上面的 venv Python 路徑。

```powershell
python -m trd_bot paper --runtime runtime/v2-paper --seconds 60
python -m trd_bot report --runtime runtime/v2-paper
python -m trd_bot replay --input runtime/v2-demo/events.jsonl --runtime runtime/v2-replay
python -m trd_bot compare --input runtime/v2-demo/events.jsonl --runtime runtime/v2-compare
```

公開行情預設只執行 60 秒，不接受無限執行。執行期只允許指定 Binance market-data-only HTTP／WebSocket URL：交易規則、深度快照、平均價格及 BTCUSDT 深度／成交訂閱；拒絕其他路徑、交易／帳戶端點與重新導向。公開連線失敗會安全停機，絕不混入合成行情。地區限制、DNS、網路延遲、資料缺漏或規則不支援均可能導致停止或沒有成交。

重播使用錄製資料及相應規則，在虛擬時鐘下依有序事件決策；同一資料、設定與版本產生相同成交及研究報告。`compare` 按接收時間約 70%／30% 切分開發與保留資料，相同接收時間不跨兩區；不在保留集調參。兩區各比較固定策略、不交易、較高手續費與較保守成交情境。沒有足夠不同時間的事件時，應檢查切分筆數，不能將空保留集解讀成驗證。

## 策略、規則與資金

固定非 AI 策略使用最佳買賣量的 imbalance：無持倉且比例至少 0.60 時提出限價買入意圖；有持倉且比例不高於 0.40 時提出限價賣出意圖。策略不能自行核准委託；獨立風控確認規則、餘額、預留款與最壞成交曝險。不交易對照永遠不提出委託。

`config.json` 預設如下，價格、金額及比例以 Decimal 字串設定：

| 設定 | 預設 |
| --- | --- |
| 初始資產 | 50 USDT／0 BTC |
| 單筆買入預算 | 10 USDT，包含費用 |
| 持倉市值上限 | 淨值 40% |
| 回撤停止門檻 | 相對權益峰值 5% |
| 未完成委託／有效期 | 最多 1 筆／30 秒 |
| 行情過期門檻 | 5 秒 |
| 買賣手續費假設 | 各 0.1%，以 USDT 扣除 |
| 模擬延遲／成交量參與率 | 100ms／10% |
| trade-through 門檻 | 1 bps |
| 公開錄製 | 60 秒、單檔 10 MB、保留事件資料 100 MB |

費率來源與設定日期保存在設定及報告中，只是研究假設，不代表任何帳戶實際費率。買單預留價款及費用，賣單預留 BTC；總額、可用額及預留額分開，禁止負餘額、借貸、槓桿與放空。零 BTC 不能先賣出。

公開 `exchangeInfo` 取得 tickSize、stepSize、數量及適用名義金額限制並保存來源與時間；離線使用版本化 fixture。量化後再次檢查規則；規則未知、過期或不支援時拒絕新單，不為湊成交放寬門檻。被動價格變動造成曝險超限時禁止增加曝險並留下事件。

## 成交模型與限制

最佳報價觸及限價不等於成交。模型要求委託生效後的成交證據、相反 aggressor side、足夠的 trade-through、排隊量消耗及成交量參與上限，支援部分成交、取消與到期。`slippage_bps` 是更嚴格的穿價證據門檻，成交仍按原限價，不能突破限價；沒有另外虛構市場衝擊成本。

資料層從有序更新輸出有界 20 檔深度，不能宣稱完整 L2 或真實排隊位置。隱藏流動性、完整撤單延遲、真實撮合排序與市場衝擊未建模，排隊是近似。較保守情境降低參與率、增加排隊量、延遲與穿價門檻；比較結果仍不驗證成交模型準確。

權益採現金加持倉最後有效最佳買價估值，費用各扣一次；資料過期後僅提供歷史估值，`data_fresh=false`，不宣稱目前回撤安全。5% 與 5 秒是風控停止條件，跳空、排程或 I/O 延遲可能穿越門檻，不能保證精確停機時間或最大損失。

## 輸出與本機操作

每個 runtime 保存 SQLite 權威帳本、行情事件、`rules.json` 規則、`health.json` 面板投影及報告。離線示範為 `events.jsonl`；公開行情每次另存 `events-<session>.jsonl` 與同名 `.initial.json` 起始快照，恢復後不追加到舊 session。重播公開錄製時將兩檔放在一起，再以 `--input events-<session>.jsonl` 指定；快照保留資產、峰值、歷史事件 ID 與稽核，不能套用到公開帳戶或覆寫既有帳本。研究報告為 `report.json`、`trades.csv`、`equity.csv`、`rejections.csv`；比較另有 `comparison.json` 與各情境報告。包含來源／合成標示、設定、資料覆蓋與缺口、成交、拒絕原因、費用、淨值／回撤、未平倉部位及模型限制。`fills.jsonl` 是 SQLite 成交紀錄的診斷投影，不能當作獨立帳本。

Windows 可雙擊 `launch-panel.cmd`；也可執行 `python -m trd_bot dashboard --open-browser`。面板只綁定 `127.0.0.1`，公開帳戶使用 `runtime/dashboard/v2-paper`，每次有限執行 60 秒；停止與審查後恢復沿用核心風控。

原版 `runtime/paper`、`runtime/dashboard/paper` 等目錄保留，不自動移轉、不覆寫成 50 USDT。V2 設定或來源與既有帳戶不符時拒絕啟動；設定變更須用新 runtime。SQLite 損壞不得刪檔假裝恢復或重新補資金。停機、人工解鎖、磁碟上限及本機介面生命週期詳見 [維運文件](docs/OPERATIONS.md)。

實際驗證命令、通過／失敗數與未驗證項目記錄於 [驗證紀錄](docs/VALIDATION.md)。測試通過只表示已測工程行為符合斷言，不能當作盈利、實盤安全或長時間運行證明。既有研究背景見 [研究文件](docs/RESEARCH.md)。
