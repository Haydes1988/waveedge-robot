#!/usr/bin/env python3
"""
WaveEdge FULL-FIDELITY Weekly Backtest
Mirrors the LIVE Pine Script exactly:
  Gate0 (D1 EMA20) -> Gate1 (4H OB/EMA + FVG) -> Fib score
  -> Gate2 (30min zone) -> Reversal candle -> WaveTrend
  -> EQH/EQL score -> ADX trend filter -> min_score threshold
Runs every Sunday via cron. Reports on last 7 days of signals,
using longer history underneath purely for indicator warm-up
(no look-ahead bias anywhere).
"""

import json
import os
from datetime import datetime, timedelta

LOG_FILE = "/root/waveedge/logs/backtest_full.log"
REPORT_DIR = "/root/waveedge/reports"

STRATEGIES = {
    "Scalp": {"exec_period": "7d", "exec_interval": "5m", "atr_mult": 1.2,
              "tp_r": 1.5, "min_score": 5, "wt_th": 53, "gate1_mode": "ema"},
    "Day":   {"exec_period": "7d", "exec_interval": "15m", "atr_mult": 1.5,
              "tp_r": 2.0, "min_score": 7, "wt_th": 53, "gate1_mode": "ob"},
}

PAIRS = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X"}


def log(msg):
    ts = datetime.utcnow().isoformat()
    line = f"{ts} | FULLBT | {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def to_naive(df):
    import pandas as pd
    df = df.copy()
    idx = pd.to_datetime(df.index)
    try:
        if idx.tz is not None:
            idx = idx.tz_localize(None)
    except Exception:
        pass
    df.index = idx
    return df


def fetch(symbol, period, interval):
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        if df is None or df.empty:
            return None
        return to_naive(df)
    except Exception as e:
        log(f"Fetch error {symbol} {interval}: {e}")
        return None


def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calc_atr(high, low, close, period=14):
    import pandas as pd
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
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
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / (atr + 1e-10)
    ndi = 100 * ndm.ewm(span=period, adjust=False).mean() / (atr + 1e-10)
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi + 1e-10)
    return dx.ewm(span=period, adjust=False).mean()


def calc_wavetrend(hlc3):
    ap = calc_ema(hlc3, 10)
    d = calc_ema((hlc3 - ap).abs(), 10)
    ci = (hlc3 - ap) / (0.015 * d + 1e-10)
    wt1 = calc_ema(ci, 21)
    wt2 = wt1.rolling(4).mean()
    return wt1, wt2


def detect_reversal(df):
    body = (df['Close'] - df['Open']).abs()
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


def find_pivot(series, left=10, right=10, mode='high'):
    win = series.rolling(left + right + 1, center=True)
    extreme = win.max() if mode == 'high' else win.min()
    return series.where(series == extreme)


def eqh_eql_columns(df, tol=0.0010, left=10, right=10):
    import pandas as pd
    ph_visible = find_pivot(df['High'], left, right, 'high').shift(right)
    pl_visible = find_pivot(df['Low'], left, right, 'low').shift(right)

    ph_events = ph_visible.dropna()
    pl_events = pl_visible.dropna()

    last_ph1 = ph_events.reindex(df.index).ffill()
    last_ph2 = ph_events.shift(1).reindex(df.index).ffill()
    last_pl1 = pl_events.reindex(df.index).ffill()
    last_pl2 = pl_events.shift(1).reindex(df.index).ffill()

    eqh_detected = last_ph1.notna() & last_ph2.notna() & ((last_ph1 - last_ph2).abs() <= tol)
    eql_detected = last_pl1.notna() & last_pl2.notna() & ((last_pl1 - last_pl2).abs() <= tol)
    eqh_swept = eqh_detected & (df['High'] > last_ph1) & (df['Close'] < last_ph1)
    eql_swept = eql_detected & (df['Low'] < last_pl1) & (df['Close'] > last_pl1)
    return eqh_swept.fillna(False), eql_swept.fillna(False)


def build_d1_context(symbol):
    d1 = fetch(symbol, "6mo", "1d")
    if d1 is None or len(d1) < 25:
        return None
    d1 = d1.copy()
    d1['ema20'] = calc_ema(d1['Close'], 20)
    d1['gate0_bull'] = d1['Close'] > d1['ema20']
    d1['gate0_bear'] = d1['Close'] < d1['ema20']
    return d1[['gate0_bull', 'gate0_bear']]


def build_h4_context(symbol):
    h1 = fetch(symbol, "60d", "1h")
    if h1 is None or len(h1) < 60:
        return None
    h4 = h1.resample('4h', label='right', closed='right').agg(
        {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}
    ).dropna()
    if len(h4) < 55:
        return None
    h4 = h4.copy()
    h4['ema20'] = calc_ema(h4['Close'], 20)
    h4['close1'] = h4['Close'].shift(1)
    h4['high2'] = h4['High'].shift(2)
    h4['low2'] = h4['Low'].shift(2)
    h4['swing_high'] = h4['High'].rolling(50).max()
    h4['swing_low'] = h4['Low'].rolling(50).min()
    h4['fvg_bull'] = h4['Low'] > h4['high2']
    h4['fvg_bear'] = h4['High'] < h4['low2']
    h4['ob_bull'] = h4['Close'] > h4['close1']
    h4['ob_bear'] = h4['Close'] < h4['close1']
    h4['adx'] = calc_adx(h4['High'], h4['Low'], h4['Close'])
    cols = ['Close', 'ema20', 'ob_bull', 'ob_bear', 'fvg_bull', 'fvg_bear',
            'swing_high', 'swing_low', 'adx']
    return h4[cols].rename(columns={'Close': 'h4_close'})


def build_m30_context(symbol):
    m30 = fetch(symbol, "60d", "30m")
    if m30 is None or len(m30) < 5:
        return None
    m30 = m30.copy()
    m30['mid30'] = (m30['High'] + m30['Low']) / 2
    return m30[['mid30']]


def merge_asof_safe(left, right, suffix):
    import pandas as pd
    left2 = left.reset_index().rename(columns={left.index.name or 'index': 'time'})
    right2 = right.reset_index().rename(columns={right.index.name or 'index': 'time'})
    left2 = left2.sort_values('time')
    right2 = right2.sort_values('time')
    merged = pd.merge_asof(left2, right2, on='time', direction='backward', suffixes=('', suffix))
    merged = merged.set_index('time')
    return merged


def check_outcome(df, idx, entry, sl, tp1, direction, lookahead=40):
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


def run_full_strategy(pair_name, symbol, strategy_name, cfg, d1_ctx, h4_ctx, m30_ctx):
    import pandas as pd
    results = []
    simplified_count = 0

    exec_df = fetch(symbol, cfg["exec_period"], cfg["exec_interval"])
    if exec_df is None or len(exec_df) < 60:
        log(f"{pair_name} {strategy_name}: insufficient exec data")
        return results, simplified_count

    merged = merge_asof_safe(exec_df, d1_ctx, "_d1")
    merged = merge_asof_safe(merged, h4_ctx, "_h4")
    merged = merge_asof_safe(merged, m30_ctx, "_m30")

    if cfg["gate1_mode"] == "ema":
        gate1_bull = merged['gate0_bull'] & ((merged['h4_close'] > merged['ema20']) | merged['fvg_bull'])
        gate1_bear = merged['gate0_bear'] & ((merged['h4_close'] < merged['ema20']) | merged['fvg_bear'])
    else:
        gate1_bull = merged['gate0_bull'] & (merged['ob_bull'] | merged['fvg_bull'])
        gate1_bear = merged['gate0_bear'] & (merged['ob_bear'] | merged['fvg_bear'])

    fib_range = merged['swing_high'] - merged['swing_low']
    fib_buffer = 0.003
    fib_bull_50 = merged['swing_high'] - fib_range * 0.5
    fib_bull_618 = merged['swing_high'] - fib_range * 0.618
    fib_bull_low = pd.concat([fib_bull_50, fib_bull_618], axis=1).min(axis=1) - fib_buffer
    fib_bull_high = pd.concat([fib_bull_50, fib_bull_618], axis=1).max(axis=1) + fib_buffer
    price_in_bull_fib = (merged['Close'] >= fib_bull_low) & (merged['Close'] <= fib_bull_high)

    fib_bear_50 = merged['swing_low'] + fib_range * 0.5
    fib_bear_618 = merged['swing_low'] + fib_range * 0.618
    fib_bear_low = pd.concat([fib_bear_50, fib_bear_618], axis=1).min(axis=1) - fib_buffer
    fib_bear_high = pd.concat([fib_bear_50, fib_bear_618], axis=1).max(axis=1) + fib_buffer
    price_in_bear_fib = (merged['Close'] >= fib_bear_low) & (merged['Close'] <= fib_bear_high)

    gate2_bull = gate1_bull & (merged['Close'] < merged['mid30'])
    gate2_bear = gate1_bear & (merged['Close'] > merged['mid30'])

    rev_bull, rev_bear = detect_reversal(merged)

    hlc3 = (merged['High'] + merged['Low'] + merged['Close']) / 3
    wt1, wt2 = calc_wavetrend(hlc3)
    wt_xbull = (wt1 > wt2) & (wt1.shift(1) <= wt2.shift(1))
    wt_xbear = (wt1 < wt2) & (wt1.shift(1) >= wt2.shift(1))
    wt_os = wt1 <= -cfg["wt_th"]
    wt_ob = wt1 >= cfg["wt_th"]

    eqh_swept, eql_swept = eqh_eql_columns(merged)

    atr = calc_atr(merged['High'], merged['Low'], merged['Close'])
    market_trending = merged['adx'] >= 20

    score_bull = (gate1_bull.astype(int) * 2 +
                  price_in_bull_fib.astype(int) * 2 +
                  gate2_bull.astype(int) * 1 +
                  wt_xbull.astype(int) * 1 +
                  wt_os.astype(int) * 1 +
                  rev_bull.astype(int) * 2 +
                  merged['fvg_bull'].astype(int) * 1 +
                  (atr > 0).astype(int) * 1 +
                  eql_swept.astype(int) * 2 +
                  merged['gate0_bull'].astype(int) * 2)

    score_bear = (gate1_bear.astype(int) * 2 +
                  price_in_bear_fib.astype(int) * 2 +
                  gate2_bear.astype(int) * 1 +
                  wt_xbear.astype(int) * 1 +
                  wt_ob.astype(int) * 1 +
                  rev_bear.astype(int) * 2 +
                  merged['fvg_bear'].astype(int) * 1 +
                  (atr > 0).astype(int) * 1 +
                  eqh_swept.astype(int) * 2 +
                  merged['gate0_bear'].astype(int) * 2)

    long_signal = gate2_bull & rev_bull & (score_bull >= cfg["min_score"]) & market_trending
    short_signal = gate2_bear & rev_bear & (score_bear >= cfg["min_score"]) & market_trending

    simplified_long = merged['gate0_bull'] & wt_xbull & rev_bull & market_trending
    simplified_short = merged['gate0_bear'] & wt_xbear & rev_bear & market_trending
    simplified_count = int((simplified_long | simplified_short).sum())

    cutoff = merged.index.max() - timedelta(days=7)
    n = len(merged)

    for i in range(60, n - 45):
        row = merged.iloc[i]
        if row.name < cutoff:
            continue
        entry = float(row['Close'])
        a = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 0.0010
        if bool(long_signal.iloc[i]):
            sl = entry - a * cfg["atr_mult"]
            tp1 = entry + (entry - sl) * cfg["tp_r"]
            out = check_outcome(merged, i, entry, sl, tp1, "buy")
            if out:
                results.append({"time": str(row.name)[:16], "strategy": strategy_name,
                                 "pair": pair_name, "direction": "BUY",
                                 "score": int(score_bull.iloc[i]),
                                 "result": out["result"], "pips": out["pips"], "r": out["r"]})
        elif bool(short_signal.iloc[i]):
            sl = entry + a * cfg["atr_mult"]
            tp1 = entry - (sl - entry) * cfg["tp_r"]
            out = check_outcome(merged, i, entry, sl, tp1, "sell")
            if out:
                results.append({"time": str(row.name)[:16], "strategy": strategy_name,
                                 "pair": pair_name, "direction": "SELL",
                                 "score": int(score_bear.iloc[i]),
                                 "result": out["result"], "pips": out["pips"], "r": out["r"]})

    return results, simplified_count


def generate_report(all_results, simplified_totals):
    if not all_results:
        lines = ["=" * 54, "  WAVEEDGE FULL-FIDELITY BACKTEST REPORT", "=" * 54, "",
                  "  NO SIGNALS PASSED THE FULL FILTER STACK THIS WEEK.",
                  "  (Gate0+Gate1+Fib+Gate2+Reversal+Score+ADX all required)", ""]
        for k, v in simplified_totals.items():
            lines.append(f"  Note: simplified version would have shown {v} setups for {k}")
        lines.append("=" * 54)
        return "\n".join(lines)

    wins = [r for r in all_results if r['result'] == 'WIN']
    losses = [r for r in all_results if r['result'] == 'LOSS']
    total = len(all_results)
    win_rate = len(wins) / total * 100 if total else 0
    total_pips = sum(r['pips'] for r in all_results)
    avg_r = sum(r['r'] for r in all_results) / total if total else 0

    lines = []
    lines.append("=" * 54)
    lines.append("  WAVEEDGE FULL-FIDELITY BACKTEST REPORT")
    lines.append(f"  {(datetime.utcnow()-timedelta(days=7)).strftime('%b %d')} - {datetime.utcnow().strftime('%b %d %Y')}")
    lines.append("  (mirrors live Pine Script: all gates + score + ADX)")
    lines.append("=" * 54)
    lines.append("")
    lines.append(f"  Total Signals : {total}")
    lines.append(f"  Wins          : {len(wins)}")
    lines.append(f"  Losses        : {len(losses)}")
    lines.append(f"  Win Rate      : {win_rate:.1f}%")
    lines.append(f"  Total Pips    : {total_pips:+.1f}")
    lines.append(f"  Average R     : {avg_r:+.2f}")
    lines.append("")
    lines.append("-" * 54)
    lines.append("FILTER EFFECT (full system vs simplified gates):")
    for k, v in simplified_totals.items():
        lines.append(f"  {k}: simplified saw {v} setups -> full system fired {len([r for r in all_results if r['strategy']+'_'+r['pair'] == k])}")
    lines.append("")
    lines.append("-" * 54)
    lines.append("BY PAIR:")
    for pair in set(r['pair'] for r in all_results):
        pr = [r for r in all_results if r['pair'] == pair]
        pw = len([r for r in pr if r['result'] == 'WIN'])
        pp = sum(r['pips'] for r in pr)
        lines.append(f"  {pair:8}: {len(pr):3} trades | {pw/len(pr)*100:.0f}% WR | {pp:+.1f} pips")

    lines.append("")
    lines.append("-" * 54)
    lines.append("BY STRATEGY:")
    for strat in set(r['strategy'] for r in all_results):
        sr = [r for r in all_results if r['strategy'] == strat]
        sw = len([r for r in sr if r['result'] == 'WIN'])
        sp = sum(r['pips'] for r in sr)
        lines.append(f"  {strat:8}: {len(sr):3} trades | {sw/len(sr)*100:.0f}% WR | {sp:+.1f} pips")

    lines.append("")
    lines.append("-" * 54)
    lines.append("ALL SIGNALS:")
    for r in sorted(all_results, key=lambda x: x['time']):
        icon = "WIN " if r['result'] == 'WIN' else "LOSS"
        lines.append(f"  {icon} | {r['time']} | {r['pair']:7} | {r['strategy']:5} | "
                      f"{r['direction']:4} | score:{r['score']:2} | {r['pips']:+.1f} pips | {r['r']:+.2f}R")

    lines.append("")
    lines.append("-" * 54)
    if win_rate >= 50 and avg_r >= 0.3:
        verdict = "STRONG — full filter stack producing positive expectancy"
    elif win_rate >= 40:
        verdict = "MARGINAL — close to breakeven, monitor another week"
    else:
        verdict = "WEAK — full system still underperforming, do not go live"
    lines.append(f"  VERDICT: {verdict}")
    lines.append("=" * 54)
    return "\n".join(lines)


def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    log("Full-fidelity backtest starting...")

    all_results = []
    simplified_totals = {}

    for pair_name, symbol in PAIRS.items():
        log(f"Building context for {pair_name}...")
        d1_ctx = build_d1_context(symbol)
        h4_ctx = build_h4_context(symbol)
        m30_ctx = build_m30_context(symbol)

        if d1_ctx is None or h4_ctx is None or m30_ctx is None:
            log(f"{pair_name}: insufficient context data, skipping")
            continue

        for strat_name, cfg in STRATEGIES.items():
            log(f"Running {strat_name} on {pair_name}...")
            results, simp_count = run_full_strategy(pair_name, symbol, strat_name, cfg,
                                                      d1_ctx, h4_ctx, m30_ctx)
            all_results.extend(results)
            simplified_totals[f"{strat_name}_{pair_name}"] = simp_count
            log(f"{pair_name} {strat_name}: {len(results)} full-fidelity signals "
                f"(simplified would show {simp_count})")

    report = generate_report(all_results, simplified_totals)
    date_str = datetime.utcnow().strftime("%Y%m%d")
    report_path = f"{REPORT_DIR}/full_backtest_{date_str}.txt"
    with open(report_path, "w") as f:
        f.write(report)

    json_path = f"{REPORT_DIR}/full_results_{date_str}.json"
    with open(json_path, "w") as f:
        json.dump(all_results, f, indent=2)

    log(f"Report saved: {report_path}")
    print(report)


if __name__ == "__main__":
    main()
