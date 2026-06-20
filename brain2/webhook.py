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
MARKET_ID = 401484347
MARKET_NAME = "EUR/USD"

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
        margin = api.get_margin_info()
        balance = margin.get("Balance", 0)
        if balance == 0:
            info = api.get_account_info()
            balance = 500
        return float(balance)
    except Exception as e:
        log("BALANCE ERROR: " + str(e) + " — using fallback 500")
        return 500.0

def calculate_quantity(balance, risk_pct, entry_price, sl_price):
    risk_dollars = balance * (risk_pct / 100.0)
    pip_distance = abs(entry_price - sl_price)
    if pip_distance <= 0:
        pip_distance = 0.0010
    quantity = (risk_dollars / pip_distance) * 0.0001 * 10000
    quantity = max(1000, min(int(quantity), 50000))
    return quantity, risk_dollars

def place_trade(symbol, action, risk_pct, sl_price, tp1_price, tp2_price):
    try:
        if symbol != "EURUSD":
            log("BLOCKED: " + symbol + " not available on GCAPI demo")
            return None

        api = GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)
        balance = get_live_balance(api)
        offer_price = api.get_prices(market_id=MARKET_ID, num_ticks=1)
        quantity, risk_dollars = calculate_quantity(balance, risk_pct, offer_price, sl_price)
        half_qty = int(quantity / 2)
        remaining_qty = quantity - half_qty

        result = api.trade_order(
            quantity=quantity,
            offer_price=offer_price,
            direction=action,
            trading_acc_id=TRADING_ACC_ID,
            market_id=MARKET_ID,
            market_name=MARKET_NAME
        )

        log("TRADE PLACED: " + symbol + " " + action + " balance:$" + str(round(balance,2)) + " risk:$" + str(round(risk_dollars,2)) + " qty:" + str(quantity) + " price:" + str(offer_price) + " sl:" + str(sl_price) + " tp1:" + str(tp1_price) + " tp2:" + str(tp2_price))

        return {
            "order_result": result,
            "balance": balance,
            "risk_dollars": risk_dollars,
            "quantity": quantity,
            "half_qty": half_qty,
            "remaining_qty": remaining_qty,
            "entry_price": offer_price,
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
            log("SECURITY: Bad passphrase")
            return jsonify({"status": "unauthorized"}), 403

        action = data.get("action", "")
        symbol = data.get("sym", "")
        score = data.get("score", 0)
        risk_pct = float(data.get("risk", 1.5))
        sl_price = float(data.get("sl", 0))
        tp1_price = float(data.get("tp1", 0))
        tp2_price = float(data.get("tp2", 0))
        strategy = data.get("strategy", "unknown")

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
            "status": "executed" if trade_result else "failed"
        }
        save_trade(trade_log)
        return jsonify({"status": "ok", "trade": str(trade_result)}), 200
    except Exception as e:
        log("ERROR: " + str(e))
        return jsonify({"status": "error"}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "WaveEdge running"}), 200

if __name__ == "__main__":
    log("WaveEdge Webhook v2 starting...")
    app.run(host="0.0.0.0", port=80, debug=False)
