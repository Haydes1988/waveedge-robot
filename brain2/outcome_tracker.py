#!/usr/bin/env python3
"""
WaveEdge Outcome Tracker v1
Runs every 5 minutes via cron (same cadence as position_manager).

PROBLEM THIS SOLVES:
  trades.json logs when a trade OPENS. Nothing ever recorded whether it
  WON or LOST. Brain 3's win-rate math has been permanently neutral (50%)
  as a result, and daily_pnl.json only ever subtracts committed risk at
  open — a WIN never credits anything back, so the daily loss limit was
  really tracking "risk committed today," not "money actually lost today."

HOW IT WORKS (uses only APIs already proven working — no guessing at
GCAPI's trade-history endpoint):
  1. Poll open positions (same call webhook.py and position_manager.py
     already use successfully).
  2. Compare to the last poll's snapshot. Anything present before but
     missing now = a position that CLOSED in the last 5 minutes.
  3. Match that closure to the OLDEST unresolved ("result": None) entry
     in trades.json for the same symbol + direction (FIFO — reasonable
     since MAX_POSITIONS_PER_SYMBOL=2 keeps ambiguity low).
  4. Re-fetch 5-minute candles spanning [signal time -> now] and check
     whether price touched the recorded SL or TP1 first — same logic
     already validated in the backtest engine.
  5. Patch that trades.json entry in place with the real result + pips.
  6. If it was a WIN, credit the originally-deducted risk back into
     daily_pnl.json — so the daily loss limit reflects REALIZED loss,
     not cumulative risk exposure.

LIMITATION, STATED PLAINLY: if a position closes and re-opens on the
same symbol+direction within one 5-minute poll window, this can't see
the gap and may mismatch. Rare given Scalp's typical hold time, but
real. Revisit if trades.json shows odd result assignments.
"""

import json
import os
from datetime import datetime, timedelta, timezone

TRADES_FILE     = "/root/waveedge/data/trades.json"
DAILY_PNL_FILE  = "/root/waveedge/data/daily_pnl.json"
SNAPSHOT_FILE   = "/root/waveedge/data/last_open_positions.json"
LOG_FILE        = "/root/waveedge/logs/outcome_tracker.log"

GCAPI_USER     = "demo-ahedgin"
GCAPI_PASS     = "Hedgin456$"
GCAPI_KEY      = "A.Hedgin"
TRADING_ACC_ID = 407829813

MARKET_MAP = {
    401484347: "EURUSD", 401484392: "GBPUSD", 401484338: "EURJPY",
    401484385: "GBPJPY", 401484414: "USDJPY", 401484317: "AUDUSD",
    401484406: "USDCAD", 401484335: "EURGBP", 401484407: "USDCHF",
    401484402: "NZDUSD"
}
YF_SYMBOLS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X", "EURJPY": "EURJPY=X", "GBPJPY": "GBPJPY=X",
    "EURGBP": "EURGBP=X"
}
PIP_SIZES = {
    "EURUSD": 0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01,
    "AUDUSD": 0.0001, "USDCAD": 0.0001, "USDCHF": 0.0001,
    "NZDUSD": 0.0001, "EURJPY": 0.01, "GBPJPY": 0.01, "EURGBP": 0.0001
}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(msg):
    ts = utcnow().isoformat()
    line = f"{ts} | OUTCOME | {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def get_open_positions():
    try:
        from gcapi import GCapiClient
        api = GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)
        pos = api.list_open_positions(trading_acc_id=TRADING_ACC_ID)
        return pos.get("OpenPositions", []) or []
    except Exception as e:
        log(f"GCAPI positions error: {e}")
        return None  # None = couldn't check; distinct from [] = confirmed empty


def extract_field(p, names, default=None):
    for n in names:
        if n in p and p[n] is not None:
            return p[n]
    return default


def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def fetch_5m(symbol_yf):
    try:
        import yfinance as yf
        df = yf.Ticker(symbol_yf).history(period="2d", interval="5m")
        if df is None or df.empty:
            return None
        try:
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
        except Exception:
            pass
        return df
    except Exception as e:
        log(f"5m fetch error {symbol_yf}: {e}")
        return None


def check_touch(df, signal_time, sl, tp1, direction, pip_size):
    """Did price touch SL or TP1 first, between signal_time and now?"""
    try:
        import pandas as pd
        start = pd.Timestamp(signal_time)
        window = df[df.index >= start]
        if window.empty:
            return None
        for _, c in window.iterrows():
            if direction == "buy":
                if float(c["Low"]) <= sl:
                    return {"result": "LOSS", "pips": round((sl - float(c["Open"])) / pip_size, 1)}
                if float(c["High"]) >= tp1:
                    return {"result": "WIN", "pips": round((tp1 - float(c["Open"])) / pip_size, 1)}
            else:
                if float(c["High"]) >= sl:
                    return {"result": "LOSS", "pips": round((float(c["Open"]) - sl) / pip_size, 1)}
                if float(c["Low"]) <= tp1:
                    return {"result": "WIN", "pips": round((float(c["Open"]) - tp1) / pip_size, 1)}
        return None  # still open / hasn't resolved within available data
    except Exception as e:
        log(f"Touch-check error: {e}")
        return None


def find_unresolved_trade(trades, symbol, direction):
    """Oldest unresolved (result is None) executed trade for this symbol+direction."""
    candidates = [
        t for t in trades
        if t.get("symbol") == symbol
        and t.get("action") == direction
        and t.get("status") == "executed"
        and t.get("result") is None
        and t.get("entry_price") is not None
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda t: t["timestamp"])
    return candidates[0]


def credit_win_back(risk_dollars):
    if not risk_dollars:
        return
    today = utcnow().strftime("%Y-%m-%d")
    pnl = load_json(DAILY_PNL_FILE, {})
    pnl[today] = pnl.get(today, 0) + risk_dollars  # undo the at-open deduction
    save_json(DAILY_PNL_FILE, pnl)
    log(f"Daily P/L credited back ${risk_dollars:.2f} for a WIN — "
        f"today's realized loss now ${pnl[today]:.2f}")


def main():
    current = get_open_positions()
    if current is None:
        log("Could not reach GCAPI this run — skipping (will retry in 5 min)")
        return

    current_keys = set()
    for p in current:
        mid = extract_field(p, ["MarketId", "marketId"])
        direction = str(extract_field(p, ["Direction", "TradeDirection", "direction"], "")).lower()
        symbol = MARKET_MAP.get(mid)
        if symbol:
            current_keys.add((symbol, direction))

    previous = load_json(SNAPSHOT_FILE, {"keys": []})
    previous_keys = set(tuple(k) for k in previous.get("keys", []))

    closed = previous_keys - current_keys
    save_json(SNAPSHOT_FILE, {"keys": [list(k) for k in current_keys],
                              "updated": utcnow().isoformat()})

    if not closed:
        log(f"No closures this run — {len(current_keys)} position(s) still open")
        return

    trades = load_json(TRADES_FILE, [])
    changed = False

    for symbol, direction in closed:
        trade = find_unresolved_trade(trades, symbol, direction)
        if not trade:
            log(f"{symbol} {direction}: closed but no matching unresolved signal found — skipping")
            continue

        yf_sym = YF_SYMBOLS.get(symbol)
        pip = PIP_SIZES.get(symbol, 0.0001)
        df = fetch_5m(yf_sym) if yf_sym else None
        if df is None:
            log(f"{symbol} {direction}: closed, but no price data to determine outcome")
            continue

        outcome = check_touch(df, trade["timestamp"], float(trade["sl"]),
                              float(trade["tp1"]), direction, pip)
        if not outcome:
            log(f"{symbol} {direction}: closed but SL/TP touch not found in available candles — "
                f"marking result UNKNOWN rather than guessing")
            trade["result"] = "UNKNOWN"
            trade["outcome_checked"] = True
            changed = True
            continue

        trade["result"] = outcome["result"]
        trade["outcome_pips"] = outcome["pips"]
        trade["outcome_checked"] = True
        changed = True
        log(f"{symbol} {direction}: RESOLVED as {outcome['result']} ({outcome['pips']:+.1f} pips)")

        if outcome["result"] == "WIN":
            credit_win_back(trade.get("risk_dollars"))

    if changed:
        save_json(TRADES_FILE, trades)
        log("trades.json updated with resolved outcomes")


if __name__ == "__main__":
    main()
