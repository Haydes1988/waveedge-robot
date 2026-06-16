python3 -c "
import os
code = '''from flask import Flask, request, jsonify
import json
import os
from datetime import datetime

app = Flask(__name__)
PASSPHRASE = \"WAVEEDGE2024\"
LOG_FILE = \"/root/waveedge/logs/webhook.log\"
TRADE_FILE = \"/root/waveedge/data/trades.json\"

def log(msg):
    timestamp = datetime.utcnow().isoformat()
    with open(LOG_FILE, \"a\") as f:
        f.write(f\"{timestamp} | {msg}\\n\")
    print(f\"{timestamp} | {msg}\")

def save_trade(data):
    trades = []
    if os.path.exists(TRADE_FILE):
        with open(TRADE_FILE, \"r\") as f:
            try:
                trades = json.load(f)
            except:
                trades = []
    trades.append(data)
    with open(TRADE_FILE, \"w\") as f:
        json.dump(trades, f, indent=2)

@app.route(\"/webhook\", methods=[\"POST\"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            log(\"ERROR: Empty payload\")
            return jsonify({\"status\": \"error\"}), 400
        if data.get(\"pass\") != PASSPHRASE:
            log(\"SECURITY: Bad passphrase\")
            return jsonify({\"status\": \"unauthorized\"}), 403
        trade = {
            \"timestamp\": datetime.utcnow().isoformat(),
            \"strategy\": data.get(\"strategy\", \"unknown\"),
            \"symbol\": data.get(\"sym\", \"\"),
            \"action\": data.get(\"action\", \"\"),
            \"score\": data.get(\"score\", 0),
            \"risk\": data.get(\"risk\", 0),
            \"status\": \"received\"
        }
        save_trade(trade)
        log(f\"SIGNAL: {trade[\'strategy\']} {trade[\'action\']} {trade[\'symbol\']}\")
        return jsonify({\"status\": \"ok\"}), 200
    except Exception as e:
        log(f\"ERROR: {str(e)}\")
        return jsonify({\"status\": \"error\"}), 500

@app.route(\"/health\", methods=[\"GET\"])
def health():
    return jsonify({\"status\": \"WaveEdge running\"}), 200

if __name__ == \"__main__\":
    log(\"WaveEdge Webhook starting...\")
    app.run(host=\"0.0.0.0\", port=8080, debug=False)
'''
with open('/root/waveedge/brain2/webhook.py', 'w') as f:
    f.write(code)
print('Done')
"
