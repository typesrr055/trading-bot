#!/usr/bin/env python3
# BOT_VERSION=3.0
"""交易機器人 v3(單一檔案,純標準函式庫,不呼叫 AI)。
抓 OKX 行情 -> 指標/支撐壓力 -> 只產生「順勢回檔限價單」-> 程式把關 -> 追蹤上一張方案 -> 印出可直接送出的報告。
  python3 bot.py --capital 本金USD     例行執行(不帶 --capital 時用 100)
  python3 bot.py --confirm --why "..." [--diss "..."] [--fund "..."]   分身通過後登記方案
  python3 bot.py --reject  --why "..." [--fund "..."]                  分身否決(12 小時內不再提同一方案)
狀態與設定放在 ./trading-bot-state/(可用環境變數 BOT_STATE_DIR 改位置):目前方案、否決冷卻、基本面摘要。所有數字與時間都由本程式產生。
"""
import argparse, json, math, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

CFG = dict(
    capital_usd=100.0, risk_pct=2.0, max_lev=3.0, taker_fee_pct=0.05, mmr_pct=0.4,
    min_rr=2.0, min_score=50, max_age_min=30, valid_h=6, max_hold_h=24, veto_h=12, fund_h=12,
    watch=["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"], auto_top_n=5, screen_pick=8,
    screen_min_usd=1e6, gate_min_usd=1e7, exclude=[])
ROOT = os.environ.get("BOT_STATE_DIR") or os.path.join(os.getcwd(), "trading-bot-state")
os.makedirs(ROOT, exist_ok=True)
OUT = os.path.join(ROOT, "out")
STATE = os.path.join(ROOT, "state.json")
CAND = os.path.join(ROOT, "candidate.json")
CONF = os.path.join(ROOT, "config.json")
try:
    CFG.update({k: v for k, v in json.load(open(CONF, encoding="utf-8")).items() if k in CFG})
except Exception:  # noqa: BLE001
    pass
API = "https://www.okx.com/api/v5/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
TPE = timezone(timedelta(hours=8))
WD = "一二三四五六日"
TF_W = {"1D": 3.0, "4H": 2.0, "1H": 1.5, "15m": 0.5}
BARS = ["15m", "1H", "4H", "1D"]
ERR = []


def tpe(ts, fmt="%m-%d %H:%M"):
    d = datetime.fromtimestamp(ts, TPE)
    return d.strftime(fmt) + "(週%s)" % WD[d.weekday()]


def g(x):
    return "%.6g" % x


def short(sym):
    return sym.replace("-USDT-SWAP", "")


# ------------------------------------------------------------------ 行情
def get(path, retries=2):
    last = ""
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(API + path, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                j = json.loads(r.read().decode("utf-8"))
            if str(j.get("code", "0")) != "0":
                raise RuntimeError("code %s %s" % (j.get("code"), j.get("msg")))
            return j.get("data") or []
        except Exception as e:  # noqa: BLE001
            last = "%s :: %s" % (path, e)
            time.sleep(1.2 * (i + 1))
    ERR.append(last)
    return None


def candles(inst, bar, limit=200):
    rows = get("market/candles?instId=%s&bar=%s&limit=%d" % (inst, bar, limit))
    out = []
    for r in reversed(rows or []):  # OKX 回傳最新在前
        try:
            out.append({"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                        "c": float(r[4]), "v": float(r[5])})
        except (ValueError, IndexError):
            continue
    return out


def screen_metrics(r):
    """日線注意力分數(只決定哪些標的進入深入分析)。"""
    if len(r) < 55:
        return None
    c = [x["c"] for x in r]
    trs = [max(r[i]["h"] - r[i]["l"], abs(r[i]["h"] - r[i - 1]["c"]), abs(r[i]["l"] - r[i - 1]["c"])) for i in range(1, len(r))]
    atr = sum(trs[-14:]) / 14
    if atr <= 0 or c[-1] <= 0:
        return None
    s50 = sum(c[-50:]) / 50
    tscore = max(-5.0, min(5.0, (c[-1] - s50) / atr))
    vols = [x["v"] for x in r]
    base = sum(vols[-22:-2]) / 20 if sum(vols[-22:-2]) > 0 else 0
    vr = (max(vols[-2], vols[-1]) / base) if base else 0
    brk = c[-1] > max(x["h"] for x in r[-21:-1]) or c[-1] < min(x["l"] for x in r[-21:-1])
    atr_pct = atr / c[-1] * 100
    z = min(abs(c[-1] / c[-2] - 1) * 100 / atr_pct, 2.0) if atr_pct else 0
    return min(abs(tscore), 4) / 4 * 40 + min(vr, 3) / 3 * 25 + (20 if brk else 0) + z / 2 * 15


def collect(extra_syms):
    tk = get("market/tickers?instType=SWAP")
    ins = get("public/instruments?instType=SWAP")
    if tk is None or ins is None:
        return None
    stock = {x["instId"] for x in ins if x.get("instCategory") == "3"}  # 股票永續不做
    rows = {}
    for x in tk:
        i = x.get("instId", "")
        if i.endswith("-USDT-SWAP") and i not in stock and i not in CFG["exclude"]:
            try:
                rows[i] = (float(x["last"]), float(x["volCcy24h"]) * float(x["last"]))
            except (KeyError, ValueError):
                pass
    byvol = sorted(rows, key=lambda i: -rows[i][1])
    syms = [s for s in CFG["watch"] if s in rows]
    syms += [s for s in byvol[:CFG["auto_top_n"]] if s not in syms]
    elig = [i for i in byvol if rows[i][1] >= CFG["screen_min_usd"]]

    def scr(i):
        time.sleep(0.12)
        try:
            return i, screen_metrics(candles(i, "1D", 100))
        except Exception:  # noqa: BLE001
            return i, None
    with ThreadPoolExecutor(max_workers=6) as ex:
        sc = sorted([(m, i) for i, m in ex.map(scr, elig) if m], reverse=True)
    syms += [i for _, i in sc if i not in syms][:CFG["screen_pick"]]
    syms += [s for s in extra_syms if s in rows and s not in syms]
    jobs = [(s, b) for s in syms for b in BARS] + [(s, "funding") for s in syms]

    def run(j):
        s, b = j
        if b == "funding":
            d = get("public/funding-rate?instId=%s" % s, retries=1)
            try:
                return j, float(d[0]["fundingRate"]) * 100
            except Exception:  # noqa: BLE001
                return j, None
        return j, candles(s, b)
    data = {s: {"tf": {}, "funding": None, "last": rows[s][0], "vol": rows[s][1]} for s in syms}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for (s, b), v in ex.map(run, jobs):
            if b == "funding":
                data[s]["funding"] = v
            else:
                data[s]["tf"][b] = v
    return data, len(elig)


# ------------------------------------------------------------------ 指標(與回測用的 analyze.py 相同算法)
def sma_at(vals, n, idx=None):
    idx = len(vals) - 1 if idx is None else idx
    return None if idx + 1 < n else sum(vals[idx - n + 1: idx + 1]) / n


def rsi_wilder(cl, n):
    if len(cl) < n + 1:
        return None
    gn = ls = 0.0
    for i in range(1, n + 1):
        d = cl[i] - cl[i - 1]
        gn += max(d, 0)
        ls += max(-d, 0)
    ag, al = gn / n, ls / n
    for i in range(n + 1, len(cl)):
        d = cl[i] - cl[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)


def atr_wilder(cs, n=14):
    if len(cs) < n + 1:
        return None
    trs = [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]), abs(cs[i]["l"] - cs[i - 1]["c"])) for i in range(1, len(cs))]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def pivots(cs, k=3):
    hi, lo = [], []
    for i in range(k, len(cs) - k):
        h, l = cs[i]["h"], cs[i]["l"]
        if all(h >= cs[j]["h"] for j in range(i - k, i + k + 1)):
            hi.append((i, h))
        if all(l <= cs[j]["l"] for j in range(i - k, i + k + 1)):
            lo.append((i, l))
    return hi, lo


def snapshot_tf(cs):
    if len(cs) < 30:
        return None
    cl = [c["c"] for c in cs]
    s20, s50, s20p = sma_at(cl, 20), sma_at(cl, 50), sma_at(cl, 20, len(cl) - 6)
    hi, lo = pivots(cs[-150:])
    sc, p = 0, cl[-1]
    if s20:
        sc += 1 if p > s20 else -1
    if s50:
        sc += 1 if p > s50 else -1
    if s20 and s20p:
        sc += 1 if s20 > s20p else -1
    if len(hi) >= 2 and len(lo) >= 2:
        up = hi[-1][1] > hi[-2][1] and lo[-1][1] > lo[-2][1]
        dn = hi[-1][1] < hi[-2][1] and lo[-1][1] < lo[-2][1]
        sc += 2 if up else (-2 if dn else 0)
    bb = None
    if len(cl) >= 20:
        w = cl[-20:]
        mid = sum(w) / 20
        sd = math.sqrt(sum((x - mid) ** 2 for x in w) / 20)
        bb = (mid + 2 * sd, mid - 2 * sd)
    return {"trend": "up" if sc >= 3 else ("down" if sc <= -3 else "range"), "ma50": s50, "boll": bb,
            "rsi12": rsi_wilder(cl, 12), "atr": atr_wilder(cs), "hi": hi, "lo": lo, "cs": cs}


def cluster(items, tol):
    out = []
    for p, w, s in sorted(items):
        if out and abs(p - out[-1][0]) <= tol:
            q, qw, qs = out[-1]
            out[-1] = ((q * qw + p * w) / (qw + w), qw + w, qs + [s])
        else:
            out.append((p, w, [s]))
    return out


def collect_levels(tfs, price, atr1h):
    items = []
    for tf, s in tfs.items():
        w = TF_W.get(tf, 1.0)
        items += [(p, w, "%s前高" % tf) for _, p in s["hi"][-6:]]
        items += [(p, w, "%s前低" % tf) for _, p in s["lo"][-6:]]
        if tf in ("4H", "1D") and s["ma50"]:
            items.append((s["ma50"], w * 0.7, "%sMA50" % tf))
        if tf in ("1H", "4H") and s["boll"]:
            items.append((s["boll"][0], 0.6, "%sBOLL上軌" % tf))
            items.append((s["boll"][1], 0.6, "%sBOLL下軌" % tf))
    c = tfs["1H"]["cs"][-24:]
    items.append((max(x["h"] for x in c), 1.5, "24H高"))
    items.append((min(x["l"] for x in c), 1.5, "24H低"))
    cs = tfs["4H"]["cs"][-100:]
    hi_i = max(range(len(cs)), key=lambda i: cs[i]["h"])
    lo_i = min(range(len(cs)), key=lambda i: cs[i]["l"])
    hi_p, lo_p = cs[hi_i]["h"], cs[lo_i]["l"]
    if hi_p > lo_p:
        for r in (0.382, 0.5, 0.618):
            items.append((hi_p - (hi_p - lo_p) * r if hi_i > lo_i else lo_p + (hi_p - lo_p) * r, 1.0, "Fib%.3g" % r))
    lv = cluster(items, max(atr1h * 0.3, price * 0.0008))
    sup = sorted([l for l in lv if l[0] < price - atr1h * 0.15], key=lambda l: -l[0])
    res = sorted([l for l in lv if l[0] > price + atr1h * 0.15], key=lambda l: l[0])
    return sup, res


def pick_targets(sgn, entry, risk, levels):
    cands = [l for l in levels if sgn * (l - entry) > 0]
    tp1, real1 = next((l for l in cands if sgn * (l - entry) >= 1.0 * risk), None), True
    if tp1 is None:
        tp1, real1 = entry + sgn * 2.0 * risk, False
    tp2, real2 = next((l for l in cands if sgn * (l - tp1) > 0.3 * risk and sgn * (l - entry) >= 2.0 * risk), None), True
    if tp2 is None:
        tp2, real2 = entry + sgn * max(3.0 * risk, sgn * (tp1 - entry) * 1.5), False
    return tp1, real1, tp2, real2


def size_position(entry, sl, sgn):
    """停損距離決定槓桿:名目 = 願意虧的金額 / (停損距離% + 來回手續費%)。"""
    cap = CFG["capital_usd"]
    stop = abs(entry - sl) / entry
    fee = 2.0 * CFG["taker_fee_pct"] / 100.0
    notional = cap * CFG["risk_pct"] / 100.0 / (stop + fee)
    lev = notional / cap
    if lev > CFG["max_lev"]:
        lev = CFG["max_lev"]
        notional = lev * cap
    lev = max(lev, 1.0)
    liq = entry * (1 - sgn / lev + sgn * CFG["mmr_pct"] / 100.0)
    return {"stop_pct": stop * 100, "notional": notional, "lev": lev, "margin": notional / lev,
            "loss": notional * (stop + fee), "liq": liq}


def analyze(sym, d, now):
    """回傳該標的的回檔限價單候選(最多:逢壓力空、逢支撐多各一)。"""
    tfs = {}
    for b in BARS:
        s = snapshot_tf(d["tf"].get(b) or [])
        if s:
            tfs[b] = s
    if not all(b in tfs for b in ("1H", "4H", "1D")) or not tfs["1H"]["atr"]:
        return []
    small = tfs.get("15m") or tfs["1H"]
    if (now - small["cs"][-1]["t"]) / 60 > CFG["max_age_min"]:
        return []
    price, atr = d["last"], tfs["1H"]["atr"]
    sup, res = collect_levels(tfs, price, atr)
    sup_p, res_p = [l[0] for l in sup], [l[0] for l in res]
    bias = sum(TF_W[t] * {"up": 1, "down": -1, "range": 0}[s["trend"]] for t, s in tfs.items()) / sum(TF_W[t] for t in tfs)
    rsi1 = tfs["1H"]["rsi12"] or 50
    dcl = [c["c"] for c in tfs["1D"]["cs"]]
    mom = (dcl[-1] / dcl[-31] - 1) * 100 if len(dcl) >= 31 else None
    buf = max(atr, 0.6 * (tfs["4H"]["atr"] or 0), price * 0.003)  # 停損緩衝:至少 1 個 1H ATR
    out = []

    def add(sgn, entry, sl, src, levels):
        risk = abs(entry - sl)
        if risk <= 0 or risk / entry < 0.0015:
            return
        tp1, real1, tp2, real2 = pick_targets(sgn, entry, risk, levels)
        rr1, rr2 = abs(tp1 - entry) / risk, abs(tp2 - entry) / risk
        score = 50.0 + 20 * sgn * bias
        if (sgn < 0 and rsi1 >= 55) or (sgn > 0 and rsi1 <= 45):
            score += 5
        if (sgn < 0 and rsi1 < 30) or (sgn > 0 and rsi1 > 70):
            score -= 10
        dist = abs(entry - price) / atr
        score += 10 if dist <= 1 else 5 if dist <= 2 else -15 if dist > 3 else 0
        score += 10 if rr2 >= 3 else 5 if rr2 >= CFG["min_rr"] else -15
        if rr1 < 1.0:
            score -= 10
        f = d["funding"]
        if f is not None and abs(f) > 0.03:
            score += 5 if (f > 0) == (sgn < 0) else -5
        c = {"sym": sym, "side": "short" if sgn < 0 else "long", "sgn": sgn, "price": price, "entry": entry, "sl": sl,
             "tp1": tp1, "tp2": tp2, "rr1": rr1, "rr2": rr2, "real2": real2, "score": score, "src": "/".join(src[:3]),
             "atr_mult": risk / atr, "dist_atr": dist, "t1d": tfs["1D"]["trend"], "t4h": tfs["4H"]["trend"],
             "t1h": tfs["1H"]["trend"], "rsi4h": tfs["4H"]["rsi12"] or 50, "rsi1d": tfs["1D"]["rsi12"] or 50,
             "mom30": mom, "vol": d["vol"], "funding": f}
        c.update(size_position(entry, sl, sgn))
        out.append(c)

    if res:
        r1 = res[0]
        sl = r1[0] + buf
        if len(res) > 1 and res[1][0] - r1[0] <= 1.5 * atr:
            sl = max(sl, res[1][0] + 0.1 * atr)
        add(-1, r1[0], sl, r1[2], sup_p)
    if sup:
        s1 = sup[0]
        sl = s1[0] - buf
        if len(sup) > 1 and s1[0] - sup[1][0] <= 1.5 * atr:
            sl = min(sl, sup[1][0] - 0.1 * atr)
        add(1, s1[0], sl, s1[2], res_p)
    return out


# ------------------------------------------------------------------ 把關
def reasons(c, veto, now):
    want, r = ("up" if c["sgn"] > 0 else "down"), []
    if c["t1d"] != want or c["t4h"] != want:
        r.append("日線 %s、4H %s 沒有同向" % (c["t1d"], c["t4h"]))
    if c["mom30"] is None or c["mom30"] * c["sgn"] <= 0:
        r.append("30 日動能 %s 與方向相反" % ("不明" if c["mom30"] is None else "%+.1f%%" % c["mom30"]))
    if c["score"] < CFG["min_score"]:
        r.append("分數 %.0f < %d" % (c["score"], CFG["min_score"]))
    if c["rr2"] < CFG["min_rr"]:
        r.append("賺賠比(TP2) %.2f < %g" % (c["rr2"], CFG["min_rr"]))
    elif not c["real2"]:
        r.append("目標價沒有實際支撐/壓力位")
    if c["rr1"] < 1.0:
        r.append("TP1 賺賠比 %.2f < 1" % c["rr1"])
    if c["sgn"] < 0 and min(c["rsi4h"], c["rsi1d"]) < 30:
        r.append("RSI12 超賣(4H %.0f、日線 %.0f)" % (c["rsi4h"], c["rsi1d"]))
    if c["sgn"] > 0 and max(c["rsi4h"], c["rsi1d"]) > 70:
        r.append("RSI12 過熱(4H %.0f、日線 %.0f)" % (c["rsi4h"], c["rsi1d"]))
    if c["vol"] < CFG["gate_min_usd"]:
        r.append("24 小時成交額 %.0f 百萬 USD 太小" % (c["vol"] / 1e6))
    if c["atr_mult"] < 0.999:
        r.append("停損不到 1 個 1H ATR")
    if veto.get("%s|%s" % (c["sym"], c["side"]), 0) > now:
        r.append("分身已否決,冷卻中")
    return r


GATE_TXT = "只做順勢回檔限價單;日線與 4H 同向;30 日動能同向;分數≥%d;賺賠比(TP2)≥%g 且目標是實際支撐/壓力;TP1 賺賠比≥1;4H/日線 RSI12 未超賣或過熱;24 小時成交額≥%.0f 百萬 USD;停損≥1 個 1H ATR" % (
    CFG["min_score"], CFG["min_rr"], CFG["gate_min_usd"] / 1e6)


# ------------------------------------------------------------------ 追蹤上一張方案
def net_r(gross_r, stop_pct):
    fee = 2 * CFG["taker_fee_pct"]
    return (gross_r * stop_pct - fee) / (stop_pct + fee)


def track(p, c15, now):
    """用 15 分 K 判定:成交/到期/停損/TP。同一根同時碰停損與停盈算停損;成交那根不算停盈。回傳事件文字或 None。"""
    sgn, e, sl, risk = p["sgn"], p["entry"], p["sl"], abs(p["entry"] - p["sl"])
    hit = lambda b, px, favor: (b["h"] >= px) if (sgn > 0) == favor else (b["l"] <= px)  # noqa: E731

    def close(result, gross):
        p.update(status="closed", result=result, r=round(net_r(gross, p["stop_pct"]), 2), closed_t=now)
        return "%s,結果 %+.2fR(約 %+.2f USD,已扣手續費)" % (result, p["r"], p["r"] * p["loss"])
    for b in c15:
        if b["t"] < p["t0"] + 600 or (p["status"] == "open" and b["t"] < p["fill_t"]):
            continue  # 每次都從頭重算(結果相同),狀態檔只在狀態改變時才需要上傳
        if p["status"] == "open" and b["t"] == p["fill_t"]:  # 成交那根只看停損
            if hit(b, sl, False):
                return close("成交後同一根 K 棒停損", -1.0)
            continue
        if p["status"] == "pending":
            if b["t"] >= p["valid_until"]:
                p.update(status="closed", result="到期未成交", r=0.0, closed_t=now)
                return "掛單到期未成交(已作廢,請撤單)"
            if hit(b, e, False):
                p.update(status="open", fill_t=b["t"], tp1_done=False)
                if hit(b, sl, False):
                    return close("成交後同一根 K 棒停損", -1.0)
            continue
        if b["t"] >= p["fill_t"] + CFG["max_hold_h"] * 3600:
            mv = sgn * (b["o"] - e) / risk
            return close("持倉滿 %d 小時,時間出場" % CFG["max_hold_h"], 0.5 * p["rr1"] + 0.5 * mv if p["tp1_done"] else mv)
        if hit(b, sl, False):
            return close("TP1 後剩餘倉位停損" if p["tp1_done"] else "停損", 0.5 * p["rr1"] - 0.5 if p["tp1_done"] else -1.0)
        if not p["tp1_done"] and hit(b, p["tp1"], True):
            p["tp1_done"] = True
        if p["tp1_done"] and hit(b, p["tp2"], True):
            return close("TP1、TP2 都到", 0.5 * p["rr1"] + 0.5 * p["rr2"])
    return None


def plan_line(p, last):
    side = "做空" if p["sgn"] < 0 else "做多"
    if p["status"] == "pending":
        return "%s %s 限價 %s 尚未成交;掛單有效到 %s,過了就撤單" % (short(p["sym"]), side, g(p["entry"]), tpe(p["valid_until"]))
    fl = net_r(p["sgn"] * (last - p["entry"]) / abs(p["entry"] - p["sl"]), p["stop_pct"])
    return "%s %s 已在 %s 成交(%s);%s;停損 %s、TP2 %s;最晚 %s 全部出場;現價 %s,浮動約 %+.2fR" % (
        short(p["sym"]), side, g(p["entry"]), tpe(p["fill_t"]), "TP1 已到(應已平一半)" if p["tp1_done"] else "TP1 %s 未到" % g(p["tp1"]),
        g(p["sl"]), g(p["tp2"]), tpe(p["fill_t"] + CFG["max_hold_h"] * 3600), g(last), fl)


# ------------------------------------------------------------------ 報告
DISCLAIMER = "這不是投資建議,最終決定由你負責。"


def sheet(c, why, diss, prev, now):
    sell = c["sgn"] < 0
    act = "價格反彈到 %s 時賣出做空" % g(c["entry"]) if sell else "價格回落到 %s 時買進做多" % g(c["entry"])
    L = ["【白話摘要】",
         "📌 一句話結論:%s %s(掛限價單等它來,不追價)。" % (act, short(c["sym"])),
         "🧭 現在你要做什麼:掛限價%s %s;同時設停損 %s、停盈 TP1 %s(到了先平一半)、TP2 %s。" % (
             "賣出" if sell else "買進", g(c["entry"]), g(c["sl"]), g(c["tp1"]), g(c["tp2"])),
         "💰 金額:名目 %.0f USD、槓桿 %.1fx(保證金 %.2f USD);停損打到約虧 %.2f USD。" % (c["notional"], c["lev"], c["margin"], c["loss"]),
         "⏰ 什麼時候取消:%s 前沒成交就撤單;成交後最晚 %d 小時內全部出場。" % (tpe(c["valid_until"]), CFG["max_hold_h"]),
         "❓ 為什麼:%s" % why,
         "📍 上次方案:%s" % prev, DISCLAIMER, "",
         "【決策單】%s(台北)|現價 %s" % (tpe(now, "%Y-%m-%d %H:%M"), g(c["price"])),
         "結論:%s —— %s —— 限價單(順勢回檔)" % ("做空" if sell else "做多", c["sym"]),
         "進場:%s(%s)  停損:%s(距離 %.2f%%,%.2f 個 1H ATR)  TP1:%s  TP2:%s(賺賠比 %.2f / %.2f)" % (
             g(c["entry"]), c["src"], g(c["sl"]), c["stop_pct"], c["atr_mult"], g(c["tp1"]), g(c["tp2"]), c["rr1"], c["rr2"]),
         "倉位:名目 %.0f USD|槓桿 %.1fx|保證金 %.2f USD|停損虧 %.2f USD|估計強平 %s" % (c["notional"], c["lev"], c["margin"], c["loss"], g(c["liq"])),
         "掛單有效至:%s;成交後最晚出場:%d 小時" % (tpe(c["valid_until"]), CFG["max_hold_h"]),
         "依據:日線 %s、4H %s、1H %s|30 日動能 %+.1f%%|RSI12 4H %.0f、日線 %.0f|24 小時成交額 %.0f 百萬 USD|資金費率 %s|分數 %.0f" % (
             c["t1d"], c["t4h"], c["t1h"], c["mom30"], c["rsi4h"], c["rsi1d"], c["vol"] / 1e6,
             "-" if c["funding"] is None else "%.4f%%" % c["funding"], c["score"]),
         "理由:%s" % why, "分身意見分歧:%s" % (diss or "無"),
         "風險提醒:同時最多一張單;停損單要跟進場單一起掛;TP1 先平一半;重大事件前先撤單。"]
    return "\n".join(L)


def wait_report(head, todo, why, prev, now, tail):
    L = ["【白話摘要】", "📌 一句話結論:%s" % head, "🧭 現在你要做什麼:%s" % todo, "❓ 為什麼:%s" % why,
         "📍 上次方案:%s" % prev, DISCLAIMER, "", "【決策單】%s(台北)" % tpe(now, "%Y-%m-%d %H:%M")] + tail
    return "\n".join(L)


def emit(report, uploads, nxt):
    print("===REPORT===")
    print(report)
    print("===END===")
    for src, dst in uploads:
        print("SAVED %s" % src)
    print(nxt)


SEND = "NEXT=SEND:把 ===REPORT=== 與 ===END=== 之間的文字原封不動回覆給使用者;SAVED 行是已存在本機的檔案,不用另外處理;其他事都不要做。"


def load_state():
    try:
        s = json.load(open(STATE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        s = {}
    s.setdefault("plan", None)
    s.setdefault("veto", {})
    s.setdefault("fund", None)
    return s


def save_state(s):
    json.dump(s, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ 主流程
def main_run():
    now = int(time.time())
    st = load_state()
    before = json.dumps(st, sort_keys=True, ensure_ascii=False)
    st["veto"] = {k: v for k, v in st["veto"].items() if v > now}
    plan = st["plan"]
    got = collect([plan["sym"]] if plan else [])
    if got is None or len(ERR) > 20:
        tail = ["結論:觀望 —— 行情抓取失敗,本次不提供任何價位", "實際錯誤:"] + ["  " + e for e in ERR[:5]]
        prev = "無法更新(行情失敗)。" + (("原方案:" + plan_line(plan, plan["entry"])) if plan and plan["status"] == "pending" and plan["valid_until"] > now else "若有未成交掛單且已過有效時間,請撤單。")
        emit(wait_report("行情抓不到,這次不給方案,觀望。", "不用下新單。", "資料來源連線失敗(錯誤訊息見下方)。", prev, now, tail), [], SEND)
        return
    data, n_elig = got
    uploads, prev = [], "無"
    if plan:
        ev = track(plan, data.get(plan["sym"], {}).get("tf", {}).get("15m") or [], now)
        if plan["status"] == "closed":
            os.makedirs(OUT, exist_ok=True)
            fn = "plan_%s.json" % plan["id"]
            json.dump(plan, open(os.path.join(OUT, fn), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            uploads.append((os.path.join(OUT, fn), "plans/" + fn))
            prev = "%s %s:%s" % (short(plan["sym"]), "做空" if plan["sgn"] < 0 else "做多", ev)
            st["plan"] = plan = None
        else:
            prev = plan_line(plan, data[plan["sym"]]["last"]) if plan["sym"] in data else "方案標的本次抓不到行情,請以上一份決策單為準"
    cands = [c for s, d in data.items() for c in analyze(s, d, now)]
    for c in cands:
        c["why_not"] = reasons(c, st["veto"], now)
    good = sorted([c for c in cands if not c["why_not"]], key=lambda c: -c["score"])
    near = sorted([c for c in cands if c["why_not"]], key=lambda c: (len(c["why_not"]), -c["score"]))[:3]
    near_txt = ["  %s %s 分數 %.0f:%s" % (short(c["sym"]), "做空" if c["sgn"] < 0 else "做多", c["score"], "、".join(c["why_not"])) for c in near]
    stat = "掃描 %d 檔(流動性達標 %d 檔),候選 %d 個,通過把關 %d 個;抓取錯誤 %d" % (len(data), n_elig, len(cands), len(good), len(ERR))
    tail = ["把關條件:" + GATE_TXT, stat]

    def finish(report, nxt):
        if json.dumps(st, sort_keys=True, ensure_ascii=False) != before:
            save_state(st)
            uploads.insert(0, (STATE, "state.json"))
        emit(report, uploads, nxt)

    if plan:  # 已有方案:同時最多一張單,不找新方案、不啟動分身
        todo = "保持掛單,到期就撤。" if plan["status"] == "pending" else "照計畫持倉:停損與停盈單保持掛著,TP1 到先平一半。"
        finish(wait_report("沿用上一張方案,不開新單。", todo, "同時最多一張單;上一張方案還沒結束。", prev, now,
                           ["結論:沿用既有方案 —— " + plan["sym"]] + tail), SEND)
    elif good:
        c = good[0]
        c["valid_until"] = now + CFG["valid_h"] * 3600
        c["t0"], c["id"] = now, datetime.fromtimestamp(now, TPE).strftime("%Y%m%d_%H%M")
        c["prev"] = prev
        json.dump(c, open(CAND, "w", encoding="utf-8"), ensure_ascii=False)
        fund = st["fund"]
        ftxt = "無快取,基本面分身需上網查(最多 3 次搜尋)"
        if fund and now - fund["t"] < CFG["fund_h"] * 3600:
            ftxt = "快取 %.1f 小時前:%s(基本面分身不要再上網查)" % ((now - fund["t"]) / 3600, fund["text"])
        print("===CANDIDATE===")
        print(sheet(c, "(待分身審核)", "", prev, now))
        if len(good) > 1:
            b = good[1]
            print("備案:%s %s 限價 %s 停損 %s TP1 %s TP2 %s(賺賠比 %.2f/%.2f)分數 %.0f" % (
                short(b["sym"]), "做空" if b["sgn"] < 0 else "做多", g(b["entry"]), g(b["sl"]), g(b["tp1"]), g(b["tp2"]), b["rr1"], b["rr2"], b["score"]))
        print("基本面:" + ftxt)
        print(stat)
        print("===END===")
        finish_state = json.dumps(st, sort_keys=True, ensure_ascii=False) != before
        if finish_state:
            save_state(st)
        print("NEXT=REVIEW:照 SKILL.md「有候選方案時的審核」一節做,然後執行 --confirm 或 --reject。")
    else:
        why = "沒有任何方案通過程式把關。" + (" 最接近的是 %s(%s)。" % (short(near[0]["sym"]) + (" 做空" if near[0]["sgn"] < 0 else " 做多"), "、".join(near[0]["why_not"])) if near else "")
        finish(wait_report("現在沒有符合條件的機會,觀望,不用下單。", "不用做事。", why, prev, now,
                           ["結論:觀望 —— 程式把關無方案通過", "最接近的 3 檔:"] + near_txt + tail), SEND)


def main_decide(a):
    now = int(time.time())
    st = load_state()
    c = json.load(open(CAND, encoding="utf-8"))
    if a.fund:
        st["fund"] = {"t": now, "text": a.fund[:300]}
    uploads = [(STATE, "state.json")]
    if a.confirm:
        keep = ("id t0 sym side sgn entry sl tp1 tp2 rr1 rr2 stop_pct notional lev margin loss liq valid_until score").split()
        st["plan"] = dict({k: c[k] for k in keep}, status="pending", why=a.why[:300])
        report = sheet(c, a.why, a.diss, c.get("prev", "無"), now)
        os.makedirs(OUT, exist_ok=True)
        fn = "decision_%s.md" % c["id"]
        open(os.path.join(OUT, fn), "w", encoding="utf-8").write(report + "\n")
        uploads.append((os.path.join(OUT, fn), "reports/" + fn))
        nxt = SEND + " 若有通知工具,可另外推播「📌 一句話結論」那一行。"
    else:
        st["veto"]["%s|%s" % (c["sym"], c["side"])] = now + CFG["veto_h"] * 3600
        report = wait_report("有一個候選方案,但審核沒過,觀望,不用下單。", "不用做事。", "%s %s 被否決:%s" % (
            short(c["sym"]), "做空" if c["sgn"] < 0 else "做多", a.why), c.get("prev", "無"), now,
            ["結論:觀望 —— 分身否決(%d 小時內不再提同一方案)" % CFG["veto_h"], "分身意見分歧:%s" % (a.diss or "無")])
        nxt = SEND
    save_state(st)
    emit(report, uploads, nxt)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--reject", action="store_true")
    ap.add_argument("--why", default="")
    ap.add_argument("--diss", default="")
    ap.add_argument("--fund", default="")
    ap.add_argument("--capital", type=float, help="本金 USD(會存進 config.json)")
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
    if A.confirm or A.reject:
        main_decide(A)
    else:
        main_run()
