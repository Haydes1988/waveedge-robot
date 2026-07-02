#!/usr/bin/env python3
"""
WaveEdge Position Manager v1 — SHADOW MODE
Runs every 5 minutes via cron.

WHAT IT DOES:
  Watches every OPEN position and checks for reversal signatures:
    1. WaveTrend crossing AGAINST the position (5-minute chart)
    2. Opposite reversal candle (engulfing / pin bar) on last closed 5m bar
    3. Price within 10 pips of Brain 3's reversal/defense point
  When 2 of 3 fire -> logs an EXIT SIGNAL.

SHADOW MODE (current state):
  It NEVER closes a trade. It only records "would have exited here at
  +X pips" to /root/waveedge/data/exit_signals.json and notifications.json.
  After 1-2 weeks we compare shadow exits vs actual SL/TP outcomes.
  If early exits genuinely keep more profit, set SHADOW_MODE = False.

  DO NOT flip SHADOW_MODE to False without reviewing the data with Claude.
"""

import json
import os
from datetime import datetime, timezone

# ─── THE SWITCH ───────────────────────────────────────────────────────────────
SHADOW_MODE = True   # True = watch and log only. False = actually close trades.
# ──────────────────────────────────────────────────────────────────────────────

CONTEXT_FILE  = "/root/waveedge/data/market_context.json"
EXIT_FILE     = "/root/waveedge/data/exit_signals.json"
NOTIFY_FILE   = "/root/waveedge/data/notifications.json"
SCHEMA_FILE   = "/root/waveedge/data/position_schema_sample.json"
LOG_FILE      = "/root/waveedge/logs/position_manager.log"

GCAPI_USER     = "demo-ahedgin"
GCAPI_PASS     = "Hedgin456$"
GCAPI_KEY      = "A.Hedgin"
TRADING_ACC_ID = 407829813

REVERSAL_POINT_PIPS = 10   # "near defense level" threshold
MIN_SIGNALS_TO_FIRE = 2    # how many of the 3 checks must agree

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
    line = f"{ts} | POSMGR | {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def save_alert(alert_type, title, body):
    try:
        alerts = []
        if os.path.exists(NOTIFY_FILE):
            with open(NOTIFY_FILE, "r") as f:
                try:
                    alerts = json.load(f)
                except Exception:
                    alerts = []
        alerts.append({
            "time": utcnow().isoformat(),
            "time_human": utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "type": alert_type,
            "title": title,
            "body": body
        })
        alerts = alerts[-50:]
        with open(NOTIFY_FILE, "w") as f:
            json.dump(alerts, f, indent=2)
    except Exception as e:
        log(f"Alert save error: {e}")


def save_exit_signal(record):
    try:
        signals = []
        if os.path.exists(EXIT_FILE):
            with open(EXIT_FILE, "r") as f:
                try:
                    signals = json.load(f)
                except Exception:
                    signals = []
        signals.append(record)
        signals = signals[-200:]
        with open(EXIT_FILE, "w") as f:
            json.dump(signals, f, indent=2)
    except Exception as e:
        log(f"Exit signal save error: {e}")


# ─── GCAPI POSITIONS ─────────────────────────────────────────────────────────

def get_open_positions():
    try:
        from gcapi import GCapiClient
        api = GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)
        pos = api.list_open_positions(trading_acc_id=TRADING_ACC_ID)
        positions = pos.get("OpenPositions", []) or []
        # Save one raw sample so we can learn GCAPI's exact field names
        if positions and not os.path.exists(SCHEMA_FILE):
            with open(SCHEMA_FILE, "w") as f:
                json.dump(positions[0], f, indent=2)
            log(f"Saved raw position schema sample to {SCHEMA_FILE}")
        return positions
    except Exception as e:
        log(f"GCAPI positions error: {e}")
        return []


def extract_field(p, names, default=None):
    """Defensive field access — GCAPI field names vary by endpoint version."""
    for n in names:
        if n in p and p[n] is not None:
            return p[n]
    return default


# ─── REVERSAL CHECKS ──────────────────────────────────────────────────────────

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def get_5m_data(symbol_yf):
    try:
        import yfinance as yf
        df = yf.Ticker(symbol_yf).history(period="2d", interval="5m")
        if df is None or len(df) < 30:
            return None
        try:
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
        except Exception:
            pass
        return df
    except Exception as e:
        log(f"5m data error {symbol_yf}: {e}")
        return None


def check_wavetrend_cross(df, direction):
    """True if WaveTrend crossed AGAINST the position on the last closed bar."""
    try:
        hlc3 = (df["High"] + df["Low"] + df["Close"]) / 3
        ap  = calc_ema(hlc3, 10)
        d   = calc_ema((hlc3 - ap).abs(), 10)
        ci  = (hlc3 - ap) / (0.015 * d + 1e-10)
        wt1 = calc_ema(ci, 21)
        wt2 = wt1.rolling(4).mean()
        # last CLOSED bar = -2 (bar -1 may still be forming)
        cross_down = wt1.iloc[-2] < wt2.iloc[-2] and wt1.iloc[-3] >= wt2.iloc[-3]
        cross_up   = wt1.iloc[-2] > wt2.iloc[-2] and wt1.iloc[-3] <= wt2.iloc[-3]
        if direction == "buy" and cross_down:
            return True
        if direction == "sell" and cross_up:
            return True
        return False
    except Exception:
        return False


def check_opposite_candle(df, direction):
    """True if last closed 5m bar is a reversal candle AGAINST the position."""
    try:
        c  = df.iloc[-2]   # last closed bar
        c1 = df.iloc[-3]
        body    = abs(c["Close"] - c["Open"])
        body1   = abs(c1["Close"] - c1["Open"])
        rng     = c["High"] - c["Low"]
        if rng <= 0:
            return False
        upper = c["High"] - max(c["Close"], c["Open"])
        lower = min(c["Close"], c["Open"]) - c["Low"]

        bear_engulf = (c["Close"] < c["Open"] and c["Close"] < c1["Open"]
                       and c["Open"] > c1["Close"] and body > body1 * 0.8)
        bull_engulf = (c["Close"] > c["Open"] and c["Close"] > c1["Open"]
                       and c["Open"] < c1["Close"] and body > body1 * 0.8)
        bear_pin = (upper >= body * 2.0 and c["Close"] <= c["High"] - rng * 0.6)
        bull_pin = (lower >= body * 2.0 and c["Close"] >= c["Low"] + rng * 0.6)

        if direction == "buy" and (bear_engulf or bear_pin):
            return True
        if direction == "sell" and (bull_engulf or bull_pin):
            return True
        return False
    except Exception:
        return False


def check_near_reversal_point(symbol, price):
    """True if price is within REVERSAL_POINT_PIPS of Brain 3's defense level."""
    try:
        if not os.path.exists(CONTEXT_FILE):
            return False, None
        with open(CONTEXT_FILE, "r") as f:
            ctx = json.load(f)
        pair_ctx = ctx.get("pair_context", {}).get(symbol, {})
        lm   = pair_ctx.get("liquidity_map") or {}
        proj = lm.get("projection") or {}
        rp   = proj.get("reversal_point") or {}
        lvl  = rp.get("level")
        if lvl is None:
            return False, None
        pip = PIP_SIZES.get(symbol, 0.0001)
        dist_pips = abs(price - float(lvl)) / pip
        if dist_pips <= REVERSAL_POINT_PIPS:
            return True, f"{rp.get('name')}@{lvl} ({dist_pips:.1f}p away)"
        return False, None
    except Exception:
        return False, None


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    mode = "SHADOW (log only)" if SHADOW_MODE else "LIVE (will close trades)"
    positions = get_open_positions()

    if not positions:
        log(f"Mode: {mode} | No open positions — nothing to watch")
        return

    log(f"Mode: {mode} | Watching {len(positions)} open position(s)")

    for p in positions:
        market_id = extract_field(p, ["MarketId", "marketId"])
        symbol    = MARKET_MAP.get(market_id)
        if not symbol:
            log(f"Unknown MarketId {market_id} — skipping (see {SCHEMA_FILE})")
            continue

        direction = str(extract_field(p, ["Direction", "TradeDirection", "direction"], "")).lower()
        entry     = extract_field(p, ["Price", "OpenPrice", "price", "EntryPrice"])
        qty       = extract_field(p, ["Quantity", "quantity", "Size"])

        yf_sym = YF_SYMBOLS.get(symbol)
        df = get_5m_data(yf_sym) if yf_sym else None
        if df is None:
            log(f"{symbol}: no 5m data — skipping this run")
            continue

        current = float(df["Close"].iloc[-1])
        pip     = PIP_SIZES.get(symbol, 0.0001)

        pl_pips = None
        if entry is not None and direction in ("buy", "sell"):
            try:
                e = float(entry)
                pl_pips = round(((current - e) if direction == "buy" else (e - current)) / pip, 1)
            except Exception:
                pl_pips = None

        # The three reversal checks
        sig_wt      = check_wavetrend_cross(df, direction)
        sig_candle  = check_opposite_candle(df, direction)
        sig_level, level_txt = check_near_reversal_point(symbol, current)

        fired = []
        if sig_wt:     fired.append("WaveTrend cross against position")
        if sig_candle: fired.append("Opposite reversal candle (5m)")
        if sig_level:  fired.append(f"At Brain3 defense level: {level_txt}")

        pl_txt = f"{pl_pips:+.1f} pips" if pl_pips is not None else "P/L unknown (entry not readable)"
        log(f"{symbol} {direction.upper()} | now:{current} | {pl_txt} | signals: {len(fired)}/3 {fired if fired else ''}")

        if len(fired) >= MIN_SIGNALS_TO_FIRE:
            record = {
                "time": utcnow().isoformat(),
                "time_human": utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                "symbol": symbol,
                "direction": direction,
                "entry": entry,
                "current_price": current,
                "pl_pips_at_signal": pl_pips,
                "quantity": qty,
                "signals": fired,
                "mode": "SHADOW" if SHADOW_MODE else "LIVE",
                "action_taken": "logged only" if SHADOW_MODE else "position closed"
            }
            save_exit_signal(record)
            save_alert(
                "EXIT_SIGNAL_SHADOW" if SHADOW_MODE else "EXIT_EXECUTED",
                f"{symbol} {direction.upper()} reversal detected — "
                f"{'would exit' if SHADOW_MODE else 'CLOSED'} at {pl_txt}",
                f"Position: {symbol} {direction.upper()}\n"
                f"Entry:    {entry}\n"
                f"Now:      {current}\n"
                f"P/L:      {pl_txt}\n"
                f"Signals ({len(fired)}/3):\n" +
                "\n".join(f"  - {s}" for s in fired) +
                f"\n\nMode: {'SHADOW — trade NOT touched' if SHADOW_MODE else 'LIVE — trade closed'}"
            )
            log(f"EXIT SIGNAL {'(shadow)' if SHADOW_MODE else '(LIVE)'}: {symbol} at {pl_txt}")

            if not SHADOW_MODE:
                # Live close path — intentionally NOT implemented yet.
                # Requires validated shadow data + GCAPI close-position schema
                # from SCHEMA_FILE before this block gets written.
                log("LIVE close requested but close logic not implemented — staying safe")


if __name__ == "__main__":
    main()
