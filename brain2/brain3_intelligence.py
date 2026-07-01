#!/usr/bin/env python3
"""
WaveEdge Brain 3 - Market Intelligence Hub
Runs every 2 hours via cron.
Writes /root/waveedge/data/market_context.json
Webhook reads this before every trade.

Intelligence layers:
1. NEWS AWARENESS: Block trades 30min before high-impact events
2. ADX REGIME: Detect trending vs ranging per pair
3. PERFORMANCE TRACKING: Raise threshold if pair has been losing
4. D1 BIAS: Confirm direction from daily EMA
5. CONFLUENCE HINT: Detect if multiple timeframes agree
"""

import json
import os
from datetime import datetime, timedelta

CONTEXT_FILE = "/root/waveedge/data/market_context.json"
TRADES_FILE  = "/root/waveedge/data/trades.json"
NEWS_FILE    = "/root/waveedge/data/news_calendar.json"
LOG_FILE     = "/root/waveedge/logs/brain3.log"

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


def log(msg):
    ts = datetime.utcnow().isoformat()
    line = f"{ts} | BRAIN3 | {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def fetch_ohlcv(symbol, period, interval):
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        if df is None or df.empty:
            return None
        try:
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
        except Exception:
            pass
        return df
    except Exception as e:
        log(f"Fetch error {symbol} {interval}: {e}")
        return None


def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


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


def get_pair_adx_regime(symbol):
    """Returns ADX value and regime for the 4H chart."""
    try:
        h1 = fetch_ohlcv(symbol, "30d", "1h")
        if h1 is None or len(h1) < 60:
            return None, "UNKNOWN"
        h4 = h1.resample('4h', label='right', closed='right').agg(
            {'High': 'max', 'Low': 'min', 'Close': 'last'}
        ).dropna()
        if len(h4) < 20:
            return None, "UNKNOWN"
        adx = calc_adx(h4['High'], h4['Low'], h4['Close'])
        adx_val = float(adx.iloc[-1])
        if adx_val >= 25:
            regime = "TRENDING"
        elif adx_val >= 18:
            regime = "WEAK_TREND"
        else:
            regime = "RANGING"
        return round(adx_val, 1), regime
    except Exception as e:
        log(f"ADX error for {symbol}: {e}")
        return None, "UNKNOWN"


def get_d1_bias(symbol):
    """Returns D1 EMA20 bias — BULL, BEAR, or NEUTRAL."""
    try:
        d1 = fetch_ohlcv(symbol, "3mo", "1d")
        if d1 is None or len(d1) < 25:
            return "NEUTRAL"
        ema20 = calc_ema(d1['Close'], 20)
        close = float(d1['Close'].iloc[-1])
        ema   = float(ema20.iloc[-1])
        gap   = abs(close - ema) / ema
        if gap < 0.0003:  # too close to call — within 3 pips effectively
            return "NEUTRAL"
        return "BULL" if close > ema else "BEAR"
    except Exception as e:
        log(f"Bias error for {symbol}: {e}")
        return "NEUTRAL"


def get_recent_performance(pair_name, lookback=15):
    """
    Reads trades.json and returns win rate for the last N trades on this pair.
    Returns win_rate (0-100), win_count, loss_count.
    """
    try:
        if not os.path.exists(TRADES_FILE):
            return 50.0, 0, 0
        with open(TRADES_FILE, "r") as f:
            trades = json.load(f)
        pair_trades = [t for t in trades if t.get("symbol") == pair_name
                       and t.get("status") == "executed"
                       and ("WIN" in str(t.get("result","")).upper()
                            or "LOSS" in str(t.get("result","")).upper()
                            or t.get("status") == "closed")]
        recent = pair_trades[-lookback:] if len(pair_trades) >= lookback else pair_trades
        completed = [t for t in recent
                     if "WIN" in str(t.get("result","")).upper()
                     or "LOSS" in str(t.get("result","")).upper()]
        if not completed:
            return 50.0, 0, 0  # no outcome data yet — neutral, no penalty
        wins   = sum(1 for t in completed if "WIN" in str(t.get("result","")).upper())
        losses = len(completed) - wins
        wr     = wins / len(recent) * 100
        return round(wr, 1), wins, losses
    except Exception as e:
        log(f"Performance read error for {pair_name}: {e}")
        return 50.0, 0, 0


def check_news_block():
    """
    Reads Brain 2's news calendar. Returns True if HIGH impact news
    is within the next 45 minutes, plus a list of upcoming events.
    """
    try:
        if not os.path.exists(NEWS_FILE):
            return False, []
        with open(NEWS_FILE, "r") as f:
            news = json.load(f)
        now = datetime.utcnow()
        window_start = now
        window_end   = now + timedelta(minutes=45)
        upcoming     = []
        block        = False
        for event in news:
            try:
                evt_time = datetime.fromisoformat(event.get("time", "").replace("Z", ""))
                if window_start <= evt_time <= window_end:
                    impact = event.get("impact", "").upper()
                    upcoming.append({
                        "time": event.get("time"),
                        "currency": event.get("currency", ""),
                        "event": event.get("event", ""),
                        "impact": impact
                    })
                    if impact in ("HIGH", "CRITICAL"):
                        block = True
            except Exception:
                continue
        return block, upcoming
    except Exception as e:
        log(f"News check error: {e}")
        return False, []


def determine_pair_threshold_adjustment(wr, adx, regime):
    """
    Returns the score adjustment to apply to min_score.
    Positive = raise threshold (harder to trade).
    Negative = lower threshold (easier to trade).
    """
    adjustment = 0
    reason_parts = []

    # Recent performance penalty
    if wr < 30:
        adjustment += 2
        reason_parts.append(f"poor recent WR ({wr:.0f}%)")
    elif wr < 40:
        adjustment += 1
        reason_parts.append(f"below-avg WR ({wr:.0f}%)")

    # Market regime penalty
    if regime == "RANGING":
        adjustment += 2
        reason_parts.append("market RANGING")
    elif regime == "WEAK_TREND":
        adjustment += 1
        reason_parts.append("weak trend")

    reason = ", ".join(reason_parts) if reason_parts else "conditions normal"
    return adjustment, reason


def build_context():
    """
    Main function — builds the full market context object and writes it.
    """
    log("Building market context...")
    now = datetime.utcnow()

    # Check news
    news_block, upcoming_news = check_news_block()
    if news_block:
        log(f"⚠️  NEWS BLOCK ACTIVE — {len(upcoming_news)} high-impact events within 45min")
    else:
        log(f"News clear — {len(upcoming_news)} events in next 45min (none high-impact)")

    pair_context = {}
    for pair_name, symbol in PAIRS.items():
        log(f"Analyzing {pair_name}...")
        adx_val, regime = get_pair_adx_regime(symbol)
        bias             = get_d1_bias(symbol)
        wr, wins, losses = get_recent_performance(pair_name)
        score_adj, reason = determine_pair_threshold_adjustment(wr, adx_val, regime)

        pair_block = False
        block_reason = ""
        if regime == "RANGING" and wr < 30:
            pair_block   = True
            block_reason = f"ranging market + poor performance ({wr:.0f}% WR)"

        pair_context[pair_name] = {
            "adx_4h": adx_val,
            "regime": regime,
            "bias_d1": bias,
            "recent_wr": wr,
            "recent_wins": wins,
            "recent_losses": losses,
            "score_adjustment": score_adj,
            "reason": reason,
            "block": pair_block,
            "block_reason": block_reason
        }

        status = "BLOCKED" if pair_block else f"adj+{score_adj}" if score_adj > 0 else "CLEAR"
        log(f"{pair_name}: ADX={adx_val} {regime} | Bias={bias} | WR={wr:.0f}% | {status}")

    context = {
        "updated": now.isoformat(),
        "updated_human": now.strftime("%Y-%m-%d %H:%M UTC"),
        "news_block_active": news_block,
        "news_events_upcoming": upcoming_news,
        "pair_context": pair_context,
        "system_mode": "NEWS_BLOCK" if news_block else "NORMAL",
        "block_all": news_block
    }

    os.makedirs(os.path.dirname(CONTEXT_FILE), exist_ok=True)
    with open(CONTEXT_FILE, "w") as f:
        json.dump(context, f, indent=2)

    log(f"Context written to {CONTEXT_FILE}")
    return context


def print_summary(context):
    print("\n" + "=" * 52)
    print("  WAVEEDGE BRAIN 3 — MARKET INTELLIGENCE")
    print(f"  {context['updated_human']}")
    print("=" * 52)
    print(f"  System Mode  : {context['system_mode']}")
    print(f"  News Block   : {'YES ⚠️' if context['news_block_active'] else 'No'}")
    if context['news_events_upcoming']:
        for evt in context['news_events_upcoming']:
            print(f"  ⚡ {evt['impact']:6} | {evt['currency']} | {evt['event'][:35]}")
    print("-" * 52)
    print("  PAIR INTELLIGENCE:")
    for pair, ctx in context['pair_context'].items():
        status = "🔴 BLOCKED" if ctx['block'] else f"🟡 +{ctx['score_adjustment']}" if ctx['score_adjustment'] > 0 else "🟢 CLEAR"
        print(f"  {pair:8} | {ctx['regime']:12} | D1:{ctx['bias_d1']:4} | "
              f"WR:{ctx['recent_wr']:4.0f}% | {status}")
    print("=" * 52)


if __name__ == "__main__":
    context = build_context()
    print_summary(context)
