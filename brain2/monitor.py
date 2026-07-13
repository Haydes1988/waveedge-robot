#!/usr/bin/env python3
"""WaveEdge service/security monitor aligned with the three-leg race limits."""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from urllib.request import urlopen

HOME = os.getenv("WAVEEDGE_HOME", "/root/waveedge")
LOG_FILE = os.getenv("WAVEEDGE_MONITOR_LOG", f"{HOME}/logs/monitor.log")
WEBHOOK_LOG = os.getenv("WAVEEDGE_WEBHOOK_LOG", f"{HOME}/logs/webhook.log")
ALERT_FILE = os.getenv("WAVEEDGE_NOTIFY_FILE", f"{HOME}/data/notifications.json")
STATE_FILE = os.getenv("WAVEEDGE_MONITOR_STATE", f"{HOME}/data/monitor_state.json")
WEBHOOK_SCRIPT = os.getenv("WAVEEDGE_WEBHOOK_SCRIPT", f"{HOME}/brain2/webhook.py")
HEALTH_URL = os.getenv("WAVEEDGE_HEALTH_URL", "http://127.0.0.1/health")

GCAPI_USER = os.getenv("WAVEEDGE_GCAPI_USER", "")
GCAPI_PASS = os.getenv("WAVEEDGE_GCAPI_PASS", "")
GCAPI_KEY = os.getenv("WAVEEDGE_GCAPI_KEY", "")
TRADING_ACC_ID = int(os.getenv("WAVEEDGE_TRADING_ACC_ID", "407829813"))
MAX_OPEN_POSITIONS = int(os.getenv("WAVEEDGE_MAX_TOTAL_POSITIONS", "9"))
CHECK_INTERVAL = int(os.getenv("WAVEEDGE_MONITOR_INTERVAL", "900"))

ATTACK_PATTERNS = (
    "cgi-bin", "/etc/passwd", "bin/sh", "cmd.exe", "phpmyadmin",
    "wp-admin", "eval(", "../", "%2e%2e", "shell",
)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(message):
    line = f"{utcnow().isoformat()} | MONITOR | {message}"
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


def save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp.{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def save_alert(alert_type, detail):
    import fcntl
    os.makedirs(os.path.dirname(ALERT_FILE), exist_ok=True)
    with open(ALERT_FILE + ".lock", "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        alerts = load_json(ALERT_FILE, [])
        alerts.append({"time": utcnow().isoformat(), "type": alert_type,
                       "title": alert_type.replace("_", " ").title(), "body": detail})
        save_json(ALERT_FILE, alerts[-100:])


def check_webhook_log_for_attacks():
    if not os.path.exists(WEBHOOK_LOG):
        return
    state = load_json(STATE_FILE, {})
    offset = int(state.get("webhook_log_offset", 0))
    size = os.path.getsize(WEBHOOK_LOG)
    if offset > size:  # log rotated
        offset = 0
    with open(WEBHOOK_LOG, "r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        lines = handle.readlines()
        state["webhook_log_offset"] = handle.tell()
    save_json(STATE_FILE, state)
    for line in lines:
        lowered = line.lower()
        match = next((pattern for pattern in ATTACK_PATTERNS if pattern in lowered), None)
        if match:
            kind = "ATTACK_BLOCKED" if "403" in line or "404" in line else "ATTACK_WARNING"
            log(f"{kind}: {match} — {line.strip()}")
            save_alert(kind, line.strip())


def make_api():
    if not (GCAPI_USER and GCAPI_PASS and GCAPI_KEY):
        raise RuntimeError("Broker credentials are not configured in environment variables")
    from gcapi import GCapiClient
    return GCapiClient(username=GCAPI_USER, password=GCAPI_PASS, appkey=GCAPI_KEY)


def check_open_positions():
    try:
        positions = make_api().list_open_positions(
            trading_acc_id=TRADING_ACC_ID
        ).get("OpenPositions", []) or []
        log(f"POSITIONS: {len(positions)} open")
        for position in positions:
            stop = position.get("StopOrder", position.get("StopLoss", position.get("stop_loss")))
            if stop is None:
                order_id = position.get("OrderId", position.get("PositionId", "unknown"))
                detail = f"Position {order_id} has no detectable stop loss"
                log("WARNING: " + detail)
                save_alert("NO_STOP_LOSS", detail)
        if len(positions) > MAX_OPEN_POSITIONS:
            detail = f"{len(positions)} open positions exceeds maximum {MAX_OPEN_POSITIONS}"
            log("WARNING: " + detail)
            save_alert("TOO_MANY_POSITIONS", detail)
    except Exception as exc:
        log(f"POSITION CHECK ERROR: {exc}")


def webhook_alive():
    try:
        with urlopen(HEALTH_URL, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("status") == "WaveEdge running"
    except Exception:
        return False


def restart_webhook():
    if not os.path.isfile(WEBHOOK_SCRIPT):
        raise FileNotFoundError(f"Webhook script not found: {WEBHOOK_SCRIPT}")
    os.makedirs(os.path.dirname(WEBHOOK_LOG), exist_ok=True)
    with open(WEBHOOK_LOG, "a", encoding="utf-8") as output:
        subprocess.Popen(["python3", WEBHOOK_SCRIPT], stdout=output,
                         stderr=subprocess.STDOUT, start_new_session=True,
                         close_fds=True)


def check_webhook_alive():
    if webhook_alive():
        log("WEBHOOK: Alive")
        return
    log("WEBHOOK: DOWN — restart requested")
    save_alert("WEBHOOK_DOWN", "Health check failed; monitor requested a clean restart")
    try:
        restart_webhook()
    except Exception as exc:
        log(f"WEBHOOK RESTART ERROR: {exc}")


def run_once():
    check_webhook_log_for_attacks()
    check_open_positions()
    check_webhook_alive()


if __name__ == "__main__":
    log("WaveEdge monitor v2 starting")
    while True:
        try:
            run_once()
        except Exception as exc:
            log(f"MONITOR LOOP ERROR: {exc}")
        time.sleep(CHECK_INTERVAL)
