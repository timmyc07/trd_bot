# 前置研究與三重審查

## 本機面板補充（2026-10-14）

依 Ponytail full 選擇最小完整方案：重用原 CLI 與 Python stdlib，僅增加 loopback server 與靜態響應式介面。先讀 core/cli、既有測試、README/OPERATIONS、pyproject、Docker、systemd 與 CI，再以三個 agent 完成相容性、風險與維運審查。

GitHub 深層搜尋比較 [Hummingbot Dashboard](https://github.com/hummingbot/dashboard)、其 [部署依賴](https://github.com/hummingbot/dashboard/blob/main/INSTALLATION.md) 和 Streamlit 交易面板生態。Hummingbot Dashboard 需要獨立 backend/broker/Docker，不符合此小型本機 bot 的最小範圍，未複製其源碼。直接重用 [CPython HTTP server](https://github.com/python/cpython/blob/3.12/Lib/http/server.py) 與既有交易 CLI，避免重寫交易／復原邏輯。

官方查核：[Python HTTP server](https://docs.python.org/3.12/library/http.server.html)、[subprocess](https://docs.python.org/3.12/library/subprocess.html)。HTTP server 非 production server，因此只綁定127.0.0.1、Host/Origin/token 驗證、不允許任意路徑／指令、不配置 CORS；子程序不用 shell，輸出導向檔案並限量讀尾端。

三重審查確認採用信心 >90%；後置審查修正停止中狀態被新鮮 health 覆蓋、demo 後恢復確認引用錯誤帳戶、連線恢復後舊警告未清除。保留原金額計算、logger、風控與鎖；不新增真實下單、帳戶重設、遠端部署或 AI 自動調參。

實測發現 Windows 面板 reader 與 state/health replace 的短暫競爭，另行搜尋 [GitHub 同類問題](https://github.com/jamesdchen/hpc-agent/issues/153)、[atomicwrites 上游說明](https://github.com/untitaker/python-atomicwrites)，並查核 [Microsoft CreateFile share mode](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)、[Python os.replace](https://docs.python.org/3.12/library/os.html#os.replace)。經三個 agent 再審查後，在原 helper 添加 Windows 限次 PermissionError 重試；不引入已停止維護的 atomicwrites 套件，不取消原子替換或錯誤保護。

日期：2026-10-14。原工作目錄為空，沒有既有程式、文件與部署設定。開工前依使用者要求，以三個 agent 分別完成官方相容性、程式／風險、維運／慣例審查；後續再次審查核心與 CLI。

## GitHub 深層搜尋與重用決策

1. [CCXT](https://github.com/ccxt/ccxt)、[公開串流範例](https://github.com/ccxt/ccxt/blob/master/examples/py/one-exchange-many-streams.py)：MIT，直接依賴 `ccxt.pro`，沿用公開 watch 方法、rate limiter、內建 order book 重建和 asyncio 資源關閉。避免重新實作交易所增量快照與 WebSocket 協定。
2. [Hummingbot simple PMM](https://github.com/hummingbot/hummingbot/blob/master/scripts/simple_pmm.py)、[做市策略文件](https://github.com/hummingbot/hummingbot-site/blob/main/docs/strategies/v1-strategies/pure-market-making.md)：研究 Decimal spread、定期刷新與預算檢查模式。未複製源碼或引入完整 Hummingbot runtime；本版的 microprice/skew 來自使用者提供的策略參考，搭配已知做市公式。
3. [hftbacktest](https://github.com/nkaz001/hftbacktest)、[latency models](https://github.com/nkaz001/hftbacktest/blob/master/docs/latency_models.rst)：已有成熟回測技術。初版提供小型 JSONL 研究回放；若需要正式 L2/L3 queue 模型、策略比較與校準，應接入此工具，不宣稱自行達到同等精度。

## 審查 1：官方相容性

- [CCXT Pro 官方文件](https://docs.ccxt.com/docs/pro-manual)：Pro 隨 ccxt 提供，支援 asyncio watch_order_book/watch_trades；使用 newUpdates=True 並另做 ID 去重。
- [CCXT Python 版本設定](https://github.com/ccxt/ccxt/blob/master/pyproject.toml)：確認 Python 3.12 相容。直接 feed 依賴固定 4.5.87，transitive 由 pip resolver 安裝；尚非全平台 hash lock。
- [Binance 公開串流文件](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams)：行情與私人下單分離，CCXT 負責訂閱與重建；連線失敗會停止 paper engine。
- [Python Decimal](https://docs.python.org/3.12/library/decimal.html)：價量以字串轉換，買價向下／賣價向上依 tick 倍數取整。
- [hftbacktest 成交限制](https://hftbacktest.readthedocs.io/en/latest/order_fill.html)：歷史回放無法改變市場，排隊與市場衝擊需另外校準。

## 審查 2：程式與錯誤

參考文有 init 拼法、縮排、ask rounding、live PnL 固定為零、缺時間戳視為新鮮、未預留 pending orders、重啟丟失 halt 等問題，沒有原樣使用。新核心加入參數驗證、有限數值、正價量、未交叉訂單簿、時間戳／事件順序、交易去重、Decimal 預算及 projected exposure、watchdog、持久化停機。

後置審查修正：KILL 在事件處理前檢查；成交來源時間需晚於實際報價生效；未成交買單隨 mark 更新重新檢查曝險；禁止 demo/replay 恢復而重播已處理事件；已停機 demo 不覆寫錄製資料。

公開 smoke 發現 CCXT 初始 REST snapshot 可沒有 timestamp。再次經三個 agent 審查，adapter 只略過首次帶 timestamp 行情前的 None 快照、警告一次，等待後續 WebSocket depthUpdate；30 秒 startup watchdog 仍有效。已觀察帶 timestamp 的 book 後，再出現 None 仍送入核心停機。未偽造來源時間。

## 審查 3：慣例與維運

stdlib 核心／unittest 離線驗證，中央 logger 記錄成交與停機；單 writer 鎖、原子 JSON + fsync、非 root 部署範本、禁止未審查重啟，文件同步說明資料來源及復原邊界。

本版技術可行性與 paper-only 操作經相容性文件、可重現測試與程式審查達到要求的 >90% 採用門檻。實際獲利、真實排隊精度、微秒 HFT、24/7 實盤部署均未達門檻，未落地。未使用／驗證附件中的 Grok Bot 產品與收益宣稱。
