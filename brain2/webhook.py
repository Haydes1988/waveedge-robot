#!/usr/bin/env python3
"""WaveEdge live execution gateway v4.0.

One approved setup opens one coordinated three-position race. All legs share
the broker entry and stop, have progressive TP1/TP2/TP3 objectives, and split
one account-level risk budget 40/35/25.
"""

import hashlib
import hmac
import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

app = Flask(__name__)

HOME = os.getenv("WAVEEDGE_HOME", "/root/waveedge")
LOG_FILE = os.getenv("WAVEEDGE_WEBHOOK_LOG", f"{HOME}/logs/webhook.log")
TRADE_FILE = os.getenv("WAVEEDGE_TRADES_FILE", f"{HOME}/data/trades.json")
DAILY_PNL_FILE = os.getenv("WAVEEDGE_DAILY_PNL_FILE", f"{HOME}/data/daily_pnl.json")
CONTEXT_FILE = os.getenv("WAVEEDGE_CONTEXT_FILE", f"{HOME}/data/market_context.json")
SIGNALS_FILE = os.getenv("WAVEEDGE_SIGNALS_FILE", f"{HOME}/data/recent_signals.json")

PASSPHRASE = os.getenv("WAVEEDGE_WEBHOOK_PASSPHRASE", "")
GCAPI_USER = os.getenv("WAVEEDGE_GCAPI_USER", "")
GCAPI_PASS = os.getenv("WAVEEDGE_GCAPI_PASS", "")
GCAPI_KEY = os.getenv("WAVEEDGE_GCAPI_KEY", "")
TRADING_ACC_ID = int(os.getenv("WAVEEDGE_TRADING_ACC_ID", "407829813"))
ACCOUNT_BALANCE = float(os.getenv("WAVEEDGE_ACCOUNT_BALANCE", "500"))

DEFAULT_SL_PIPS = 20
DAILY_LOSS_LIMIT = float(os.getenv("WAVEEDGE_DAILY_LOSS_LIMIT", "25"))
TOTAL_DRAWDOWN_LIMIT = float(os.getenv("WAVEEDGE_TOTAL_DRAWDOWN_LIMIT", "75"))
MAX_RISK_PCT = float(os.getenv("WAVEEDGE_MAX_RISK_PCT", "1.0"))
MAX_POSITIONS_PER_SYMBOL = 3
MAX_TOTAL_POSITIONS = int(os.getenv("WAVEEDGE_MAX_TOTAL_POSITIONS", "9"))
RISK_WEIGHTS = (0.40, 0.35, 0.25)
FALLBACK_TP_RS = (1.0, 2.0, 3.5)
MIN_TP_RS = (0.8, 1.5, 2.0)
BASE_MIN_SCORES = {"Scalp": 7, "Day": 7, "Swing": 8}

MARKET_MAP = {
    "EURUSD": {"id": 401484347, "name": "EUR/USD", "pip": 0.0001, "usd_quote": True},
    "GBPUSD": {"id": 401484392, "name": "GBP/USD", "pip": 0.0001, "usd_quote": True},
    "USDJPY": {"id": 401484414, "name": "USD/JPY", "pip": 0.01, "usd_quote": False},
    "AUDUSD": {"id": 401484317, "name": "AUD/USD", "pip": 0.0001, "usd_quote": True},
    "USDCAD": {"id": 401484406, "name": "USD/CAD", "pip": 0.0001, "usd_quote": False},
    "USDCHF": {"id": 401484407, "name": "USD/CHF", "pip": 0.0001, "usd_quote": False},
    "NZDUSD": {"id": 401484402, "name": "NZD/USD", "pip": 0.0001, "usd_quote": True},
}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(message):
    line = f"{utcnow().isoformat()} | WEBHOOK | {message}"
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
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


def append_json(path, item, limit=None):
    import fcntl
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        values = load_json(path, [])
        if not isinstance(values, list):
            values = []
        values.append(item)
        if limit:
            values = values[-limit:]
        atomic_json(path, values)


def as_number(value, field, default=None):
    if value in (None, "") and default is not None:
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def make_api():
    missing = [name for name, value in (
        ("WAVEEDGE_GCAPI_USER", GCAPI_USER), ("WAVEEDGE_GCAPI_PASS", GCAPI_PASS),
        ("WAVEEDGE_GCAPI_KEY", GCAPI_KEY)) if not value]
    if missing:
        raise RuntimeError("Missing broker environment: " + ", ".join(missing))
    from gcapi import GCapiClient
    return GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)


def get_daily_pnl():
    today = utcnow().strftime("%Y-%m-%d")
    data = load_json(DAILY_PNL_FILE, {})
    return float(data.get(today, 0.0)), data


def get_total_pnl():
    data = load_json(DAILY_PNL_FILE, {})
    return round(sum(float(v) for v in data.values() if isinstance(v, (int, float))), 2)


def get_realized_drawdown():
    """Approximate peak-to-trough drawdown from ordered daily realized P/L."""
    data = load_json(DAILY_PNL_FILE, {})
    running = peak = 0.0
    worst = 0.0
    for day in sorted(data):
        value = data[day]
        if not isinstance(value, (int, float)):
            continue
        running += float(value)
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return round(worst, 2)


def read_market_context():
    context = load_json(CONTEXT_FILE, None)
    if not isinstance(context, dict):
        return None, "Brain 3 context missing"
    try:
        updated = datetime.fromisoformat(str(context["updated"]).replace("Z", "+00:00"))
        if updated.tzinfo is not None:
            updated = updated.astimezone(timezone.utc).replace(tzinfo=None)
    except (KeyError, TypeError, ValueError):
        return None, "Brain 3 context timestamp invalid"
    if utcnow() - updated > timedelta(minutes=45):
        return None, "Brain 3 context stale (>45 minutes)"
    return context, None


def intelligence_check(symbol, strategy, score, action, context):
    base = BASE_MIN_SCORES.get(strategy, 8)
    if context is None:
        return False, "BRAIN OFFLINE: execution fails closed", base
    pair = context.get("pair_context", {}).get(symbol)
    if not isinstance(pair, dict):
        return False, f"No Brain 3 context for {symbol}", base
    if context.get("block_all"):
        return False, "Global safety/news block active", base
    if pair.get("news_block"):
        return False, pair.get("news_block_reason", "Pair-specific news block active"), base
    if pair.get("block"):
        return False, pair.get("block_reason", "Pair blocked by Brain 3"), base

    permission = pair.get("trade_permission", "WAIT")
    allowed = {
        "BUY_ONLY": {"buy"}, "SELL_ONLY": {"sell"}, "BOTH": {"buy", "sell"},
        "WAIT": set(), "BLOCK": set(),
    }.get(permission, set())
    if action not in allowed:
        return False, f"Direction blocked: {permission} does not allow {action.upper()}", base

    adjustment = int(pair.get("score_adjustment", 0) or 0)
    final_min = base + adjustment
    if score < final_min:
        return False, f"Score too low: {score} < {final_min}", final_min
    return True, f"Brain 3 approved {permission}; score {score} >= {final_min}", final_min


def signal_key(data, symbol, action, strategy):
    anchor = data.get("alert_id") or data.get("bar_time") or data.get("time")
    if anchor is None:
        anchor = utcnow().strftime("%Y-%m-%dT%H:%M")
    raw = f"{symbol}|{action}|{strategy}|{anchor}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def register_signal(key, symbol, action, strategy, score):
    import fcntl
    os.makedirs(os.path.dirname(SIGNALS_FILE), exist_ok=True)
    with open(SIGNALS_FILE + ".lock", "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        signals = load_json(SIGNALS_FILE, [])
        now = utcnow()
        cutoff = now - timedelta(hours=4)
        kept = []
        duplicate = False
        for signal in signals if isinstance(signals, list) else []:
            try:
                when = datetime.fromisoformat(signal["time"])
            except (KeyError, TypeError, ValueError):
                continue
            if when > cutoff:
                kept.append(signal)
                duplicate = duplicate or signal.get("signal_key") == key
        if duplicate:
            return True, False
        confluence = any(s.get("symbol") == symbol and s.get("action") == action and
                         s.get("strategy") != strategy for s in kept)
        kept.append({"time": now.isoformat(), "signal_key": key, "symbol": symbol,
                     "action": action, "strategy": strategy, "score": score})
        atomic_json(SIGNALS_FILE, kept)
        return False, confluence


def extract_price(payload, action):
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, list) and payload:
        return extract_price(payload[0], action)
    if not isinstance(payload, dict):
        raise ValueError("Broker price response has an unsupported shape")
    preferred = ("Offer", "offer", "Ask", "ask") if action == "buy" else ("Bid", "bid")
    for key in preferred + ("Price", "price", "Last", "last"):
        if key in payload and isinstance(payload[key], (int, float)):
            return float(payload[key])
    for value in payload.values():
        if isinstance(value, (dict, list)):
            try:
                return extract_price(value, action)
            except ValueError:
                pass
    raise ValueError("No numeric broker price found")


def open_position_counts(api, market_id):
    response = api.list_open_positions(trading_acc_id=TRADING_ACC_ID)
    positions = response.get("OpenPositions", []) or []
    same = [p for p in positions if p.get("MarketId", p.get("marketId")) == market_id]
    return len(positions), len(same)


def normalize_targets(entry, sl, action, supplied):
    risk = abs(entry - sl)
    if risk <= 0:
        raise ValueError("Stop loss equals entry")
    sign = 1 if action == "buy" else -1
    fallback = [entry + sign * risk * r for r in FALLBACK_TP_RS]
    # Old chart alerts only carried TP1/TP2. Mixing those legacy targets with a
    # newly-derived TP3 would make live execution disagree with the v5 backtest.
    # Until the chart sends all three, use the complete canonical ladder.
    if len(supplied) < 3 or not supplied[2]:
        return fallback
    targets = []
    previous_r = 0.0
    for index, raw in enumerate(supplied):
        candidate = raw if raw and math.isfinite(raw) else fallback[index]
        r_value = sign * (candidate - entry) / risk
        if r_value < MIN_TP_RS[index] or r_value <= previous_r:
            candidate = fallback[index]
            r_value = FALLBACK_TP_RS[index]
        targets.append(candidate)
        previous_r = r_value
    if not (targets[0] * sign < targets[1] * sign < targets[2] * sign):
        targets = fallback
    return targets


def quantity_for_risk(risk_dollars, entry, sl, market):
    stop_pips = abs(entry - sl) / market["pip"]
    if stop_pips < 5:
        raise ValueError("Stop loss is under 5 pips")
    pip_value_unit = market["pip"] if market["usd_quote"] else market["pip"] / entry
    raw = risk_dollars / (stop_pips * pip_value_unit)
    quantity = max(1000, min(int(raw), 50000))
    actual = quantity * stop_pips * pip_value_unit
    return quantity, round(actual, 2)


def broker_order(api, market, action, quantity, entry, sl, tp):
    kwargs = {"quantity": quantity, "offer_price": entry, "direction": action,
              "trading_acc_id": TRADING_ACC_ID, "market_id": market["id"],
              "market_name": market["name"], "stop_loss": sl, "take_profit": tp}
    try:
        result = api.trade_order(**kwargs)
    except TypeError as exc:
        raise RuntimeError("Broker client does not support attached SL/TP; naked order refused") from exc
    status = int(result.get("Status", 0))
    return status == 1, result


def blocked(stage, reason, symbol=None, action=None):
    log(f"BLOCKED [{stage}] {reason}")
    return {"executed": False, "block_stage": stage, "block_reason": reason,
            "symbol": symbol, "action": action, "legs": []}

def record_active_race(symbol, action, entry, sl, legs):
    """Called right after place_race() executes successfully — persists
    race details so breakeven_manager.py (a separate script) can watch
    for a winning leg and shift the others to breakeven."""
    import json, os
    path = "/root/waveedge/data/active_races.json"
    races = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                races = json.load(f)
        except Exception:
            races = []
    races.append({
        "symbol": symbol, "action": action, "entry": entry, "sl": sl,
        "opened_at": __import__("datetime").datetime.utcnow().isoformat(),
        "all_closed": False, "breakeven_shifted": False,
        "legs": [{"position": leg["position"], "tp": leg["tp"],
                 "broker_order_id": leg.get("broker_order_id"),
                 "status": "OPEN"} for leg in legs if leg.get("executed")]
    })
    races = races[-100:]
    with open(path, "w") as f:
        json.dump(races, f, indent=2)
def place_race(symbol, action, risk_pct, sl_price, supplied_targets):
    market = MARKET_MAP[symbol]
    daily_pnl, _ = get_daily_pnl()
    if daily_pnl <= -DAILY_LOSS_LIMIT:
        return blocked("daily_loss_limit", f"Daily realized P/L {daily_pnl:.2f}", symbol, action)
    drawdown = get_realized_drawdown()
    if drawdown <= -TOTAL_DRAWDOWN_LIMIT:
        return blocked("drawdown_stop", f"Realized drawdown {drawdown:.2f}", symbol, action)

    try:
        api = make_api()
        total_open, symbol_open = open_position_counts(api, market["id"])
    except Exception as exc:
        return blocked("broker_unavailable", str(exc), symbol, action)
    if symbol_open + 3 > MAX_POSITIONS_PER_SYMBOL:
        return blocked("position_limit_symbol", "A complete three-leg race will not fit", symbol, action)
    if total_open + 3 > MAX_TOTAL_POSITIONS:
        return blocked("position_limit_total", "A complete three-leg race will not fit", symbol, action)

    try:
        entry = extract_price(api.get_prices(market_id=market["id"], num_ticks=1), action)
        pip = market["pip"]
        if sl_price == 0:
            sl_price = entry - DEFAULT_SL_PIPS * pip if action == "buy" else entry + DEFAULT_SL_PIPS * pip
        if (action == "buy" and sl_price >= entry) or (action == "sell" and sl_price <= entry):
            raise ValueError("Stop loss is on the wrong side of broker entry")
        targets = normalize_targets(entry, sl_price, action, supplied_targets)
    except Exception as exc:
        return blocked("invalid_levels", str(exc), symbol, action)

    total_risk = ACCOUNT_BALANCE * risk_pct / 100.0
    prepared = []
    try:
        for index, (weight, target) in enumerate(zip(RISK_WEIGHTS, targets), start=1):
            quantity, actual_risk = quantity_for_risk(total_risk * weight, entry, sl_price, market)
            prepared.append({"position": index, "weight": weight, "tp": target,
                             "quantity": quantity, "risk_dollars": actual_risk})
    except ValueError as exc:
        return blocked("position_sizing", str(exc), symbol, action)
    actual_total = sum(leg["risk_dollars"] for leg in prepared)
    if actual_total > total_risk * 1.10:
        return blocked("minimum_size_risk", f"Actual ${actual_total:.2f} exceeds intended ${total_risk:.2f}", symbol, action)

    legs = []
    for leg in prepared:
        try:
            ok, result = broker_order(api, market, action, leg["quantity"], entry,
                                      sl_price, leg["tp"])
        except Exception as exc:
            ok, result = False, {"StatusReason": str(exc)}
        order_id = result.get("OrderId", result.get("PositionId", result.get("TradeId")))
        legs.append({**leg, "executed": ok, "status_code": result.get("Status", 0),
                     "broker_order_id": order_id,
                     "broker_reason": None if ok else result.get("StatusReason", "unknown")})
        if not ok:
            break
    executed = [leg for leg in legs if leg["executed"]]
    if executed:
       record_active_race(symbol, action, entry, sl_price, legs)
    complete = len(executed) == 3
    stage = None if complete else ("partial_execution" if executed else "broker_rejected")
    reason = None if complete else f"{len(executed)}/3 race legs executed"
    log(f"RACE {symbol} {action.upper()}: {len(executed)}/3 legs executed; entry {entry}, SL {sl_price}")
    return {"executed": bool(executed), "race_complete": complete, "block_stage": stage,
            "block_reason": reason, "symbol": symbol, "action": action,
            "entry_price": entry, "sl": sl_price, "legs": legs,
            "risk_dollars": round(sum(x["risk_dollars"] for x in executed), 2)}


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"status": "error", "msg": "JSON object required"}), 400
        if not PASSPHRASE:
            return jsonify({"status": "error", "msg": "server passphrase not configured"}), 503
        supplied_pass = str(data.get("pass", ""))
        if not hmac.compare_digest(supplied_pass, PASSPHRASE):
            log(f"SECURITY bad passphrase from {request.remote_addr}")
            return jsonify({"status": "unauthorized"}), 403

        action = str(data.get("action", "")).strip().lower()
        raw_symbol = data.get("sym")
        if raw_symbol is None and data.get("symbol") is not None:
            raw_symbol = data.get("symbol")
            log("DEPRECATED payload key 'symbol' accepted; TradingView must send 'sym'")
        symbol = str(raw_symbol or "").upper().replace("/", "")
        strategy = str(data.get("strategy", "unknown")).strip().title()
        score = int(as_number(data.get("score", 0), "score"))
        risk_pct = min(max(as_number(data.get("risk", 1.0), "risk"), 0.0), MAX_RISK_PCT)
        sl = as_number(data.get("sl", 0), "sl")
        targets = [as_number(data.get(f"tp{i}", 0), f"tp{i}") for i in (1, 2, 3)]

        if action not in ("buy", "sell"):
            raise ValueError("action must be buy or sell")
        if symbol not in MARKET_MAP:
            raise ValueError(f"unsupported symbol {symbol!r}")
        if risk_pct <= 0:
            raise ValueError("risk must be greater than zero")

        key = signal_key(data, symbol, action, strategy)
        duplicate, confluence = register_signal(key, symbol, action, strategy, score)
        if duplicate:
            return jsonify({"status": "ok", "signal_status": "duplicate_ignored"}), 200

        context, context_error = read_market_context()
        approved, reason, required_score = intelligence_check(
            symbol, strategy, score, action, context
        )
        if context_error:
            reason = context_error + "; execution fails closed"
            approved = False
        result = (place_race(symbol, action, risk_pct, sl, targets) if approved
                  else blocked("brain3_reject", reason, symbol, action))

        now = utcnow().isoformat()
        if result.get("executed"):
            for leg in result["legs"]:
                if not leg.get("executed"):
                    continue
                append_json(TRADE_FILE, {
                    "timestamp": now, "signal_id": key, "strategy": strategy,
                    "symbol": symbol, "action": action, "score": score,
                    "risk_pct": risk_pct, "risk_weight": leg["weight"],
                    "race_position": leg["position"], "race_size": 3,
                    "entry_price": result["entry_price"], "sl": result["sl"],
                    "tp1": leg["tp"], "tp": leg["tp"],
                    "risk_dollars": leg["risk_dollars"],
                    "broker_order_id": leg.get("broker_order_id"),
                    "intelligence_approved": True, "intelligence_reason": reason,
                    "required_score": required_score, "confluence": confluence,
                    "status": "executed", "result": None, "outcome_pips": None,
                    "outcome_r": None, "outcome_checked": False, "pnl_recorded": False,
                })
        else:
            append_json(TRADE_FILE, {
                "timestamp": now, "signal_id": key, "strategy": strategy,
                "symbol": symbol, "action": action, "score": score,
                "risk_pct": risk_pct, "intelligence_approved": approved,
                "intelligence_reason": reason, "status": f"blocked_{result.get('block_stage')}",
                "block_reason": result.get("block_reason"), "result": None,
                "outcome_checked": False,
            })

        signal_status = ("executed_three_leg_race" if result.get("race_complete") else
                         f"blocked_{result.get('block_stage')}" if not result.get("executed") else
                         "partial_execution")
        return jsonify({"status": "ok", "approved": approved, "reason": reason,
                        "signal_status": signal_status,
                        "legs_executed": sum(bool(x.get("executed")) for x in result.get("legs", []))}), 200
    except ValueError as exc:
        log(f"BAD SIGNAL: {exc}")
        return jsonify({"status": "error", "msg": str(exc)}), 400
    except Exception as exc:
        log(f"WEBHOOK ERROR: {exc}")
        return jsonify({"status": "error", "msg": "internal error"}), 500


@app.route("/health", methods=["GET"])
def health():
    daily_pnl, _ = get_daily_pnl()
    context, error = read_market_context()
    return jsonify({
        "status": "WaveEdge running", "version": "4.0",
        "execution_model": "three_position_race", "risk_weights": RISK_WEIGHTS,
        "pairs": list(MARKET_MAP), "daily_realized_pnl": daily_pnl,
        "total_realized_pnl": get_total_pnl(), "daily_loss_limit": DAILY_LOSS_LIMIT,
        "realized_drawdown": get_realized_drawdown(),
        "drawdown_stop_at": -TOTAL_DRAWDOWN_LIMIT, "max_risk_pct": MAX_RISK_PCT,
        "max_positions_per_symbol": MAX_POSITIONS_PER_SYMBOL,
        "max_total_positions": MAX_TOTAL_POSITIONS,
        "brain3_status": "online" if context else error,
        "broker_configured": bool(GCAPI_USER and GCAPI_PASS and GCAPI_KEY),
    }), 200


@app.route("/intelligence", methods=["GET"])
def intelligence():
    context, error = read_market_context()
    return jsonify(context if context else {"status": "offline", "reason": error}), 200


@app.route("/positions", methods=["GET"])
def positions():
    try:
        api = make_api()
        data = api.list_open_positions(trading_acc_id=TRADING_ACC_ID)
        return jsonify({"open_positions": len(data.get("OpenPositions", []) or []),
                        "data": data}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


if __name__ == "__main__":
    log("WaveEdge webhook v4.0 starting — three-position race, fail-closed")
    app.run(host="0.0.0.0", port=int(os.getenv("WAVEEDGE_PORT", "80")), debug=False)
