#!/usr/bin/env python3
"""
WaveEdge EMA 10/23 Cross Alert
Runs every 5 minutes via cron. Watches every pair's H1 EMA10/EMA23 for a
FRESH cross since the last check, and sends a real notification the
moment it happens -- BUY-side (10 crosses above 23) or SELL-side (10
crosses below 23) -- regardless of whether a full trade signal ever
fires. This gives direct visibility into sell-side activity specifically,
even when no trade results from it, addressing "I never see any sells."
"""
import json
import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timezone
from env_loader import load_env

load_env()

PAIRS_YF = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X",
}

STATE_FILE = "/root/waveedge/data/ema_cross_state.json"
LOG_FILE   = "/root/waveedge/logs/ema_cross_alert.log"

GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")
NOTIFY_TO      = os.environ.get("NOTIFY_EMAIL", GMAIL_USER)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(msg):
    ts = utcnow().isoformat()
    print(f"{ts} | EMACROSS | {msg}")


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


def send_email(subject, body):
    if not GMAIL_APP_PASS:
        log(f"NO GMAIL_APP_PASS SET -- would have sent: {subject}")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = NOTIFY_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASS)
            server.send_message(msg)
        log(f"EMAIL SENT: {subject}")
    except Exception as e:
        log(f"EMAIL FAILED: {e}")


def get_ema_state(pair, yf_symbol):
    import yfinance as yf
    df = yf.Ticker(yf_symbol).history(period="5d", interval="1h")
    if df is None or df.empty or len(df) < 25:
        return None
    ema10 = df["Close"].ewm(span=10, adjust=False).mean()
    ema23 = df["Close"].ewm(span=23, adjust=False).mean()
    return "BULL" if ema10.iloc[-1] > ema23.iloc[-1] else "BEAR"


def main():
    state = load_json(STATE_FILE, {})
    changed = False

    for pair, yf_symbol in PAIRS_YF.items():
        current = get_ema_state(pair, yf_symbol)
        if current is None:
            log(f"{pair}: could not fetch data, skipping")
            continue
        prev = state.get(pair)

        if prev is not None and prev != current:
            side = "BUY-SIDE" if current == "BULL" else "SELL-SIDE"
            subject = f"WaveEdge: {pair} EMA 10/23 crossed -- {side}"
            body = (
                f"{pair}: EMA10 just crossed {'above' if current == 'BULL' else 'below'} "
                f"EMA23 on the H1 chart.\n\n"
                f"This is a {side} signal on the moving average filter -- "
                f"it does NOT mean a trade has fired. This is the underlying "
                f"structure the entry gate checks. Whether an actual trade "
                f"executes still depends on score, Brain 3 approval, and the "
                f"rest of the strategy's conditions.\n\n"
                f"Previous state: {prev} | New state: {current}"
            )
            send_email(subject, body)
            log(f"{pair}: CROSS DETECTED {prev} -> {current} ({side})")
            changed = True
        else:
            log(f"{pair}: {current} (no change)")

        state[pair] = current

    if changed:
        save_json(STATE_FILE, state)
    else:
        save_json(STATE_FILE, state)  # keep state fresh even with no cross


if __name__ == "__main__":
    main()
