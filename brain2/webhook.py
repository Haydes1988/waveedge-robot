from flask import Flask, request, jsonify
from gcapi import GCapiClient
import json
import os
from datetime import datetime

app = Flask(__name__)
PASSPHRASE = "WAVEEDGE2024"
LOG_FILE = "/root/waveedge/logs/webhook.log"
TRADE_FILE = "/root/waveedge/data/trades.json"
GCAPI_USER = "demo-ahedgin"
GCAPI_PASS = "Hedgin456$"
GCAPI_KEY = "A.Hedgin"
TRADING_ACC_ID = 407829813
DEFAULT_SL_PIPS = 20

MARKET_MAP = {
    "EURUSD": {"id": 401484347, "name": "EUR/USD", "pip": 0.0001},
    "GBPUSD": {"id": 401484392, "name": "GBP/USD", "pip": 0.0001},
    "EURJPY": {"id": 401484338, "name": "EUR/JPY", "pip": 0.01},
    "GBPJPY": {"id": 401484385, "name": "GBP/JPY", "pip": 0.01},
    "USDJPY": {"id": 401484414, "name": "USD/JPY", "pip": 0.01},
    "AUDUSD": {"id": 401484317, "name": "AUD/USD", "pip": 0.0001},
    "USDCAD": {"id": 401484406, "name": "USD/CAD", "pip": 0.0001},
    "EURGBP": {"id": 401484335, "name": "EUR/GBP", "pip": 0.0001}
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

def get_live_balance(api):
    try:
        api.get_account_info()
        return 500.0
    except Exception as e:
        log("BALANCE ERROR: " + str(e))
        return 500.0

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

        api = GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)
        balance = get_live_balance(api)
        pip = market["pip"]
        market_id = market["id"]
        market_name = market["name"]

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

        result = api.trade_order(
            quantity=quantity,
            offer_price=entry_price,
            direction=action,
            trading_acc_id=TRADING_ACC_ID,
            market_id=market_id,
            market_name=market_name
        )

        status = result.get("Status", 0)
        if status == 1:
            log("TRADE PLACED: " + symbol + " " + action + " balance:$" + str(round(balance,2)) + " risk:$" + str(round(risk_dollars,2)) + " qty:" + str(quantity) + " entry:" + str(entry_price) + " sl:" + str(sl_price) + " tp1:" + str(tp1_price) + " tp2:" + str(tp2_price))
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
            "tp2": tp2_price
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
    return jsonify({"status": "WaveEdge running", "version": "2.2", "pairs": list(MARKET_MAP.keys())}), 200

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
    log("WaveEdge Webhook v2.2 starting...")
    app.run(host="0.0.0.0", port=80, debug=False)
