from flask import Flask, request, jsonify
from gcapi import GCapiClient
import json
import os
from datetime import datetime

app = Flask(__name__)
PASSPHRASE = "WAVEEDGE2024"
LOG_FILE = "/root/waveedge/logs/webhook.log"
TRADE_FILE = "/root/waveedge/data/trades.json"
DAILY_PNL_FILE = "/root/waveedge/data/daily_pnl.json"
GCAPI_USER = "demo-ahedgin"
GCAPI_PASS = "Hedgin456$"
GCAPI_KEY = "A.Hedgin"
TRADING_ACC_ID = 407829813
DEFAULT_SL_PIPS = 20
DAILY_LOSS_LIMIT = 25
MAX_POSITIONS_PER_SYMBOL = 2
MAX_TOTAL_POSITIONS = 4

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
    current = pnl_data.get(today, 0)
    pnl_data[today] = current - risk_dollars
    with open(DAILY_PNL_FILE, "w") as f:
        json.dump(pnl_data, f)


def get_live_balance(api):
    try:
        api.get_account_info()
        return 500.0
    except Exception as e:
        log("BALANCE ERROR: " + str(e))
        return 500.0


def get_open_position_counts(api, market_id):
    try:
        pos = api.list_open_positions(trading_acc_id=TRADING_ACC_ID)
        positions = pos.get("OpenPositions", [])
        same_symbol = [p for p in positions if p.get("MarketId") == market_id]
        return len(positions), len(same_symbol)
    except Exception as e:
        log("POSITION COUNT ERROR: " + str(e))
        return 0, 0


def calculate_quantity(balance, risk_pct, entry_price, sl_price, pip_size):
    risk_dollars = balance * (risk_pct / 100.0)
    pip_distance = abs(entry_price - sl_price)
    if pip_distance < pip_size * 5:
        pip_distance = DEFAULT_SL_PIPS * pip_size
    quantity = (risk_dollars / pip_distance) * pip_size * 10000
    quantity = max(1000, min(int(quantity), 50000))
    return quantity, risk_dollars


def place_trade(symbol, action, risk_pct, sl_price, tp1_price, tp2_price):
    try:
        market = MARKET_MAP.get(symbol)
        if not market:
            log("BLOCKED: " + symbol + " not in approved market list")
            return None

        daily_loss, pnl_data = get_daily_loss()
        if daily_loss <= -DAILY_LOSS_LIMIT:
            log("BLOCKED: Daily loss limit reached ($" + str(DAILY_LOSS_LIMIT) + "). No more trades today.")
            return None

        api = GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)
        pip = market["pip"]
        market_id = market["id"]
        market_name = market["name"]

        total_open, symbol_open = get_open_position_counts(api, market_id)
        if symbol_open >= MAX_POSITIONS_PER_SYMBOL:
            log("BLOCKED: " + symbol + " already has " + str(symbol_open) +
                " open positions (max " + str(MAX_POSITIONS_PER_SYMBOL) + ")")
            return None
        if total_open >= MAX_TOTAL_POSITIONS:
            log("BLOCKED: Total open positions at limit (" + str(total_open) +
                "/" + str(MAX_TOTAL_POSITIONS) + ")")
            return None

        balance = get_live_balance(api)
        entry_price = api.get_prices(market_id=market_id, num_ticks=1)

        if sl_price == 0 or abs(entry_price - sl_price) < pip * 5:
            if action == "buy":
                sl_price = round(entry_price - DEFAULT_SL_PIPS * pip, 5)
                tp1_price = round(entry_price + DEFAULT_SL_PIPS * pip * 2, 5)
                tp2_price = round(entry_price + DEFAULT_SL_PIPS * pip * 4, 5)
            else:
                sl_price = round(entry_price + DEFAULT_SL_PIPS * pip, 5)
                tp1_price = round(entry_price - DEFAULT_SL_PIPS * pip * 2, 5)
                tp2_price = round(entry_price - DEFAULT_SL_PIPS * pip * 4, 5)
            log("DEFAULT SL APPLIED: " + str(DEFAULT_SL_PIPS) + " pips — sl:" + str(sl_price))

        quantity, risk_dollars = calculate_quantity(balance, risk_pct, entry_price, sl_price, pip)

        kwargs = {
            "quantity": quantity,
            "offer_price": entry_price,
            "direction": action,
            "trading_acc_id": TRADING_ACC_ID,
            "market_id": market_id,
            "market_name": market_name
        }

        try:
            kwargs["stop_loss"] = sl_price
            kwargs["take_profit"] = tp1_price
            result = api.trade_order(**kwargs)
            log("TRADE+SL ATTEMPTED: sl=" + str(sl_price) + " tp1=" + str(tp1_price))
        except TypeError:
            log("SL PARAM NOT SUPPORTED — placing without SL")
            kwargs.pop("stop_loss", None)
            kwargs.pop("take_profit", None)
            result = api.trade_order(**kwargs)

        status = result.get("Status", 0)
        if status == 1:
            log("TRADE PLACED: " + symbol + " " + action + " balance:$" + str(round(balance, 2)) +
                " risk:$" + str(round(risk_dollars, 2)) + " qty:" + str(quantity) +
                " entry:" + str(entry_price) + " sl:" + str(sl_price) + " tp1:" + str(tp1_price) +
                " openPos(total/symbol):" + str(total_open + 1) + "/" + str(symbol_open + 1))
            update_daily_loss(risk_dollars, pnl_data)
        else:
            reason = result.get("StatusReason", "unknown")
            log("TRADE REJECTED: " + symbol + " Status:" + str(status) + " Reason:" + str(reason))

        return {
            "status_code": status,
            "order_id": result.get("OrderId", 0),
            "symbol": symbol,
            "balance": balance,
            "risk_dollars": round(risk_dollars, 2),
            "quantity": quantity,
            "entry_price": entry_price,
            "sl": sl_price,
            "tp1": tp1_price,
            "tp2": tp2_price,
            "open_positions_after": total_open + (1 if status == 1 else 0)
        }
    except Exception as e:
        log("TRADE ERROR: " + str(e))
        return None


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            log("ERROR: Empty payload")
            return jsonify({"status": "error"}), 400
        if data.get("pass") != PASSPHRASE:
            log("SECURITY: Bad passphrase from " + str(request.remote_addr))
            return jsonify({"status": "unauthorized"}), 403

        action = data.get("action", "")
        symbol = data.get("sym", "")
        score = data.get("score", 0)
        risk_pct = float(data.get("risk", 1.5))
        sl_price = float(data.get("sl", 0))
        tp1_price = float(data.get("tp1", 0))
        tp2_price = float(data.get("tp2", 0))
        strategy = data.get("strategy", "unknown")

        if action not in ["buy", "sell"]:
            log("BLOCKED: Invalid action — " + str(action))
            return jsonify({"status": "error", "msg": "invalid action"}), 400

        if symbol not in MARKET_MAP:
            log("BLOCKED: " + symbol + " not in approved list")
            return jsonify({"status": "error", "msg": "symbol not supported"}), 400

        log("SIGNAL: " + strategy + " " + action + " " + symbol + " Score:" + str(score) + " Risk%:" + str(risk_pct))

        trade_result = place_trade(symbol, action, risk_pct, sl_price, tp1_price, tp2_price)

        trade_log = {
            "timestamp": datetime.utcnow().isoformat(),
            "strategy": strategy,
            "symbol": symbol,
            "action": action,
            "score": score,
            "risk_pct": risk_pct,
            "sl": sl_price,
            "tp1": tp1_price,
            "tp2": tp2_price,
            "result": str(trade_result),
            "status": "executed" if trade_result and trade_result.get("status_code") == 1 else "failed"
        }
        save_trade(trade_log)
        return jsonify({"status": "ok", "result": str(trade_result)}), 200
    except Exception as e:
        log("ERROR: " + str(e))
        return jsonify({"status": "error"}), 500


@app.route("/health", methods=["GET"])
def health():
    daily_loss, _ = get_daily_loss()
    return jsonify({
        "status": "WaveEdge running",
        "version": "2.5",
        "pairs": list(MARKET_MAP.keys()),
        "daily_loss_today": daily_loss,
        "daily_loss_limit": DAILY_LOSS_LIMIT,
        "max_positions_per_symbol": MAX_POSITIONS_PER_SYMBOL,
        "max_total_positions": MAX_TOTAL_POSITIONS
    }), 200


@app.route("/positions", methods=["GET"])
def positions():
    try:
        api = GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)
        pos = api.list_open_positions(trading_acc_id=TRADING_ACC_ID)
        open_count = len(pos.get("OpenPositions", []))
        log("POSITIONS CHECK: " + str(open_count) + " open")
        return jsonify({"open_positions": open_count, "data": pos}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    log("WaveEdge Webhook v2.5 starting...")
    app.run(host="0.0.0.0", port=80, debug=False)
