from flask import Flask, request, jsonify
from gcapi import GCapiClient
import json
import os
from datetime import datetime, timedelta

app = Flask(__name__)
PASSPHRASE = "WAVEEDGE2024"
LOG_FILE        = "/root/waveedge/logs/webhook.log"
TRADE_FILE      = "/root/waveedge/data/trades.json"
DAILY_PNL_FILE  = "/root/waveedge/data/daily_pnl.json"
CONTEXT_FILE    = "/root/waveedge/data/market_context.json"
SIGNALS_FILE    = "/root/waveedge/data/recent_signals.json"

GCAPI_USER      = "demo-ahedgin"
GCAPI_PASS      = "Hedgin456$"
GCAPI_KEY       = "A.Hedgin"
TRADING_ACC_ID  = 407829813

DEFAULT_SL_PIPS        = 20
DAILY_LOSS_LIMIT       = 25
MAX_POSITIONS_PER_SYMBOL = 2
MAX_TOTAL_POSITIONS    = 4

# Strategy min_scores — Brain 3 may raise these dynamically
BASE_MIN_SCORES = {"Scalp": 5, "Day": 7, "Swing": 8}

MARKET_MAP = {
    "EURUSD": {"id": 401484347, "name": "EUR/USD", "pip": 0.0001},
    "GBPUSD": {"id": 401484392, "name": "GBP/USD", "pip": 0.0001},
    "EURJPY": {"id": 401484338, "name": "EUR/JPY", "pip": 0.01},
    "GBPJPY": {"id": 401484385, "name": "GBP/JPY", "pip": 0.01},
    "USDJPY": {"id": 401484414, "name": "USD/JPY", "pip": 0.01},
    "AUDUSD": {"id": 401484317, "name": "AUD/USD", "pip": 0.0001},
    "USDCAD": {"id": 401484406, "name": "USD/CAD", "pip": 0.0001},
    "EURGBP": {"id": 401484335, "name": "EUR/GBP", "pip": 0.0001},
    "USDCHF": {"id": 401484407, "name": "USD/CHF", "pip": 0.0001},
    "NZDUSD": {"id": 401484402, "name": "NZD/USD", "pip": 0.0001}
}


def log(msg):
    ts = datetime.utcnow().isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(ts + " | " + msg + "\n")
    print(ts + " | " + msg)


def save_trade(data):
    trades = []
    if os.path.exists(TRADE_FILE):
        with open(TRADE_FILE, "r") as f:
            try:
                trades = json.load(f)
            except Exception:
                trades = []
    trades.append(data)
    with open(TRADE_FILE, "w") as f:
        json.dump(trades, f, indent=2)


def get_daily_loss():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        with open(DAILY_PNL_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        data = {}
    return data.get(today, 0), data


def update_daily_loss(risk_dollars, pnl_data):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    pnl_data[today] = pnl_data.get(today, 0) - risk_dollars
    with open(DAILY_PNL_FILE, "w") as f:
        json.dump(pnl_data, f)


# ─── BRAIN 3 INTELLIGENCE INTEGRATION ────────────────────────────────────────

def read_market_context():
    """Read Brain 3's market context. Returns None if file missing/stale."""
    try:
        if not os.path.exists(CONTEXT_FILE):
            return None
        with open(CONTEXT_FILE, "r") as f:
            ctx = json.load(f)
        updated = datetime.fromisoformat(ctx.get("updated", "2000-01-01"))
        if datetime.utcnow() - updated > timedelta(hours=3):
            log("BRAIN3: Context file is stale (>3h old) — proceeding without intelligence layer")
            return None
        return ctx
    except Exception as e:
        log(f"BRAIN3 read error: {e}")
        return None


def intelligence_check(symbol, strategy, score, action, context):
    """
    Apply Brain 3 intelligence before executing a trade.
    Returns (approved, reason, adjusted_min_score)
    """
    if context is None:
        return True, "Brain3 offline — proceeding with base rules", BASE_MIN_SCORES.get(strategy, 5)

    # Global news block
    if context.get("block_all") or context.get("news_block_active"):
        events = context.get("news_events_upcoming", [])
        event_str = events[0].get("event", "high-impact news") if events else "high-impact news"
        return False, f"NEWS BLOCK: {event_str} within 45min", BASE_MIN_SCORES.get(strategy, 5)

    # Pair-specific intelligence
    pair_ctx = context.get("pair_context", {}).get(symbol, {})
    if not pair_ctx:
        return True, "No pair context available", BASE_MIN_SCORES.get(strategy, 5)

    # Pair blocked by Brain 3 (ranging + poor performance combo)
    if pair_ctx.get("block"):
        return False, f"PAIR BLOCKED: {pair_ctx.get('block_reason', 'conditions unfavourable')}", BASE_MIN_SCORES.get(strategy, 5)

    # Directional bias check — warn if signal contradicts D1 EMA20
    d1_bias = pair_ctx.get("bias_d1", "NEUTRAL")
    if d1_bias == "BULL" and action == "sell":
        log(f"BRAIN3 WARNING: {symbol} SELL signal but D1 bias is BULL — reduced confidence")
    elif d1_bias == "BEAR" and action == "buy":
        log(f"BRAIN3 WARNING: {symbol} BUY signal but D1 bias is BEAR — reduced confidence")

    # Adjusted minimum score
    base_min  = BASE_MIN_SCORES.get(strategy, 5)
    adj       = pair_ctx.get("score_adjustment", 0)
    final_min = base_min + adj
    regime    = pair_ctx.get("regime", "UNKNOWN")
    wr        = pair_ctx.get("recent_wr", 50)

    if score < final_min:
        return False, (f"SCORE TOO LOW: {score} < {final_min} "
                       f"(base {base_min} + adj {adj} for {regime}, WR={wr:.0f}%)"), final_min

    reason = f"Brain3 APPROVED: score {score} >= {final_min} | {regime} | D1:{d1_bias} | WR:{wr:.0f}%"
    return True, reason, final_min


# ─── CONFLUENCE TRACKER ───────────────────────────────────────────────────────

def log_signal_for_confluence(symbol, strategy, action, score):
    """Track recent signals to detect cross-strategy confluence."""
    try:
        signals = []
        if os.path.exists(SIGNALS_FILE):
            with open(SIGNALS_FILE, "r") as f:
                signals = json.load(f)
        now = datetime.utcnow()
        signals.append({
            "time": now.isoformat(),
            "symbol": symbol,
            "strategy": strategy,
            "action": action,
            "score": score
        })
        cutoff = now - timedelta(hours=4)
        signals = [s for s in signals
                   if datetime.fromisoformat(s["time"]) > cutoff]
        with open(SIGNALS_FILE, "w") as f:
            json.dump(signals, f)
        recent_same = [s for s in signals
                       if s["symbol"] == symbol and s["action"] == action
                       and s["strategy"] != strategy]
        if recent_same:
            strategies = list(set(s["strategy"] for s in recent_same))
            log(f"CONFLUENCE: {symbol} {action} also fired on {strategies} in last 4h — HIGH CONFIDENCE")
            return True
        return False
    except Exception as e:
        log(f"Confluence tracker error: {e}")
        return False


# ─── CORE TRADING FUNCTIONS ───────────────────────────────────────────────────

def get_open_position_counts(api, market_id):
    try:
        pos = api.list_open_positions(trading_acc_id=TRADING_ACC_ID)
        positions = pos.get("OpenPositions", [])
        same_symbol = [p for p in positions if p.get("MarketId") == market_id]
        return len(positions), len(same_symbol)
    except Exception as e:
        log(f"POSITION COUNT ERROR: {e}")
        return 0, 0


def calculate_quantity(balance, risk_pct, entry_price, sl_price, pip_size):
    risk_dollars = balance * (risk_pct / 100.0)
    pip_distance = abs(entry_price - sl_price)
    if pip_distance < pip_size * 5:
        pip_distance = DEFAULT_SL_PIPS * pip_size
    quantity = (risk_dollars / pip_distance) * pip_size * 10000
    quantity = max(1000, min(int(quantity), 50000))
    return quantity, risk_dollars


def place_trade(symbol, action, strategy, risk_pct, sl_price, tp1_price, tp2_price,
                score, intelligence_approved, intel_reason):
    try:
        market = MARKET_MAP.get(symbol)
        if not market:
            log(f"BLOCKED: {symbol} not in approved market list")
            return None

        daily_loss, pnl_data = get_daily_loss()
        if daily_loss <= -DAILY_LOSS_LIMIT:
            log(f"BLOCKED: Daily loss limit reached (${DAILY_LOSS_LIMIT})")
            return None

        if not intelligence_approved:
            log(f"BLOCKED by Brain3: {intel_reason}")
            return None

        api = GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)
        pip = market["pip"]
        market_id   = market["id"]
        market_name = market["name"]

        total_open, symbol_open = get_open_position_counts(api, market_id)
        if symbol_open >= MAX_POSITIONS_PER_SYMBOL:
            log(f"BLOCKED: {symbol} at position limit ({symbol_open}/{MAX_POSITIONS_PER_SYMBOL})")
            return None
        if total_open >= MAX_TOTAL_POSITIONS:
            log(f"BLOCKED: Total position limit reached ({total_open}/{MAX_TOTAL_POSITIONS})")
            return None

        balance     = 500.0
        entry_price = api.get_prices(market_id=market_id, num_ticks=1)

        if sl_price == 0 or abs(entry_price - sl_price) < pip * 5:
            if action == "buy":
                sl_price  = round(entry_price - DEFAULT_SL_PIPS * pip, 5)
                tp1_price = round(entry_price + DEFAULT_SL_PIPS * pip * 2, 5)
                tp2_price = round(entry_price + DEFAULT_SL_PIPS * pip * 4, 5)
            else:
                sl_price  = round(entry_price + DEFAULT_SL_PIPS * pip, 5)
                tp1_price = round(entry_price - DEFAULT_SL_PIPS * pip * 2, 5)
                tp2_price = round(entry_price - DEFAULT_SL_PIPS * pip * 4, 5)
            log(f"DEFAULT SL APPLIED: {DEFAULT_SL_PIPS} pips")

        quantity, risk_dollars = calculate_quantity(balance, risk_pct, entry_price, sl_price, pip)

        kwargs = {
            "quantity": quantity, "offer_price": entry_price, "direction": action,
            "trading_acc_id": TRADING_ACC_ID, "market_id": market_id, "market_name": market_name
        }
        try:
            kwargs["stop_loss"]   = sl_price
            kwargs["take_profit"] = tp1_price
            result = api.trade_order(**kwargs)
        except TypeError:
            kwargs.pop("stop_loss", None)
            kwargs.pop("take_profit", None)
            result = api.trade_order(**kwargs)

        status = result.get("Status", 0)
        if status == 1:
            log(f"✅ TRADE PLACED: {symbol} {action} | score:{score} | "
                f"entry:{entry_price} sl:{sl_price} tp1:{tp1_price} | "
                f"risk:${round(risk_dollars,2)} qty:{quantity} | "
                f"Brain3: {intel_reason}")
            update_daily_loss(risk_dollars, pnl_data)
        else:
            reason = result.get("StatusReason", "unknown")
            log(f"❌ TRADE REJECTED: {symbol} Status:{status} Reason:{reason}")

        return {
            "status_code": status, "symbol": symbol, "action": action,
            "risk_dollars": round(risk_dollars, 2), "quantity": quantity,
            "entry_price": entry_price, "sl": sl_price, "tp1": tp1_price, "tp2": tp2_price,
            "intelligence_reason": intel_reason
        }
    except Exception as e:
        log(f"TRADE ERROR: {e}")
        return None


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"status": "error"}), 400
        if data.get("pass") != PASSPHRASE:
            log(f"SECURITY: Bad passphrase from {request.remote_addr}")
            return jsonify({"status": "unauthorized"}), 403

        action   = data.get("action", "")
        symbol   = data.get("sym", "")
        score    = int(data.get("score", 0))
        risk_pct = float(data.get("risk", 1.5))
        sl       = float(data.get("sl", 0))
        tp1      = float(data.get("tp1", 0))
        tp2      = float(data.get("tp2", 0))
        strategy = data.get("strategy", "unknown")

        if action not in ["buy", "sell"]:
            return jsonify({"status": "error", "msg": "invalid action"}), 400
        if symbol not in MARKET_MAP:
            log(f"BLOCKED: {symbol} not in approved list")
            return jsonify({"status": "ok", "msg": "symbol not supported"}), 200

        log(f"📡 SIGNAL: {strategy} {action} {symbol} score:{score} risk:{risk_pct}%")

        confluence = log_signal_for_confluence(symbol, strategy, action, score)
        context    = read_market_context()
        approved, reason, adj_min = intelligence_check(symbol, strategy, score, action, context)

        if confluence and approved:
            log(f"CONFLUENCE BOOST: {symbol} — multiple strategies agree")

        trade_result = place_trade(symbol, action, strategy, risk_pct,
                                   sl, tp1, tp2, score, approved, reason)

        trade_log = {
            "timestamp": datetime.utcnow().isoformat(),
            "strategy": strategy, "symbol": symbol, "action": action,
            "score": score, "risk_pct": risk_pct, "sl": sl, "tp1": tp1, "tp2": tp2,
            "intelligence_approved": approved, "intelligence_reason": reason,
            "confluence": confluence,
            "status": "executed" if trade_result and trade_result.get("status_code") == 1 else "blocked"
        }
        save_trade(trade_log)
        return jsonify({"status": "ok", "approved": approved, "reason": reason}), 200

    except Exception as e:
        log(f"WEBHOOK ERROR: {e}")
        return jsonify({"status": "error"}), 500


@app.route("/health", methods=["GET"])
def health():
    daily_loss, _ = get_daily_loss()
    ctx = read_market_context()
    brain3_status = ctx.get("system_mode", "offline") if ctx else "offline"
    pair_summary = {}
    if ctx:
        for pair, pc in ctx.get("pair_context", {}).items():
            pair_summary[pair] = {
                "regime": pc.get("regime"),
                "bias": pc.get("bias_d1"),
                "wr": pc.get("recent_wr"),
                "adj": pc.get("score_adjustment"),
                "block": pc.get("block")
            }
    return jsonify({
        "status": "WaveEdge running", "version": "2.6",
        "pairs": list(MARKET_MAP.keys()),
        "daily_loss_today": daily_loss,
        "daily_loss_limit": DAILY_LOSS_LIMIT,
        "max_positions_per_symbol": MAX_POSITIONS_PER_SYMBOL,
        "max_total_positions": MAX_TOTAL_POSITIONS,
        "brain3_status": brain3_status,
        "news_block": ctx.get("news_block_active", False) if ctx else "unknown",
        "pair_intelligence": pair_summary
    }), 200


@app.route("/intelligence", methods=["GET"])
def intelligence():
    """New endpoint — full Brain 3 context for monitoring."""
    ctx = read_market_context()
    if not ctx:
        return jsonify({"status": "Brain3 offline", "context": None}), 200
    return jsonify(ctx), 200


@app.route("/positions", methods=["GET"])
def positions():
    try:
        api = GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)
        pos = api.list_open_positions(trading_acc_id=TRADING_ACC_ID)
        open_count = len(pos.get("OpenPositions", []))
        return jsonify({"open_positions": open_count, "data": pos}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    log("WaveEdge Webhook v2.6 (Brain3 connected) starting...")
    app.run(host="0.0.0.0", port=80, debug=False)
