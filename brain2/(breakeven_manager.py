#!/usr/bin/env python3
"""
WaveEdge Breakeven Manager
Runs every 2 minutes via cron — more frequent than position_manager's 5min,
since this needs to react quickly once a leg wins.

PURPOSE: implements the actual design — three legs open with one shared
stop. The instant ANY leg hits its target, every other still-open leg's
stop moves to entry. This has never existed anywhere in this codebase
before tonight; this is the real fix, not the backtest simulation of it.

DEPENDS ON: webhook.py must write race details to active_races.json when
place_race() executes (see the small addition needed there, given below).

HONEST GAP: the exact GCAPI method to modify an existing position's stop
loss has never been confirmed. This tries several plausible candidates and
logs precisely which one (if any) succeeds — verify on the demo before
trusting this with anything else.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from env_loader import load_env

load_env()

ACTIVE_RACES_FILE = "/root/waveedge/data/active_races.json"
LOG_FILE          = "/root/waveedge/logs/breakeven_manager.log"

GCAPI_USER     = os.environ.get("GCAPI_USER", "")
GCAPI_PASS     = os.environ.get("GCAPI_PASS", "")
GCAPI_KEY      = os.environ.get("GCAPI_KEY", "")
TRADING_ACC_ID = int(os.environ.get("TRADING_ACC_ID", "0"))

YF_SYMBOLS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X"
}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(msg):
    ts = utcnow().isoformat()
    line = f"{ts} | BREAKEVEN | {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_api():
    from gcapi import GCapiClient
    return GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)


def get_open_position_ids(api):
    """Returns set of currently open broker order/position IDs."""
    try:
        pos = api.list_open_positions(trading_acc_id=TRADING_ACC_ID)
        ids = set()
        for p in pos.get("OpenPositions", []):
            for key in ("OrderId", "PositionId", "TradeId"):
                if p.get(key) is not None:
                    ids.add(str(p[key]))
        return ids
    except Exception as e:
        log(f"GCAPI position fetch failed: {e}")
        return None


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


def determine_close_reason(race, leg):
    """Did this closed leg most likely hit its TP (WIN) or the race SL (LOSS)?
    Reuses the proven price-history-check pattern from outcome_tracker.py."""
    yf_sym = YF_SYMBOLS.get(race["symbol"])
    df = fetch_5m(yf_sym) if yf_sym else None
    if df is None:
        return "UNKNOWN"
    try:
        import pandas as pd
        start = pd.Timestamp(race["opened_at"])
        window = df[df.index >= start]
        direction = race["action"]
        for _, c in window.iterrows():
            if direction == "buy":
                if float(c["Low"]) <= race["sl"]:
                    return "LOSS"
                if float(c["High"]) >= leg["tp"]:
                    return "WIN"
            else:
                if float(c["High"]) >= race["sl"]:
                    return "LOSS"
                if float(c["Low"]) <= leg["tp"]:
                    return "WIN"
    except Exception as e:
        log(f"Close-reason check error: {e}")
    return "UNKNOWN"


def attempt_modify_stop(api, leg, new_stop):
    """Try the plausible GCAPI methods for amending an open position's stop.
    Logs exactly which (if any) succeeds — this is the one genuinely
    unverified piece of this whole build."""
    order_id = leg.get("broker_order_id")
    candidates = [
        ("update_stop_loss", lambda: api.update_stop_loss(
            trading_acc_id=TRADING_ACC_ID, order_id=order_id, stop_loss=new_stop)),
        ("modify_position", lambda: api.modify_position(
            trading_acc_id=TRADING_ACC_ID, order_id=order_id, stop_loss=new_stop)),
        ("amend_order", lambda: api.amend_order(
            trading_acc_id=TRADING_ACC_ID, order_id=order_id, stop_loss=new_stop)),
        ("trade_order(amend)", lambda: api.trade_order(
            trading_acc_id=TRADING_ACC_ID, order_id=order_id, stop_loss=new_stop, amend=True)),
    ]
    for name, fn in candidates:
        if not hasattr(api, name.split("(")[0]):
            continue
        try:
            result = fn()
            log(f"MODIFY ATTEMPT via {name}: SUCCESS — leg {order_id} stop -> {new_stop} | result: {result}")
            return True, name
        except Exception as e:
            log(f"MODIFY ATTEMPT via {name}: FAILED — {e}")
    log(f"ALL MODIFY ATTEMPTS FAILED for leg {order_id} — "
        f"GCAPI's real method name is still unconfirmed. "
        f"MANUAL ACTION NEEDED: check this position in the platform directly.")
    return False, None


def main():
    races = load_json(ACTIVE_RACES_FILE, [])
    if not races:
        log("No active races being tracked")
        return

    api = None
    open_ids = None
    changed = False

    for race in races:
        if race.get("all_closed"):
            continue

        open_legs = [leg for leg in race["legs"] if leg["status"] == "OPEN"]
        if not open_legs:
            race["all_closed"] = True
            changed = True
            continue

        if open_ids is None:
            api = get_api()
            open_ids = get_open_position_ids(api)
            if open_ids is None:
                log("Could not reach GCAPI this run — skipping")
                return

        newly_closed = [leg for leg in open_legs
                        if str(leg.get("broker_order_id")) not in open_ids]

        if not newly_closed:
            continue

        for leg in newly_closed:
            reason = determine_close_reason(race, leg)
            leg["status"] = reason if reason != "UNKNOWN" else "CLOSED_UNKNOWN"
            log(f"{race['symbol']} {race['action']} leg {leg['position']}: "
                f"closed, determined as {leg['status']}")
            changed = True

        any_win = any(leg["status"] == "WIN" for leg in newly_closed)
        still_open = [leg for leg in race["legs"] if leg["status"] == "OPEN"]

        if any_win and still_open and not race.get("breakeven_shifted"):
            log(f"{race['symbol']} {race['action']}: leg won — shifting "
                f"{len(still_open)} remaining leg(s) to breakeven at entry {race['entry']}")
            for leg in still_open:
                success, method = attempt_modify_stop(api, leg, race["entry"])
                leg["breakeven_attempted"] = True
                leg["breakeven_success"] = success
                leg["breakeven_method"] = method
            race["breakeven_shifted"] = True
            changed = True

    if changed:
        save_json(ACTIVE_RACES_FILE, races)
    else:
        log(f"Checked {len(races)} active race(s) — no changes")


if __name__ == "__main__":
    main()
