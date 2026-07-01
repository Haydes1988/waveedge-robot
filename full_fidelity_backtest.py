#!/usr/bin/env python3
"""
WaveEdge FULL-FIDELITY Weekly Backtest v3
NEW: Key levels integration — Previous Week H/L, Previous Day H/L, Round Numbers.
All three are score contributors (+1 each, max score now 18).
Fixes: JPY pip calculation, pair-aware sizing.
"""

import json
import os
from datetime import datetime, timedelta

LOG_FILE = "/root/waveedge/logs/backtest_full.log"
REPORT_DIR = "/root/waveedge/reports"

STRATEGIES = {
    "Scalp": {"exec_period": "7d", "exec_interval": "5m",  "atr_mult": 1.2, "tp_r": 1.5, "min_score": 5, "wt_th": 53, "gate1_mode": "ema"},
    "Day":   {"exec_period": "7d", "exec_interval": "15m", "atr_mult": 1.5, "tp_r": 2.0, "min_score": 7, "wt_th": 53, "gate1_mode": "ob"},
}

PAIRS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X"
}

PIP_SIZES = {
    "EURUSD": 0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01,
    "AUDUSD": 0.0001, "USDCAD": 0.0001, "USDCHF": 0.0001,
    "NZDUSD": 0.0001
}

# Round number granularity per pair (price increments that matter institutionally)
ROUND_LEVELS = {
    "EURUSD": [0.01],  "GBPUSD": [0.01],
    "USDJPY": [1.0],   "AUDUSD": [0.01],
    "USDCAD": [0.01],  "USDCHF": [0.01],
    "NZDUSD": [0.01]
}


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
                   (df['Open'] > df['close'].shift(1)) & (body > body.shift(1) * 0.8))
    bull_pin = (lower >= body * 2.0) & (df['Close'] >= df['Low'] + rng * 0.6)
    bear_pin = (upper >= body * 2.0) & (df['Close'] <= df['High'] - rng * 0.6)
    return bull_engulf | bull_pin, bear_engulf | bear_pin


def detect_reversal(df):
    body = (df['Close'] - df['Open']).abs()
    rng = df['High'] - df['Low']
    upper = df['High'] - df[['Close', 'Open']].max(axis=1)
    lower = df[['Close', 'Open']].min(axis=1) - df['Low']
    bull_engulf = ((df['Close'] > df['Open']) & (df['Close'] > df['Open'].shift(1)) &
                   (df['Open'] < df['Close'].shift(1)) & (body > body.shift(1) * 0.8))
    bear_engulf = ((df['Close'] < df['Open']) & (df['Close'] < df['Open'].shift(1)) &
                   (df['Open'] > df['Open'].shift(1)) & (body > body.shift(1) * 0.8))
    bull_pin = (lower >= body * 2.0) & (df['Close'] >= df['Low'] + rng * 0.6)
    bear_pin = (upper >= body * 2.0) & (df['Close'] <= df['High'] - rng * 0.6)
    return bull_engulf | bull_pin, bear_engulf | bear_pin


def find_pivot(series, left=10, right=10, mode='high'):
    win = series.rolling(left + right + 1, center=True)
    extreme = win.max() if mode == 'high' else win.min()
    return series.where(series == extreme)


def eqh_eql_columns(df, tol=0.0010):
    ph_visible = find_pivot(df['High'], 10, 10, 'high').shift(10)
    pl_visible = find_pivot(df['Low'],  10, 10, 'low').shift(10)
    last_ph1 = ph_visible.dropna().reindex(df.index).ffill()
    last_ph2 = ph_visible.shift(1).dropna().reindex(df.index).ffill()
    last_pl1 = pl_visible.dropna().reindex(df.index).ffill()
    last_pl2 = pl_visible.shift(1).dropna().reindex(df.index).ffill()
    eqh_det = last_ph1.notna() & last_ph2.notna() & ((last_ph1 - last_ph2).abs() <= tol)
    eql_det = last_pl1.notna() & last_pl2.notna() & ((last_pl1 - last_pl2).abs() <= tol)
    eqh_swept = eqh_det & (df['High'] > last_ph1) & (df['Close'] < last_ph1)
    eql_swept = eql_det & (df['Low'] < last_pl1) & (df['Close'] > last_pl1)
    return eqh_swept.fillna(False), eql_swept.fillna(False)


# ─── KEY LEVEL FUNCTIONS ──────────────────────────────────────────────────────

def build_weekly_levels(symbol):
    """Previous Week High and Low — major institutional reference levels."""
    try:
        import pandas as pd
        df_daily = fetch(symbol, "3mo", "1d")
        if df_daily is None or len(df_daily) < 10:
            return None
        df_weekly = df_daily.resample('W', label='right', closed='right').agg(
            {'High': 'max', 'Low': 'min'}
        ).dropna()
        if len(df_weekly) < 3:
            return None
        pwh = df_weekly['High'].shift(1)
        pwl = df_weekly['Low'].shift(1)
        return df_weekly.assign(pwh=pwh, pwl=pwl)[['pwh', 'pwl']].dropna()
    except Exception as e:
        log(f"Weekly levels error: {e}")
        return None


def build_daily_levels(symbol):
    """Previous Day High and Low — session reference levels."""
    try:
        df_daily = fetch(symbol, "3mo", "1d")
        if df_daily is None or len(df_daily) < 5:
            return None
        pdh = df_daily['High'].shift(1)
        pdl = df_daily['Low'].shift(1)
        return df_daily.assign(pdh=pdh, pdl=pdl)[['pdh', 'pdl']].dropna()
    except Exception as e:
        log(f"Daily levels error: {e}")
        return None


def near_round_number(price, pair_name, pip_size):
    """Check if price is near a psychological round number level."""
    levels = ROUND_LEVELS.get(pair_name, [0.01, 0.005])
    buffer = pip_size * 8   # must be genuinely near a major round figure
    for lvl in levels:
        nearest = round(price / lvl) * lvl
        if abs(price - nearest) <= buffer:
            return True
    return False


def near_key_level(price, pwh, pwl, pdh, pdl, pair_name, pip_size):
    """
    Returns score contribution (0, 1, 2, or 3) based on proximity to:
    - Previous Week High/Low (+1)
    - Previous Day High/Low  (+1)
    - Round number           (+1)
    """
    score = 0
    buf_week = pip_size * 10  # must be close to previous week H/L
    buf_day  = pip_size * 8   # must be tight to previous day H/L

    if pwh is not None and not (pwh != pwh):  # not NaN
        if abs(price - pwh) <= buf_week or abs(price - pwl) <= buf_week:
            score += 1

    if pdh is not None and not (pdh != pdh):
        if abs(price - pdh) <= buf_day or abs(price - pdl) <= buf_day:
            score += 1

    if near_round_number(price, pair_name, pip_size):
        score += 1

    return score


def check_outcome(df, idx, entry, sl, tp1, direction, pip_size=0.0001, lookahead=40):
    sl_dist = abs(entry - sl)
    if sl_dist < pip_size * 0.5:
        return None
    pip_mult = 1.0 / pip_size
    for j in range(idx + 1, min(idx + lookahead + 1, len(df))):
        c = df.iloc[j]
        if direction == "buy":
            if c['Low'] <= sl:
                return {"result": "LOSS", "pips": round((sl - entry) * pip_mult, 1), "r": -1.0}
            if c['High'] >= tp1:
                return {"result": "WIN", "pips": round((tp1 - entry) * pip_mult, 1),
                        "r": round((tp1 - entry) / sl_dist, 2)}
        else:
            if c['High'] >= sl:
                return {"result": "LOSS", "pips": round((entry - sl) * pip_mult, 1), "r": -1.0}
            if c['Low'] <= tp1:
                return {"result": "WIN", "pips": round((entry - tp1) * pip_mult, 1),
                        "r": round((entry - tp1) / sl_dist, 2)}
    return None


# ─── CONTEXT BUILDERS ────────────────────────────────────────────────────────

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
    h4['ema20']      = calc_ema(h4['Close'], 20)
    h4['close1']     = h4['Close'].shift(1)
    h4['high2']      = h4['High'].shift(2)
    h4['low2']       = h4['Low'].shift(2)
    h4['swing_high'] = h4['High'].rolling(50).max()
    h4['swing_low']  = h4['Low'].rolling(50).min()
    h4['fvg_bull']   = h4['Low'] > h4['high2']
    h4['fvg_bear']   = h4['High'] < h4['low2']
    h4['ob_bull']    = h4['Close'] > h4['close1']
    h4['ob_bear']    = h4['Close'] < h4['close1']
    h4['adx']        = calc_adx(h4['High'], h4['Low'], h4['Close'])
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
    left2  = left.reset_index().rename(columns={left.index.name or 'index': 'time'})
    right2 = right.reset_index().rename(columns={right.index.name or 'index': 'time'})
    left2  = left2.sort_values('time')
    right2 = right2.sort_values('time')
    merged = pd.merge_asof(left2, right2, on='time', direction='backward', suffixes=('', suffix))
    return merged.set_index('time')


# ─── MAIN STRATEGY RUNNER ────────────────────────────────────────────────────

def run_full_strategy(pair_name, symbol, strategy_name, cfg,
                      d1_ctx, h4_ctx, m30_ctx, weekly_lvl, daily_lvl):
    import pandas as pd
    results = []
    pip_size = PIP_SIZES.get(pair_name, 0.0001)

    exec_df = fetch(symbol, cfg["exec_period"], cfg["exec_interval"])
    if exec_df is None or len(exec_df) < 60:
        log(f"{pair_name} {strategy_name}: insufficient exec data")
        return results

    merged = merge_asof_safe(exec_df, d1_ctx, "_d1")
    merged = merge_asof_safe(merged, h4_ctx, "_h4")
    merged = merge_asof_safe(merged, m30_ctx, "_m30")

    if cfg["gate1_mode"] == "ema":
        gate1_bull = merged['gate0_bull'] & ((merged['h4_close'] > merged['ema20']) | merged['fvg_bull'])
        gate1_bear = merged['gate0_bear'] & ((merged['h4_close'] < merged['ema20']) | merged['fvg_bear'])
    else:
        gate1_bull = merged['gate0_bull'] & (merged['ob_bull'] | merged['fvg_bull'])
        gate1_bear = merged['gate0_bear'] & (merged['ob_bear'] | merged['fvg_bear'])

    fib_range    = merged['swing_high'] - merged['swing_low']
    fib_buffer   = pip_size * 30
    fib_bull_50  = merged['swing_high'] - fib_range * 0.5
    fib_bull_618 = merged['swing_high'] - fib_range * 0.618
    fib_bull_low  = pd.concat([fib_bull_50, fib_bull_618], axis=1).min(axis=1) - fib_buffer
    fib_bull_high = pd.concat([fib_bull_50, fib_bull_618], axis=1).max(axis=1) + fib_buffer
    price_in_bull_fib = (merged['Close'] >= fib_bull_low) & (merged['Close'] <= fib_bull_high)

    fib_bear_50  = merged['swing_low'] + fib_range * 0.5
    fib_bear_618 = merged['swing_low'] + fib_range * 0.618
    fib_bear_low  = pd.concat([fib_bear_50, fib_bear_618], axis=1).min(axis=1) - fib_buffer
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

    # Base score (max 15) — unchanged from live Pine Script
    score_bull = (merged['gate0_bull'].astype(int) * 2 + gate1_bull.astype(int) * 2 +
                  price_in_bull_fib.astype(int) * 2 + gate2_bull.astype(int) * 1 +
                  wt_xbull.astype(int) * 1 + wt_os.astype(int) * 1 +
                  rev_bull.astype(int) * 2 + merged['fvg_bull'].astype(int) * 1 +
                  (atr > 0).astype(int) * 1 + eql_swept.astype(int) * 2)

    score_bear = (merged['gate0_bear'].astype(int) * 2 + gate1_bear.astype(int) * 2 +
                  price_in_bear_fib.astype(int) * 2 + gate2_bear.astype(int) * 1 +
                  wt_xbear.astype(int) * 1 + wt_ob.astype(int) * 1 +
                  rev_bear.astype(int) * 2 + merged['fvg_bear'].astype(int) * 1 +
                  (atr > 0).astype(int) * 1 + eqh_swept.astype(int) * 2)

    long_signal  = gate2_bull & rev_bull & (score_bull >= cfg["min_score"]) & market_trending
    short_signal = gate2_bear & rev_bear & (score_bear >= cfg["min_score"]) & market_trending

    cutoff = merged.index.max() - timedelta(days=7)
    n = len(merged)

    for i in range(60, n - 45):
        row = merged.iloc[i]
        if row.name < cutoff:
            continue

        entry    = float(row['Close'])
        bar_date = row.name
        a = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else pip_size * 15

        # Look up key levels as of this bar's date
        pwh = pwl = pdh = pdl = None
        if weekly_lvl is not None:
            wk = weekly_lvl[weekly_lvl.index <= bar_date]
            if not wk.empty:
                pwh = float(wk['pwh'].iloc[-1])
                pwl = float(wk['pwl'].iloc[-1])
        if daily_lvl is not None:
            dy = daily_lvl[daily_lvl.index <= bar_date]
            if not dy.empty:
                pdh = float(dy['pdh'].iloc[-1])
                pdl = float(dy['pdl'].iloc[-1])

        key_score = near_key_level(entry, pwh, pwl, pdh, pdl, pair_name, pip_size)

        if bool(long_signal.iloc[i]):
            final_score = int(score_bull.iloc[i]) + key_score
            sl  = entry - a * cfg["atr_mult"]
            tp1 = entry + (entry - sl) * cfg["tp_r"]
            out = check_outcome(merged, i, entry, sl, tp1, "buy", pip_size)
            if out:
                results.append({
                    "time": str(row.name)[:16], "strategy": strategy_name,
                    "pair": pair_name, "direction": "BUY",
                    "score": final_score, "key_score": key_score,
                    "at_key_level": key_score > 0,
                    "result": out["result"], "pips": out["pips"], "r": out["r"]
                })

        elif bool(short_signal.iloc[i]):
            final_score = int(score_bear.iloc[i]) + key_score
            sl  = entry + a * cfg["atr_mult"]
            tp1 = entry - (sl - entry) * cfg["tp_r"]
            out = check_outcome(merged, i, entry, sl, tp1, "sell", pip_size)
            if out:
                results.append({
                    "time": str(row.name)[:16], "strategy": strategy_name,
                    "pair": pair_name, "direction": "SELL",
                    "score": final_score, "key_score": key_score,
                    "at_key_level": key_score > 0,
                    "result": out["result"], "pips": out["pips"], "r": out["r"]
                })

    return results


# ─── REPORT GENERATOR ────────────────────────────────────────────────────────

def generate_report(all_results):
    if not all_results:
        return "NO SIGNALS PASSED FULL FILTER STACK THIS WEEK."

    wins   = [r for r in all_results if r['result'] == 'WIN']
    losses = [r for r in all_results if r['result'] == 'LOSS']
    total  = len(all_results)
    wr     = len(wins) / total * 100 if total else 0
    tp     = sum(r['pips'] for r in all_results)
    ar     = sum(r['r'] for r in all_results) / total if total else 0

    at_key   = [r for r in all_results if r['at_key_level']]
    off_key  = [r for r in all_results if not r['at_key_level']]
    key_wr   = len([r for r in at_key if r['result'] == 'WIN']) / len(at_key) * 100 if at_key else 0
    off_wr   = len([r for r in off_key if r['result'] == 'WIN']) / len(off_key) * 100 if off_key else 0
    key_r    = sum(r['r'] for r in at_key) / len(at_key) if at_key else 0
    off_r    = sum(r['r'] for r in off_key) / len(off_key) if off_key else 0

    lines = []
    lines.append("=" * 58)
    lines.append("  WAVEEDGE FULL-FIDELITY BACKTEST REPORT v3.1")
    lines.append(f"  {(datetime.utcnow()-timedelta(days=7)).strftime('%b %d')} - {datetime.utcnow().strftime('%b %d %Y')}")
    lines.append("  Key Levels: PWH/PWL(10pip) + PDH/PDL(8pip) + Round(8pip)")
    lines.append("=" * 58)
    lines.append("")
    lines.append(f"  Total Signals : {total}")
    lines.append(f"  Wins          : {len(wins)}")
    lines.append(f"  Losses        : {len(losses)}")
    lines.append(f"  Win Rate      : {wr:.1f}%")
    lines.append(f"  Total Pips    : {tp:+.1f}")
    lines.append(f"  Average R     : {ar:+.2f}")
    lines.append("")
    lines.append("-" * 58)
    lines.append("KEY LEVEL ANALYSIS (the new insight):")
    lines.append(f"  AT key level  : {len(at_key):4} trades | {key_wr:.0f}% WR | {key_r:+.2f} avg R")
    lines.append(f"  OFF key level : {len(off_key):4} trades | {off_wr:.0f}% WR | {off_r:+.2f} avg R")
    if key_wr > off_wr:
        lines.append(f"  ✅ Key levels IMPROVE results by {key_wr-off_wr:.0f}% WR")
    else:
        lines.append(f"  ⚠️ Key levels show no WR improvement this week")
    lines.append("")
    lines.append("-" * 58)
    lines.append("BY PAIR:")
    for pair in sorted(set(r['pair'] for r in all_results)):
        pr = [r for r in all_results if r['pair'] == pair]
        pw = len([r for r in pr if r['result'] == 'WIN'])
        pp = sum(r['pips'] for r in pr)
        pr_key = [r for r in pr if r['at_key_level']]
        pk_wr  = len([r for r in pr_key if r['result'] == 'WIN']) / len(pr_key) * 100 if pr_key else 0
        lines.append(f"  {pair:8}: {len(pr):4} trades | {pw/len(pr)*100:.0f}% WR | {pp:+.1f} pips | key:{len(pr_key)} ({pk_wr:.0f}% WR)")
    lines.append("")
    lines.append("-" * 58)
    lines.append("BY STRATEGY:")
    for strat in sorted(set(r['strategy'] for r in all_results)):
        sr = [r for r in all_results if r['strategy'] == strat]
        sw = len([r for r in sr if r['result'] == 'WIN'])
        sp = sum(r['pips'] for r in sr)
        lines.append(f"  {strat:8}: {len(sr):4} trades | {sw/len(sr)*100:.0f}% WR | {sp:+.1f} pips")
    lines.append("")
    lines.append("-" * 58)
    lines.append("ALL SIGNALS (K=at key level):")
    for r in sorted(all_results, key=lambda x: x['time']):
        icon = "WIN " if r['result'] == 'WIN' else "LOSS"
        k    = "K" if r['at_key_level'] else " "
        lines.append(
            f"  {icon} {k} | {r['time']} | {r['pair']:7} | {r['strategy']:5} | "
            f"{r['direction']:4} | sc:{r['score']:2} | {r['pips']:+.1f} pips | {r['r']:+.2f}R"
        )
    lines.append("")
    lines.append("-" * 58)
    if wr >= 50 and ar >= 0.3:
        verdict = "STRONG — positive expectancy with key level integration"
    elif wr >= 40:
        verdict = "MARGINAL — monitor one more week"
    else:
        verdict = "WEAK — do not go live"
    lines.append(f"  VERDICT: {verdict}")
    lines.append("=" * 58)
    return "\n".join(lines)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    log("Full-fidelity backtest v3 (key levels) starting...")

    all_results = []
    for pair_name, symbol in PAIRS.items():
        log(f"Building context for {pair_name}...")
        d1_ctx     = build_d1_context(symbol)
        h4_ctx     = build_h4_context(symbol)
        m30_ctx    = build_m30_context(symbol)
        weekly_lvl = build_weekly_levels(symbol)
        daily_lvl  = build_daily_levels(symbol)

        if d1_ctx is None or h4_ctx is None or m30_ctx is None:
            log(f"{pair_name}: insufficient context, skipping")
            continue

        wk_ok = "✅" if weekly_lvl is not None else "❌"
        dy_ok = "✅" if daily_lvl is not None else "❌"
        log(f"{pair_name}: weekly_levels={wk_ok} daily_levels={dy_ok}")

        for strat_name, cfg in STRATEGIES.items():
            log(f"Running {strat_name} on {pair_name}...")
            results = run_full_strategy(pair_name, symbol, strat_name, cfg,
                                        d1_ctx, h4_ctx, m30_ctx, weekly_lvl, daily_lvl)
            all_results.extend(results)
            at_key = sum(1 for r in results if r['at_key_level'])
            log(f"{pair_name} {strat_name}: {len(results)} signals, {at_key} at key levels")

    report = generate_report(all_results)
    date_str = datetime.utcnow().strftime("%Y%m%d_%H%M")
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
