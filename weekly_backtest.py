#!/usr/bin/env python3
"""
WaveEdge Weekly Backtest Robot
Runs every Sunday — simulates Scalp + Day strategies
Generates performance report with win rate, pips, R multiple
"""

import sys
import json
import os
from datetime import datetime, timedelta

LOG_FILE = "/root/waveedge/logs/backtest.log"
REPORT_DIR = "/root/waveedge/reports"

def log(msg):
    ts = datetime.utcnow().isoformat()
    line = f"{ts} | BACKTEST | {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_atr(high, low, close, period=14):
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    import pandas as pd
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calc_adx(high, low, close, period=14):
    import pandas as pd
    import numpy as np
    up = high - high.shift(1)
    down = low.shift(1) - low
    pdm = pd.Series(np.where((up > down) & (up > 0), up, 0), index=high.index)
    ndm = pd.Series(np.where((down > up) & (down > 0), down, 0), index=low.index)
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / atr
    ndi = 100 * ndm.ewm(span=period, adjust=False).mean() / atr
    dx = 100 * abs(pdi - ndi) / (pdi + ndi + 1e-10)
    return dx.ewm(span=period, adjust=False).mean()

def calc_wavetrend(hlc3):
    ap = calc_ema(hlc3, 10)
    d = calc_ema(abs(hlc3 - ap), 10)
    ci = (hlc3 - ap) / (0.015 * d + 1e-10)
    wt1 = calc_ema(ci, 21)
    wt2 = wt1.rolling(4).mean()
    return wt1, wt2

def detect_reversal(df):
    import pandas as pd
    import numpy as np
    body = abs(df['Close'] - df['Open'])
    rng = df['High'] - df['Low']
    upper = df['High'] - df[['Close', 'Open']].max(axis=1)
    lower = df[['Close', 'Open']].min(axis=1) - df['Low']
    bull_engulf = ((df['Close'] > df['Open']) & (df['Close'] > df['Open'].shift(1)) &
                   (df['Open'] < df['Close'].shift(1)) & (body > body.shift(1) * 0.8))
    bear_engulf = ((df['Close'] < df['Open']) & (df['Close'] < df['Open'].shift(1)) &
                   (df['Open'] > df['Close'].shift(1)) & (body > body.shift(1) * 0.8))
    bull_pin = (lower >= body * 2.0) & (df['Close'] >= df['Low'] + rng * 0.6)
    bear_pin = (upper >= body * 2.0) & (df['Close'] <= df['High'] - rng * 0.6)
    return bull_engulf | bull_pin, bear_engulf | bear_pin

def check_outcome(df, idx, entry, sl, tp1, direction, lookahead=30):
    sl_dist = abs(entry - sl)
    if sl_dist < 0.00001:
        return None
    for j in range(idx + 1, min(idx + lookahead + 1, len(df))):
        c = df.iloc[j]
        if direction == "buy":
            if c['Low'] <= sl:
                return {"result": "LOSS", "pips": round((sl - entry) * 10000, 1), "r": -1.0}
            if c['High'] >= tp1:
                return {"result": "WIN", "pips": round((tp1 - entry) * 10000, 1),
                        "r": round((tp1 - entry) / sl_dist, 2)}
        else:
            if c['High'] >= sl:
                return {"result": "LOSS", "pips": round((entry - sl) * 10000, 1), "r": -1.0}
            if c['Low'] <= tp1:
                return {"result": "WIN", "pips": round((entry - tp1) * 10000, 1),
                        "r": round((entry - tp1) / sl_dist, 2)}
    return None

def run_strategy(df_d1, df_exec, strategy, atr_mult, tp_r):
    import pandas as pd
    results = []
    if df_d1 is None or df_exec is None:
        return results
    df_d1['ema20'] = calc_ema(df_d1['Close'], 20)
    d1_bull = float(df_d1['Close'].iloc[-1]) > float(df_d1['ema20'].iloc[-1])
    d1_bear = not d1_bull
    hlc3 = (df_exec['High'] + df_exec['Low'] + df_exec['Close']) / 3
    wt1, wt2 = calc_wavetrend(hlc3)
    wt_xbull = (wt1 > wt2) & (wt1.shift(1) <= wt2.shift(1))
    wt_xbear = (wt1 < wt2) & (wt1.shift(1) >= wt2.shift(1))
    adx = calc_adx(df_exec['High'], df_exec['Low'], df_exec['Close'])
    rev_bull, rev_bear = detect_reversal(df_exec)
    atr = calc_atr(df_exec['High'], df_exec['Low'], df_exec['Close'])
    for i in range(25, len(df_exec) - 35):
        if float(adx.iloc[i]) < 20:
            continue
        row = df_exec.iloc[i]
        entry = float(row['Close'])
        a = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 0.0010
        if d1_bull and bool(wt_xbull.iloc[i]) and bool(rev_bull.iloc[i]):
            sl = entry - a * atr_mult
            tp1 = entry + (entry - sl) * tp_r
            out = check_outcome(df_exec, i, entry, sl, tp1, "buy")
            if out:
                results.append({"time": str(row.name)[:16], "strategy": strategy,
                                 "direction": "BUY", "result": out["result"],
                                 "pips": out["pips"], "r": out["r"]})
        elif d1_bear and bool(wt_xbear.iloc[i]) and bool(rev_bear.iloc[i]):
            sl = entry + a * atr_mult
            tp1 = entry - (sl - entry) * tp_r
            out = check_outcome(df_exec, i, entry, sl, tp1, "sell")
            if out:
                results.append({"time": str(row.name)[:16], "strategy": strategy,
                                 "direction": "SELL", "result": out["result"],
                                 "pips": out["pips"], "r": out["r"]})
    return results

def fetch(symbol, period, interval):
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        return df if not df.empty else None
    except Exception as e:
        log(f"Fetch error {symbol} {interval}: {e}")
        return None

def generate_report(results_by_pair):
    all_results = []
    for pair_data in results_by_pair.values():
        for r in pair_data:
            all_results.extend(r)

    if not all_results:
        return "NO SIGNALS DETECTED THIS WEEK. Market may have been ranging."

    wins = [r for r in all_results if r['result'] == 'WIN']
    losses = [r for r in all_results if r['result'] == 'LOSS']
    total = len(all_results)
    win_rate = len(wins) / total * 100 if total > 0 else 0
    total_pips = sum(r['pips'] for r in all_results)
    avg_r = sum(r['r'] for r in all_results) / total if total > 0 else 0

    lines = []
    lines.append("=" * 52)
    lines.append("  WAVEEDGE WEEKLY BACKTEST REPORT")
    lines.append(f"  {(datetime.utcnow()-timedelta(days=7)).strftime('%b %d')} - {datetime.utcnow().strftime('%b %d %Y')}")
    lines.append("=" * 52)
    lines.append("")
    lines.append(f"  Total Signals : {total}")
    lines.append(f"  Wins          : {len(wins)}")
    lines.append(f"  Losses        : {len(losses)}")
    lines.append(f"  Win Rate      : {win_rate:.1f}%")
    lines.append(f"  Total Pips    : {total_pips:+.1f}")
    lines.append(f"  Average R     : {avg_r:+.2f}")
    lines.append("")
    lines.append("-" * 52)
    lines.append("BY PAIR:")

    for pair, pair_list in results_by_pair.items():
        pair_results = []
        for r in pair_list:
            pair_results.extend(r)
        if not pair_results:
            continue
        pw = len([r for r in pair_results if r['result'] == 'WIN'])
        pt = len(pair_results)
        pp = sum(r['pips'] for r in pair_results)
        lines.append(f"  {pair:8}: {pt:3} trades | {pw/pt*100:.0f}% WR | {pp:+.1f} pips")

    lines.append("")
    lines.append("-" * 52)
    lines.append("BY STRATEGY:")

    strategies = {}
    for r in all_results:
        s = r['strategy']
        if s not in strategies:
            strategies[s] = []
        strategies[s].append(r)

    for s, sr in strategies.items():
        sw = len([r for r in sr if r['result'] == 'WIN'])
        st = len(sr)
        sp = sum(r['pips'] for r in sr)
        lines.append(f"  {s:8}: {st:3} trades | {sw/st*100:.0f}% WR | {sp:+.1f} pips")

    lines.append("")
    lines.append("-" * 52)
    lines.append("ALL SIGNALS:")

    for r in sorted(all_results, key=lambda x: x['time']):
        icon = "WIN " if r['result'] == 'WIN' else "LOSS"
        lines.append(f"  {icon} | {r['time']} | {r['strategy']:5} | {r['direction']:4} | {r['pips']:+.1f} pips | {r['r']:+.2f}R")

    lines.append("")
    lines.append("-" * 52)
    if win_rate >= 55 and avg_r >= 1.0:
        verdict = "STRONG WEEK — Keep current settings"
    elif win_rate >= 45:
        verdict = "AVERAGE WEEK — Monitor next week"
    else:
        verdict = "WEAK WEEK — Review settings before Monday"

    lines.append(f"  VERDICT: {verdict}")
    lines.append("=" * 52)
    return "\n".join(lines)

def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    log("Weekly backtest starting...")

    pairs = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X"}
    results_by_pair = {}

    for pair_name, symbol in pairs.items():
        log(f"Processing {pair_name}...")
        df_d1 = fetch(symbol, "3mo", "1d")
        df_15m = fetch(symbol, "7d", "15m")
        df_5m = fetch(symbol, "7d", "5m")

        scalp = run_strategy(df_d1, df_5m, "Scalp", 1.2, 1.5) if df_5m is not None else []
        day = run_strategy(df_d1, df_15m, "Day", 1.5, 2.0) if df_15m is not None else []

        results_by_pair[pair_name] = [scalp, day]
        log(f"{pair_name}: {len(scalp)} scalp, {len(day)} day signals")

    report = generate_report(results_by_pair)
    date_str = datetime.utcnow().strftime("%Y%m%d")
    report_path = f"{REPORT_DIR}/backtest_{date_str}.txt"

    with open(report_path, "w") as f:
        f.write(report)

    all_results = []
    for pair_list in results_by_pair.values():
        for r in pair_list:
            all_results.extend(r)

    json_path = f"{REPORT_DIR}/results_{date_str}.json"
    with open(json_path, "w") as f:
        json.dump(all_results, f, indent=2)

    log(f"Report saved: {report_path}")
    print(report)

if __name__ == "__main__":
    main()
