import time
import json
import os
import subprocess
from datetime import datetime
from gcapi import GCapiClient

LOG_FILE = "/root/waveedge/logs/monitor.log"
WEBHOOK_LOG = "/root/waveedge/logs/webhook.log"
ALERT_FILE = "/root/waveedge/data/alerts.json"
GCAPI_USER = "demo-ahedgin"
GCAPI_PASS = "Hedgin456$"
GCAPI_KEY = "A.Hedgin"
TRADING_ACC_ID = 407829813

ATTACK_PATTERNS = [
    "cgi-bin",
    "/etc/passwd",
    "bin/sh",
    "cmd.exe",
    "phpMyAdmin",
    "wp-admin",
    "eval(",
    "../",
    "%2e%2e",
    "shell"
]

MAX_OPEN_POSITIONS = 3
CHECK_INTERVAL = 900

def log(msg):
    ts = datetime.utcnow().isoformat()
    line = ts + " | MONITOR | " + msg
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)

def save_alert(alert_type, detail):
    alerts = []
    if os.path.exists(ALERT_FILE):
        with open(ALERT_FILE, "r") as f:
            try:
                alerts = json.load(f)
            except Exception:
                alerts = []
    alerts.append({
        "timestamp": datetime.utcnow().isoformat(),
        "type": alert_type,
        "detail": detail
    })
    with open(ALERT_FILE, "w") as f:
        json.dump(alerts, f, indent=2)

def check_webhook_log_for_attacks():
    if not os.path.exists(WEBHOOK_LOG):
        return
    try:
        with open(WEBHOOK_LOG, "r") as f:
            lines = f.readlines()
        recent = lines[-100:] if len(lines) > 100 else lines
        for line in recent:
            for pattern in ATTACK_PATTERNS:
                if pattern.lower() in line.lower():
                    if "404" in line or "403" in line:
                        log("ATTACK BLOCKED: " + pattern + " — " + line.strip())
                        save_alert("ATTACK_BLOCKED", line.strip())
                    else:
                        log("ATTACK WARNING: " + pattern + " — " + line.strip())
                        save_alert("ATTACK_WARNING", line.strip())
    except Exception as e:
        log("LOG CHECK ERROR: " + str(e))

def check_open_positions():
    try:
        api = GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)
        pos = api.list_open_positions(trading_acc_id=TRADING_ACC_ID)
        positions = pos.get("OpenPositions", [])
        count = len(positions)
        if count == 0:
            log("POSITIONS: Clean — 0 open")
            return
        log("POSITIONS: " + str(count) + " open")
        for p in positions:
            has_sl = p.get("StopOrder") is not None
            direction = p.get("Direction", "unknown")
            price = p.get("Price", 0)
            order_id = p.get("OrderId", 0)
            if not has_sl:
                log("WARNING: OrderId " + str(order_id) + " " + direction + " @ " + str(price) + " has NO stop loss")
                save_alert("NO_STOP_LOSS", "OrderId:" + str(order_id) + " " + direction + "@" + str(price))
        if count > MAX_OPEN_POSITIONS:
            log("WARNING: " + str(count) + " positions open — exceeds max " + str(MAX_OPEN_POSITIONS))
            save_alert("TOO_MANY_POSITIONS", str(count) + " open positions")
    except Exception as e:
        log("POSITION CHECK ERROR: " + str(e))

def check_webhook_alive():
    try:
        result = subprocess.run(
            ["curl", "-s", "http://127.0.0.1/health"],
            capture_output=True, text=True, timeout=5
        )
        if "WaveEdge running" in result.stdout:
            log("WEBHOOK: Alive")
        else:
            log("WEBHOOK: DOWN — restarting...")
            save_alert("WEBHOOK_DOWN", "Health check failed")
            subprocess.Popen([
                "nohup", "python3",
                "/root/waveedge/brain2/webhook.py",
                ">", "/root/waveedge/logs/webhook.log",
                "2>&1", "&"
            ])
    except Exception as e:
        log("WEBHOOK CHECK ERROR: " + str(e))

if __name__ == "__main__":
    log("WaveEdge Security Monitor starting...")
    while True:
        try:
            check_webhook_log_for_attacks()
            check_open_positions()
            check_webhook_alive()
        except Exception as e:
            log("MONITOR LOOP ERROR: " + str(e))
        time.sleep(CHECK_INTERVAL)
