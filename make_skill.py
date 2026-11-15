#!/usr/bin/env python3
"""把 bot.py 轉成可分享的 skill 版本(skill/okx-pullback-bot/scripts/bot.py)。改完 bot.py 後重跑:python3 make_skill.py"""
import shutil
s = open("bot.py", encoding="utf-8").read()
R = [
 ("狀態檔 state.json(與本檔同資料夾)", "狀態與設定放在 ./trading-bot-state/(可用環境變數 BOT_STATE_DIR 改位置)"),
 ('''ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "out")
STATE = os.path.join(ROOT, "state.json")
CAND = os.path.join(ROOT, "candidate.json")
PROJ = "claude/trading-bot/"
''', '''ROOT = os.environ.get("BOT_STATE_DIR") or os.path.join(os.getcwd(), "trading-bot-state")
os.makedirs(ROOT, exist_ok=True)
OUT = os.path.join(ROOT, "out")
STATE = os.path.join(ROOT, "state.json")
CAND = os.path.join(ROOT, "candidate.json")
CONF = os.path.join(ROOT, "config.json")
try:
    CFG.update({k: v for k, v in json.load(open(CONF, encoding="utf-8")).items() if k in CFG})
except Exception:  # noqa: BLE001
    pass
'''),
 ('print("UPLOAD %s -> %s%s" % (src, PROJ, dst))', 'print("SAVED %s" % src)'),
 ('SEND = "NEXT=SEND:把 ===REPORT=== 與 ===END=== 之間的文字原封不動當作最終回覆;有 UPLOAD 行就逐一 project_write(先 Read 該檔再用 local_path);其他事都不要做。"',
  'SEND = "NEXT=SEND:把 ===REPORT=== 與 ===END=== 之間的文字原封不動回覆給使用者;SAVED 行是已存在本機的檔案,不用另外處理;其他事都不要做。"'),
 ('print("NEXT=REVIEW:用 Read 讀本資料夾的 agents-v3.md(沒有才 project_read `%sagents-v3.md`)並照做(四位分身審核後執行 --confirm 或 --reject)。" % PROJ)',
  'print("NEXT=REVIEW:照 SKILL.md「有候選方案時的審核」一節做,然後執行 --confirm 或 --reject。")'),
 ('nxt = SEND + " 另外用 PushNotification 推播「📌 一句話結論」那一行。"', 'nxt = SEND + " 若有通知工具,可另外推播「📌 一句話結論」那一行。"'),
 ('''    ap.add_argument("--capital", type=float)
    A = ap.parse_args()
    if A.capital:
        CFG["capital_usd"] = A.capital
''', '''    ap.add_argument("--capital", type=float, help="本金 USD(會存進 config.json)")
    ap.add_argument("--risk-pct", type=float, help="單筆風險百分比(會存進 config.json)")
    ap.add_argument("--max-lev", type=float, help="槓桿上限(會存進 config.json)")
    A = ap.parse_args()
    over = {k: v for k, v in (("capital_usd", A.capital), ("risk_pct", A.risk_pct), ("max_lev", A.max_lev)) if v}
    CFG.update(over)
    if CFG["capital_usd"] <= 0 or not 0 < CFG["risk_pct"] <= 5 or not 1 <= CFG["max_lev"] <= 10:
        sys.exit("設定超出安全範圍:本金需大於 0、單筆風險需在 0 到 5 之間(單位為百分比)、槓桿上限需在 1 到 10 倍。請重下參數或修正 " + CONF)
    if over:
        try:
            saved = json.load(open(CONF, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            saved = {}
        saved.update(over)
        json.dump(saved, open(CONF, "w", encoding="utf-8"), indent=1)
'''),
]
for a, b in R:
    assert s.count(a) == 1, a[:40]
    s = s.replace(a, b)
assert "PROJ" not in s
open("skill/okx-pullback-bot/scripts/bot.py", "w", encoding="utf-8").write(s)
shutil.copy("review.py", "skill/okx-pullback-bot/scripts/review.py")
print("ok")
