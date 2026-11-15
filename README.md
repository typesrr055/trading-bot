# trading-bot

OKX 永續合約決策單機器人(只產生決策單,不下單)。

- `bot.py`:單一程式,抓行情、把關、追蹤上一張方案、印出報告。排程每場 `git clone` 後執行 `python3 bot.py`。
- `agents-v3.md`:有候選方案時四位分身的審核說明。
- `review.py`:實單復盤分析。

狀態檔、決策單與假想單結果不在這個 repo。本金用 `--capital` 傳入,其餘設定在 `bot.py` 開頭的 `CFG`。
`skill/okx-pullback-bot/` 是可分享的 skill 版本,由 `make_skill.py` 從 `bot.py` 產生。

這不是投資建議;這套規則在歷史回測中的優勢很薄,沒有被證明能穩定獲利。
