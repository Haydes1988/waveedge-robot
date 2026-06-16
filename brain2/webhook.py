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

MARKET_MAP = {
    "EURUSD": "EUR/USD",
    "XAUUSD": "Gold"
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

def place_trade(symbol, action, risk_usd):
    try:
        api = GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)
        market_name = MARKET_MAP.get(symbol, symbol)
        market_info = api.get_market_info(market_name)
        market_id = market_info["MarketId"]
        prices = api.get_prices(market_id=market_id, num_ticks=1)
        if action == "buy":
            offer_price = prices["PriceTicks"][0]["Ask"]
            direction = "buy"
        else:
            offer_price = prices["PriceTicks"][0]["Bid"]
            direction = "sell"
        quantity = max(1000, int(risk_usd * 100))
        result = api.trade_order(
            quantity=quantity,
            offer_price=offer_price,
            direction=direction,
            trading_acc_id=TRADING_ACC_ID,
            market_id=market_id
        )
        log("TRADE PLACED: " + symbol + " " + action + " qty:" + str(quantity) + " price:" + str(offer_price))
        return result
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
        risk = data.get("risk", 5)
        strategy = data.get("strategy", "unknown")
        log("SIGNAL: " + strategy + " " + action + " " + symbol + " Score:" + str(score))
        result = place_trade(symbol, action, risk)
        trade = {
            "timestamp": datetime.utcnow().isoformat(),
            "strategy": strategy,
            "symbol": symbol,
            "action": action,
            "score": score,
            "risk": risk,
            "result": str(result),
            "status": "executed" if result else "failed"
        }
        save_trade(trade)
        return jsonify({"status": "ok", "trade": str(result)}), 200
    except Exception as e:
        log("ERROR: " + str(e))
        return jsonify({"status": "error"}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "WaveEdge running"}), 200

if __name__ == "__main__":
    log("WaveEdge Webhook starting...")
    app.run(host="0.0.0.0", port=80, debug=False)
