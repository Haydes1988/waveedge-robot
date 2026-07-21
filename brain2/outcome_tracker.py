#!/usr/bin/env python3
"""Resolve each WaveEdge race leg and update realized P/L exactly once."""

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone

HOME = os.getenv("WAVEEDGE_HOME", "/root/waveedge")
TRADES_FILE = os.getenv("WAVEEDGE_TRADES_FILE", f"{HOME}/data/trades.json")
DAILY_PNL_FILE = os.getenv("WAVEEDGE_DAILY_PNL_FILE", f"{HOME}/data/daily_pnl.json")
SNAPSHOT_FILE = os.getenv("WAVEEDGE_POSITION_SNAPSHOT", f"{HOME}/data/last_open_positions.json")
LOG_FILE = os.getenv("WAVEEDGE_OUTCOME_LOG", f"{HOME}/logs/outcome_tracker.log")

GCAPI_USER = os.getenv("WAVEEDGE_GCAPI_USER", "")
GCAPI_PASS = os.getenv("WAVEEDGE_GCAPI_PASS", "")
GCAPI_KEY = os.getenv("WAVEEDGE_GCAPI_KEY", "")
TRADING_ACC_ID = int(os.getenv("WAVEEDGE_TRADING_ACC_ID", "407829813"))

MARKET_MAP = {
    401484347: "EURUSD", 401484392: "GBPUSD", 401484414: "USDJPY",
    401484317: "AUDUSD", 401484406: "USDCAD", 401484407: "USDCHF",
    401484402: "NZDUSD",
}
YF_SYMBOLS = {pair: f"{pair}=X" for pair in MARKET_MAP.values()}
PIP_SIZES = {pair: (0.01 if pair == "USDJPY" else 0.0001) for pair in MARKET_MAP.values()}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(message):
    line = f"{utcnow().isoformat()} | OUTCOME | {message}"
    print(line)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


def atomic_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".waveedge-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def extract_field(record, names, default=None):
    for name in names:
        if record.get(name) is not None:
            return record[name]
    return default


def make_api():
    if not (GCAPI_USER and GCAPI_PASS and GCAPI_KEY):
        raise RuntimeError("Broker credentials are not configured in environment variables")
    from gcapi import GCapiClient
    return GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)


def get_open_positions():
    try:
        response = make_api().list_open_positions(trading_acc_id=TRADING_ACC_ID)
        return response.get("OpenPositions", []) or []
    except Exception as exc:
        log(f"Broker position check failed: {exc}")
        return None


def position_identity(position):
    identifier = extract_field(position, ["OrderId", "PositionId", "TradeId", "orderId", "positionId"])
    if identifier is not None:
        return f"id:{identifier}"
    market = extract_field(position, ["MarketId", "marketId"], "?")
    direction = str(extract_field(position, ["Direction", "TradeDirection", "direction"], "")).lower()
    entry = extract_field(position, ["Price", "OpenPrice", "EntryPrice", "price"], "?")
    quantity = extract_field(position, ["Quantity", "Size", "quantity"], "?")
    return f"fallback:{market}|{direction}|{entry}|{quantity}"


def snapshot_positions(positions):
    snap = {}
    for position in positions:
        market_id = extract_field(position, ["MarketId", "marketId"])
        symbol = MARKET_MAP.get(market_id)
        if not symbol:
            continue
        snap[position_identity(position)] = {
            "symbol": symbol,
            "direction": str(extract_field(
                position, ["Direction", "TradeDirection", "direction"], ""
            )).lower(),
            "broker_order_id": extract_field(
                position, ["OrderId", "PositionId", "TradeId", "orderId", "positionId"]
            ),
        }
    return snap


def fetch_prices(symbol_yf, signal_time):
    try:
        import pandas as pd
        import yfinance as yf
        start = pd.Timestamp(signal_time)
        age = utcnow() - start.to_pydatetime().replace(tzinfo=None)
        period = "5d" if age <= timedelta(days=5) else "30d"
        interval = "5m" if period == "5d" else "15m"
        frame = yf.Ticker(symbol_yf).history(period=period, interval=interval)
        if frame is None or frame.empty:
            return None
        if frame.index.tz is not None:
            frame.index = frame.index.tz_convert("UTC").tz_localize(None)
        return frame
    except Exception as exc:
        log(f"Price fetch failed for {symbol_yf}: {exc}")
        return None


def check_touch(frame, signal_time, entry, sl, tp, direction, pip_size):
    """Conservative OHLC resolution: when both touch in one bar, SL wins."""
    import pandas as pd
    start = pd.Timestamp(signal_time)
    if start.tzinfo is not None:
        start = start.tz_convert("UTC").tz_localize(None)
    window = frame[frame.index >= start]
    risk = abs(entry - sl)
    if window.empty or risk <= 0:
        return None
    for when, bar in window.iterrows():
        if direction == "buy":
            if float(bar["Low"]) <= sl:
                return {"result": "LOSS", "pips": round(-risk / pip_size, 1),
                        "r": -1.0, "resolved_at": str(when)}
            if float(bar["High"]) >= tp:
                reward = tp - entry
                return {"result": "WIN", "pips": round(reward / pip_size, 1),
                        "r": round(reward / risk, 3), "resolved_at": str(when)}
        else:
            if float(bar["High"]) >= sl:
                return {"result": "LOSS", "pips": round(-risk / pip_size, 1),
                        "r": -1.0, "resolved_at": str(when)}
            if float(bar["Low"]) <= tp:
                reward = entry - tp
                return {"result": "WIN", "pips": round(reward / pip_size, 1),
                        "r": round(reward / risk, 3), "resolved_at": str(when)}
    return None


def find_trade(trades, closed):
    order_id = closed.get("broker_order_id")
    candidates = [trade for trade in trades
                  if trade.get("status") == "executed" and trade.get("result") is None]
    if order_id is not None:
        exact = [trade for trade in candidates
                 if str(trade.get("broker_order_id")) == str(order_id)]
        if exact:
            return min(exact, key=lambda trade: trade.get("timestamp", ""))
    fallback = [trade for trade in candidates
                if trade.get("symbol") == closed.get("symbol") and
                trade.get("action") == closed.get("direction")]
    return min(fallback, key=lambda trade: trade.get("timestamp", "")) if fallback else None


def apply_realized_pnl(trade, outcome):
    if trade.get("pnl_recorded"):
        return
    risk = float(trade.get("risk_dollars") or 0.0)
    pnl_delta = round(risk * float(outcome["r"]), 2)
    resolved_day = utcnow().strftime("%Y-%m-%d")
    pnl = load_json(DAILY_PNL_FILE, {})
    pnl[resolved_day] = round(float(pnl.get(resolved_day, 0.0)) + pnl_delta, 2)
    atomic_json(DAILY_PNL_FILE, pnl)
    trade["realized_pnl"] = pnl_delta
    trade["pnl_recorded"] = True


def resolve_closed_trade(trade):
    required = (trade.get("entry_price"), trade.get("sl"), trade.get("tp1", trade.get("tp")))
    if any(value is None for value in required):
        return None
    symbol = trade["symbol"]
    frame = fetch_prices(YF_SYMBOLS[symbol], trade["timestamp"])
    if frame is None:
        return None
    return check_touch(frame, trade["timestamp"], float(required[0]), float(required[1]),
                       float(required[2]), trade["action"], PIP_SIZES[symbol])


def main():
    current_positions = get_open_positions()
    if current_positions is None:
        log("Fail-closed: snapshot left untouched; retry next run")
        return {"status": "broker_unavailable", "resolved": 0}

    current = snapshot_positions(current_positions)
    previous_payload = load_json(SNAPSHOT_FILE, {"positions": {}})
    previous = previous_payload.get("positions", {}) if isinstance(previous_payload, dict) else {}
    closed = [details for key, details in previous.items() if key not in current]

    # Save only after a successful broker read. An outage can never look like
    # every position suddenly closed.
    atomic_json(SNAPSHOT_FILE, {"positions": current, "updated": utcnow().isoformat()})
    if not closed:
        log(f"No closures; {len(current)} position(s) remain open")
        return {"status": "ok", "resolved": 0, "open": len(current)}

    import fcntl
    os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
    resolved_count = 0
    with open(TRADES_FILE + ".lock", "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        trades = load_json(TRADES_FILE, [])
        for details in closed:
            trade = find_trade(trades, details)
            if not trade:
                log(f"Unmatched closure: {details}")
                continue
            outcome = resolve_closed_trade(trade)
            if outcome is None:
                # A manual/early close cannot be inferred from candles. Preserve
                # UNKNOWN without pretending it was an SL or TP.
                trade.update(result="UNKNOWN", outcome_checked=True,
                             outcome_source="confirmed_closed_price_unavailable",
                             resolved_at=utcnow().isoformat())
                log(f"{trade['symbol']} P{trade.get('race_position')} closed; result UNKNOWN")
            else:
                trade.update(result=outcome["result"], outcome_pips=outcome["pips"],
                             outcome_r=outcome["r"], outcome_checked=True,
                             outcome_source="confirmed_closed_ohlc", resolved_at=outcome["resolved_at"])
                apply_realized_pnl(trade, outcome)
                log(f"{trade['symbol']} P{trade.get('race_position')}: {outcome['result']} "
                    f"{outcome['r']:+.2f}R")
            resolved_count += 1
        atomic_json(TRADES_FILE, trades)
    return {"status": "ok", "resolved": resolved_count, "open": len(current)}


if __name__ == "__main__":
    main()
