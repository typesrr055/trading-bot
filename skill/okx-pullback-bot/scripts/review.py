#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""review.py -- 實單復盤分析模組(純標準函式庫,不呼叫 AI、不連網)。

讀 actual_trades.csv(與 config.json),輸出一份 Markdown 復盤報告:
  1. 總體績效:勝率、期望值(R)、獲利因子、賺賠比、最大回撤、最長連虧
  2. 樣本是否足夠:平均 R 的 bootstrap 90% 區間與「平均 R > 0」的機率
  3. 分組:是否照決策單、標的、方向、槓桿、計畫賺賠比、持倉時間
  4. 紀律檢查:逐筆比對 config 規則(槓桿上限、賺賠比門檻、停損過近、沒照決策單)
  5. 守規則 vs 違規 的績效對照、獲利單的「拿到幾成計畫目標」
  6. (選用)journal.csv:機器人給方案 vs 觀望的次數

用法:
  python3 review.py actual_trades.csv [--config config.json] [--journal journal.csv] [--out review.md]
"""
import argparse
import csv
import json
import random
import statistics as st
from datetime import datetime

TIGHT_STOP_PCT = 0.5      # 停損距離小於此值視為「過近」(1 個 1H ATR 的粗略代理)
DISTORT_STOP_PCT = 0.3    # 小於此值時 R 會失真,不納入 R 統計
BOOT_N = 20000


def fnum(x):
    try:
        return float(str(x).split('/')[0])
    except (TypeError, ValueError):
        return None


def load_trades(path):
    rows = []
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            entry, stop = fnum(r.get('entry')), fnum(r.get('stop_price'))
            notional, pnl = fnum(r.get('notional_usd')), fnum(r.get('pnl_usdt'))
            if entry is None or pnl is None:
                continue
            t = dict(sym=(r.get('symbol') or '').replace('-USDT-SWAP', ''), side=r.get('side'),
                     lev=fnum(r.get('leverage')), entry=entry, stop=stop, notional=notional, pnl=pnl,
                     followed=(r.get('followed_report') or '').strip() or '?',
                     planned_rr=fnum(r.get('planned_rr')), open=r.get('open_time'), close=r.get('close_time'))
            t['stop_pct'] = abs(entry - stop) / entry * 100 if stop else None
            t['risk_usd'] = notional * t['stop_pct'] / 100 if (notional and t['stop_pct']) else None
            t['distorted'] = t['stop_pct'] is not None and t['stop_pct'] < DISTORT_STOP_PCT
            t['r'] = pnl / t['risk_usd'] if t['risk_usd'] else None
            try:
                fmt = '%Y-%m-%d %H:%M'
                t['hold_h'] = (datetime.strptime(t['close'], fmt) - datetime.strptime(t['open'], fmt)).total_seconds() / 3600
            except (TypeError, ValueError):
                t['hold_h'] = None
            rows.append(t)
    rows.sort(key=lambda t: t['close'] or '')
    return rows


def boot_ci(rs, seed=7):
    """平均 R 的 bootstrap 90% 區間,以及平均 R > 0 的比例。"""
    if len(rs) < 3:
        return None
    rnd = random.Random(seed)
    means = sorted(sum(rnd.choice(rs) for _ in rs) / len(rs) for _ in range(BOOT_N))
    return means[int(BOOT_N * 0.05)], means[int(BOOT_N * 0.95)], sum(m > 0 for m in means) / BOOT_N


def stats(ts):
    n = len(ts)
    if not n:
        return None
    pnl = [t['pnl'] for t in ts]
    wins, losses = [p for p in pnl if p > 0], [p for p in pnl if p <= 0]
    rs = [t['r'] for t in ts if t['r'] is not None and not t['distorted']]
    eq = peak = dd = 0.0
    streak = max_streak = 0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if p <= 0 else 0
        max_streak = max(max_streak, streak)
    rw, rl = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    return dict(n=n, win=len(wins), pnl=sum(pnl), avg=sum(pnl) / n,
                pf=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
                n_r=len(rs), avg_r=st.mean(rs) if rs else None, med_r=st.median(rs) if rs else None,
                avg_win_r=st.mean(rw) if rw else None, avg_loss_r=st.mean(rl) if rl else None,
                dd=dd, max_streak=max_streak, rs=rs)


def f(x, d=2, plus=False):
    if x is None:
        return '-'
    return ('%+.*f' if plus else '%.*f') % (d, x)


def bucket(t, key):
    if key == 'followed':
        return {'yes': '照決策單', 'partial': '部分照', 'no': '沒照決策單', 'pre-bot': '上線前'}.get(t['followed'], t['followed'])
    if key == 'sym':
        return t['sym']
    if key == 'side':
        return '做多' if t['side'] == 'long' else '做空'
    if key == 'lev':
        return '-' if t['lev'] is None else ('槓桿 ≤3x' if t['lev'] <= 3 else '槓桿 3~5x' if t['lev'] <= 5 else '槓桿 >5x')
    if key == 'rr':
        rr = t['planned_rr']
        if t['distorted']:
            return '停損過近(賺賠比失真)'
        return '-' if rr is None else ('計畫賺賠比 <1.5' if rr < 1.5 else '計畫賺賠比 1.5~2' if rr < 2 else '計畫賺賠比 ≥2')
    if key == 'hold':
        h = t['hold_h']
        return '-' if h is None else ('持倉 <1 小時' if h < 1 else '持倉 1~8 小時' if h < 8 else '持倉 ≥8 小時')


def violations(t, cfg):
    v = []
    if t['lev'] is not None and t['lev'] > cfg['soft_max_leverage']:
        v.append('槓桿 %.1fx > %gx' % (t['lev'], cfg['soft_max_leverage']))
    if t['planned_rr'] is not None and not t['distorted'] and t['planned_rr'] < cfg['min_rr']:
        v.append('計畫賺賠比 %.2f < %g' % (t['planned_rr'], cfg['min_rr']))
    if t['stop_pct'] is not None and t['stop_pct'] < TIGHT_STOP_PCT:
        v.append('停損只離 %.2f%%' % t['stop_pct'])
    if t['followed'] == 'no':
        v.append('沒照決策單')
    return v


def journal_counts(path):
    plan = wait = other = 0
    with open(path, encoding='utf-8-sig') as fh:
        next(fh, None)
        for ln in fh:
            parts = ln.rstrip('\n').split(',', 10)
            if len(parts) < 11 or parts[3] == 'actual':
                continue
            verdict = parts[10].strip('\\" ')
            if verdict.startswith('觀望'):
                wait += 1
            elif verdict.startswith('條件式') or verdict.startswith('做多') or verdict.startswith('做空'):
                plan += 1
            else:
                other += 1
    return plan, wait, other


def build(trades, cfg, journal=None):
    L = ['# 實單復盤報告', '']
    s = stats(trades)
    L += ['## 1. 總體績效', '| 項目 | 數字 |', '|---|---|',
          '| 筆數 / 勝率 | %d 筆,贏 %d 筆(%.0f%%) |' % (s['n'], s['win'], s['win'] / s['n'] * 100),
          '| 已實現損益 | %s USDT(平均每筆 %s) |' % (f(s['pnl'], 2, True), f(s['avg'], 2, True)),
          '| 獲利因子(總賺 ÷ 總賠) | %s |' % f(s['pf']),
          '| 平均 R / 中位 R | %s / %s(%d 筆可算 R) |' % (f(s['avg_r'], 2, True), f(s['med_r'], 2, True), s['n_r']),
          '| 賺的單平均 / 虧的單平均 | %s R / %s R |' % (f(s['avg_win_r'], 2, True), f(s['avg_loss_r'], 2, True)),
          '| 最大回撤 / 最長連虧 | %s USDT / %d 筆 |' % (f(s['dd']), s['max_streak']), '']
    dist = [t for t in trades if t['distorted']]
    if dist:
        L.append('註:%s 停損距離不到 %.1f%%,R 會失真,已排除在 R 統計外(損益仍計入)。' % (
            '、'.join('%s %s' % (t['open'][5:10], t['sym']) for t in dist), DISTORT_STOP_PCT))
        L.append('')

    L.append('## 2. 樣本夠不夠下結論')
    ci = boot_ci(s['rs'])
    if ci:
        lo, hi, p = ci
        L.append('- 平均 R 的 90%% 區間:%s R ~ %s R;重抽樣中平均 R 大於 0 的比例 %.0f%%。' % (f(lo, 2, True), f(hi, 2, True), p * 100))
        L.append('- 判讀:區間下緣 %s 0 → %s' % ('>' if lo > 0 else '≤', '目前的正期望值有初步支持,但仍需更多筆數。' if lo > 0
                                           else '還不能排除「其實沒有優勢、只是運氣」。'))
    risk_usd = cfg['capital_usd'] * cfg['risk_pct_per_trade'] / 100
    if s['avg_r'] is not None:
        L.append('- 以現在設定(本金 %.2f USD、單筆風險 %g%% = %.2f USD),每筆期望值約 %s USD。' % (
            cfg['capital_usd'], cfg['risk_pct_per_trade'], risk_usd, f(s['avg_r'] * risk_usd, 2, True)))
    L.append('- 經驗值:至少 30 筆才開始有參考性,100 筆才比較可信;目前 %d 筆。' % s['n'])
    L.append('')

    L.append('## 3. 分組比較(筆數少,只看方向不看絕對值)')
    L += ['| 分組 | 筆數 | 勝率 | 損益 USDT | 平均 R |', '|---|---|---|---|---|']
    for key in ('followed', 'side', 'sym', 'lev', 'rr', 'hold'):
        groups = {}
        for t in trades:
            groups.setdefault(bucket(t, key), []).append(t)
        for name in sorted(groups):
            g = stats(groups[name])
            L.append('| %s | %d | %.0f%% | %s | %s |' % (name, g['n'], g['win'] / g['n'] * 100, f(g['pnl'], 2, True), f(g['avg_r'], 2, True)))
    L.append('')

    L.append('## 4. 紀律檢查(逐筆比對 config 規則)')
    L += ['| 日期 | 標的 | 損益 | R | 違反的規則 |', '|---|---|---|---|---|']
    clean, dirty = [], []
    for t in trades:
        v = violations(t, cfg)
        (dirty if v else clean).append(t)
        L.append('| %s | %s %s | %s | %s | %s |' % (t['open'][5:10], t['sym'], '多' if t['side'] == 'long' else '空',
                                                  f(t['pnl'], 2, True), '失真' if t['distorted'] else f(t['r'], 2, True),
                                                  ';'.join(v) or '無'))
    L.append('')
    for name, g in (('完全守規則', clean), ('至少違反一條', dirty)):
        gs = stats(g)
        if gs:
            L.append('- %s:%d 筆,勝率 %.0f%%,損益 %s USDT,平均 R %s' % (name, gs['n'], gs['win'] / gs['n'] * 100, f(gs['pnl'], 2, True), f(gs['avg_r'], 2, True)))
        else:
            L.append('- %s:0 筆' % name)
    cap = [t['r'] / t['planned_rr'] for t in trades if t['r'] and t['r'] > 0 and t['planned_rr'] and not t['distorted']]
    if cap:
        L.append('- 獲利單平均只拿到計畫目標的 %.0f%%(實際 R ÷ 計畫賺賠比;超過 100%% 代表分批出在更遠的目標)。' % (st.mean(cap) * 100))
    L.append('')

    if journal:
        plan, wait, other = journal_counts(journal)
        tot = plan + wait + other
        if tot:
            L += ['## 5. 機器人決策紀錄', '- %d 次決策:給出方案 %d 次(%.0f%%)、觀望 %d 次、其他(資料失敗/測試)%d 次。' % (
                tot, plan, plan / tot * 100, wait, other), '']
    return '\n'.join(L) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('trades')
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--journal')
    ap.add_argument('--out')
    a = ap.parse_args()
    cfg = dict(capital_usd=100.0, risk_pct_per_trade=2.0, soft_max_leverage=3.0, min_rr=2.0)
    try:
        cfg.update(json.load(open(a.config, encoding='utf-8')))
    except FileNotFoundError:
        pass
    txt = build(load_trades(a.trades), cfg, a.journal)
    if a.out:
        open(a.out, 'w', encoding='utf-8').write(txt)
    print(txt)


if __name__ == '__main__':
    main()
