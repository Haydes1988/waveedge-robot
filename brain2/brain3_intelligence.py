#!/usr/bin/env python3
"""
WaveEdge Brain 3 v2 — Market Intelligence Hub
Runs every 15 minutes via cron.
NEW: Direct ForexFactory news feed, email notifications, change detection.
Sends email only when something CHANGES — no spam.
"""

import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from urllib.request import urlopen, Request

# ─── CONFIG ───────────────────────────────────────────────────────────────────
CONTEXT_FILE   = "/root/waveedge/data/market_context.json"
PREV_CONTEXT   = "/root/waveedge/data/market_context_prev.json"
NOTIFY_LOG     = "/root/waveedge/data/notifications.json"
TRADES_FILE    = "/root/waveedge/data/trades.json"
LOG_FILE       = "/root/waveedge/logs/brain3.log"

# Gmail credentials — set GMAIL_APP_PASS in /root/waveedge/security/.env
# To get App Password: myaccount.google.com/apppasswords
GMAIL_USER     = "ahedgin@gmail.com"
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")
NOTIFY_EMAIL   = "ahedgin@gmail.com"
EMAIL_ENABLED  = bool(GMAIL_APP_PASS)

# Note: ForexFactory + Investing.com URLs are managed in news_sources.py
NEWS_FALLBACK  = "/root/waveedge/data/news_calendar.json"

# Pairs
PAIRS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X"
}

PIP_SIZES = {
    "EURUSD": 0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01,
    "AUDUSD": 0.0001, "USDCAD": 0.0001, "USDCHF": 0.0001,
    "NZDUSD": 0.0001
}


# ─── LOGGING ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.utcnow().isoformat()
    line = f"{ts} | BRAIN3 | {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


# ─── EMAIL NOTIFICATIONS ──────────────────────────────────────────────────────

def send_email(subject, body):
    """Send email notification via Gmail SMTP."""
    if not EMAIL_ENABLED:
        log(f"EMAIL (no creds set): {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = NOTIFY_EMAIL
        html = f"""
        <html><body>
        <div style="font-family:monospace;background:#111;color:#0f0;padding:20px;border-radius:8px">
        <h2 style="color:#ff0;margin:0 0 10px 0">⚡ WaveEdge Alert</h2>
        <pre style="white-space:pre-wrap;color:#0f0">{body}</pre>
        <hr style="border-color:#333;margin:15px 0">
        <p style="color:#666;font-size:11px">WaveEdge Robot | {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
        </div></body></html>"""
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASS)
            server.send_message(msg)
        log(f"EMAIL SENT: {subject}")
        return True
    except Exception as e:
        log(f"EMAIL ERROR: {e}")
        return False


def save_notification(notif_type, subject, body):
    """Save notification to local log file (always, email or not)."""
    try:
        notifs = []
        if os.path.exists(NOTIFY_LOG):
            with open(NOTIFY_LOG, "r") as f:
                notifs = json.load(f)
        notifs.append({
            "time": datetime.utcnow().isoformat(),
            "type": notif_type,
            "subject": subject,
            "body": body,
            "emailed": EMAIL_ENABLED
        })
        notifs = notifs[-100:]  # keep last 100
        with open(NOTIFY_LOG, "w") as f:
            json.dump(notifs, f, indent=2)
    except Exception as e:
        log(f"Notification log error: {e}")


def notify(notif_type, subject, body):
    """Log and optionally email a notification."""
    log(f"ALERT [{notif_type}]: {subject}")
    save_notification(notif_type, subject, body)
    send_email(f"WaveEdge: {subject}", body)


# ─── NEWS FETCHING (DUAL SOURCE) ─────────────────────────────────────────────
# news_sources.py handles ForexFactory + Investing.com with merge + dedup

def get_news_data():
    """Fetch from ForexFactory + Investing.com, merged and deduplicated."""
    try:
        import sys
        import os as _os
        brain2_dir = _os.path.dirname(_os.path.abspath(__file__))
        if brain2_dir not in sys.path:
            sys.path.insert(0, brain2_dir)
        from news_sources import fetch_all_news
        return fetch_all_news(log_fn=log)
    except ImportError:
        log("news_sources.py not found — using ForexFactory direct fetch")
    # Fallback: direct ForexFactory fetch
    try:
        req = Request(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        log(f"Fallback: {len(data)} events from ForexFactory only")
        return data
    except Exception as e:
        log(f"All news sources failed: {e}")
        # Last resort: Brain 2 file
        try:
            if os.path.exists(NEWS_FALLBACK):
                with open(NEWS_FALLBACK) as f:
                    return json.load(f)
        except Exception:
            pass
        return []


def parse_event_time(time_str):
    """Parse ForexFactory event time to UTC datetime."""
    if not time_str:
        return None
    try:
        # ForexFactory format: "2026-07-02T12:30:00-0400"
        from datetime import timezone
        ts = time_str.strip()
        # Handle timezone offset
        if "+" in ts[10:] or (ts.count("-") > 2):
            # Has timezone info
            sign = 1
            if ts[-5] == "-":
                sign = -1
                ts_clean = ts[:-5]
                offset_str = ts[-4:]
            elif ts[-5] == "+":
                ts_clean = ts[:-5]
                offset_str = ts[-4:]
            else:
                ts_clean = ts
                offset_str = "0000"
            dt = datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S")
            hrs = int(offset_str[:2])
            mins = int(offset_str[2:])
            offset = timedelta(hours=hrs, minutes=mins)
            dt = dt - (offset * sign)  # convert to UTC
            return dt
        else:
            return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def check_news_block(news_data):
    """
    Returns (block, upcoming_events, critical_events).
    Blocks if HIGH impact event within 45 minutes.
    Warns for events within 2 hours.
    """
    if not news_data:
        return False, [], []

    now = datetime.utcnow()
    block_window = now + timedelta(minutes=45)
    warn_window  = now + timedelta(hours=2)

    block        = False
    upcoming     = []
    critical     = []

    for event in news_data:
        impact = str(event.get("impact", "")).strip().lower()
        if impact not in ("high", "medium"):
            continue

        title    = event.get("title", event.get("name", "Unknown Event"))
        country  = event.get("country", event.get("currency", ""))
        time_str = event.get("date", event.get("time", ""))
        evt_time = parse_event_time(time_str)
        if not evt_time:
            continue

        if now <= evt_time <= warn_window:
            mins_away = int((evt_time - now).total_seconds() / 60)
            entry = {
                "title": title, "country": country,
                "time_utc": evt_time.strftime("%H:%M UTC"),
                "mins_away": mins_away, "impact": impact.upper()
            }
            upcoming.append(entry)
            if evt_time <= block_window and impact == "high":
                block = True
                critical.append(entry)

    return block, upcoming, critical


# ─── MARKET ANALYSIS ──────────────────────────────────────────────────────────

def fetch_ohlcv(symbol, period, interval):
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        if df is None or df.empty:
            return None
        try:
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
        except Exception:
            pass
        return df
    except Exception as e:
        log(f"Fetch error {symbol} {interval}: {e}")
        return None


def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calc_adx(high, low, close, period=14):
    import pandas as pd
    import numpy as np
    up = high - high.shift(1)
    down = low.shift(1) - low
    pdm = pd.Series(np.where((up > down) & (up > 0), up, 0), index=high.index)
    ndm = pd.Series(np.where((down > up) & (down > 0), down, 0), index=low.index)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / (atr + 1e-10)
    ndi = 100 * ndm.ewm(span=period, adjust=False).mean() / (atr + 1e-10)
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi + 1e-10)
    return dx.ewm(span=period, adjust=False).mean()


def get_pair_adx_regime(symbol):
    try:
        h1 = fetch_ohlcv(symbol, "30d", "1h")
        if h1 is None or len(h1) < 60:
            return None, "UNKNOWN"
        h4 = h1.resample("4h", label="right", closed="right").agg(
            {"High": "max", "Low": "min", "Close": "last"}
        ).dropna()
        if len(h4) < 20:
            return None, "UNKNOWN"
        adx = calc_adx(h4["High"], h4["Low"], h4["Close"])
        v = float(adx.iloc[-1])
        regime = "TRENDING" if v >= 25 else "WEAK_TREND" if v >= 18 else "RANGING"
        return round(v, 1), regime
    except Exception as e:
        log(f"ADX error {symbol}: {e}")
        return None, "UNKNOWN"


def get_d1_bias(symbol):
    try:
        d1 = fetch_ohlcv(symbol, "3mo", "1d")
        if d1 is None or len(d1) < 25:
            return "NEUTRAL"
        ema20 = calc_ema(d1["Close"], 20)
        close = float(d1["Close"].iloc[-1])
        ema   = float(ema20.iloc[-1])
        gap   = abs(close - ema) / ema
        if gap < 0.0003:
            return "NEUTRAL"
        return "BULL" if close > ema else "BEAR"
    except Exception:
        return "NEUTRAL"


def get_recent_performance(pair_name, lookback=15):
    try:
        if not os.path.exists(TRADES_FILE):
            return 50.0, 0, 0
        with open(TRADES_FILE, "r") as f:
            trades = json.load(f)
        executed = [t for t in trades if t.get("symbol") == pair_name
                    and t.get("status") == "executed"
                    and ("WIN" in str(t.get("result", "")).upper()
                         or "LOSS" in str(t.get("result", "")).upper())]
        recent = executed[-lookback:]
        if not recent:
            return 50.0, 0, 0
        wins   = sum(1 for t in recent if "WIN" in str(t.get("result", "")).upper())
        losses = len(recent) - wins
        return round(wins / len(recent) * 100, 1), wins, losses
    except Exception:
        return 50.0, 0, 0


def determine_threshold_adjustment(wr, regime):
    adj = 0
    parts = []
    if wr < 30:
        adj += 2
        parts.append(f"poor WR ({wr:.0f}%)")
    elif wr < 40:
        adj += 1
        parts.append(f"below-avg WR ({wr:.0f}%)")
    if regime == "RANGING":
        adj += 2
        parts.append("market RANGING")
    elif regime == "WEAK_TREND":
        adj += 1
        parts.append("weak trend")
    return adj, ", ".join(parts) if parts else "conditions normal"


# ─── CHANGE DETECTION ─────────────────────────────────────────────────────────

def load_previous_context():
    try:
        if os.path.exists(PREV_CONTEXT):
            with open(PREV_CONTEXT, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def detect_changes_and_notify(prev_ctx, new_ctx):
    """Compare previous and current context. Fire notifications on changes."""
    if prev_ctx is None:
        log("First run — no previous context to compare")
        return

    # News block newly activated
    was_blocked = prev_ctx.get("block_all", False)
    now_blocked = new_ctx.get("block_all", False)
    if now_blocked and not was_blocked:
        events = new_ctx.get("news_events_upcoming", [])
        crit   = [e for e in events if e.get("impact") == "HIGH"]
        lines = [f"⚠️  NEWS BLOCK ACTIVATED — trades paused\n"]
        for e in crit:
            lines.append(f"  🔴 {e['title']} ({e['country']}) — {e['time_utc']} ({e['mins_away']} min)")
        body = "\n".join(lines)
        notify("NEWS_BLOCK", "NEWS BLOCK ACTIVATED — trades paused", body)

    # News block cleared
    if was_blocked and not now_blocked:
        notify("NEWS_CLEAR", "News block cleared — trading resumed",
               "All high-impact events have passed. Robot is accepting signals again.")

    # Pair blocks — newly blocked
    prev_pairs = prev_ctx.get("pair_context", {})
    new_pairs  = new_ctx.get("pair_context", {})
    for pair, ctx in new_pairs.items():
        prev = prev_pairs.get(pair, {})
        was_pair_blocked = prev.get("block", False)
        now_pair_blocked = ctx.get("block", False)
        if now_pair_blocked and not was_pair_blocked:
            reason = ctx.get("block_reason", "conditions unfavourable")
            body = (f"Pair: {pair}\nReason: {reason}\n"
                    f"ADX: {ctx.get('adx_4h')} ({ctx.get('regime')})\n"
                    f"D1 Bias: {ctx.get('bias_d1')}\n"
                    f"Recent WR: {ctx.get('recent_wr')}%")
            notify("PAIR_BLOCK", f"{pair} BLOCKED — {reason}", body)

    # Upcoming HIGH events (warn 2 hours before, once)
    new_upcoming = new_ctx.get("news_events_upcoming", [])
    prev_events  = {e.get("title", "") for e in prev_ctx.get("news_events_upcoming", [])}
    for evt in new_upcoming:
        if (evt.get("impact") == "HIGH" and
                evt.get("title") not in prev_events and
                evt.get("mins_away", 999) <= 90):
            body = (f"Event: {evt['title']} ({evt['country']})\n"
                    f"Time:  {evt['time_utc']} ({evt['mins_away']} minutes away)\n"
                    f"Impact: {evt['impact']}\n\n"
                    f"Brain 3 will block all trades within 45 minutes of this event.")
            notify("NEWS_WARN", f"⚡ HIGH IMPACT: {evt['title']} in {evt['mins_away']} min", body)


# ─── MAIN BUILD CONTEXT ───────────────────────────────────────────────────────

def build_context():
    log("Building market context...")
    now = datetime.utcnow()

    # Load previous for change detection
    prev_ctx = load_previous_context()

    # Fetch news — ForexFactory + Investing.com merged
    news_data = get_news_data()

    news_block, upcoming, critical = check_news_block(news_data)
    if news_block:
        log(f"⚠️  NEWS BLOCK: {[e['title'] for e in critical]}")
    else:
        log(f"News: {len(upcoming)} events upcoming (none blocking right now)")

    # Pair intelligence
    pair_context = {}
    for pair_name, symbol in PAIRS.items():
        adx_val, regime = get_pair_adx_regime(symbol)
        bias             = get_d1_bias(symbol)
        wr, wins, losses = get_recent_performance(pair_name)
        score_adj, reason = determine_threshold_adjustment(wr, regime)

        pair_block   = False
        block_reason = ""
        if regime == "RANGING" and wr < 30:
            pair_block   = True
            block_reason = f"ranging market + poor performance ({wr:.0f}% WR)"

        pair_context[pair_name] = {
            "adx_4h": adx_val, "regime": regime, "bias_d1": bias,
            "recent_wr": wr, "recent_wins": wins, "recent_losses": losses,
            "score_adjustment": score_adj, "reason": reason,
            "block": pair_block, "block_reason": block_reason
        }
        status = "🔴 BLOCKED" if pair_block else f"🟡+{score_adj}" if score_adj > 0 else "🟢 CLEAR"
        log(f"{pair_name}: ADX={adx_val} {regime} | D1:{bias} | WR:{wr:.0f}% | {status}")

    context = {
        "updated": now.isoformat(),
        "updated_human": now.strftime("%Y-%m-%d %H:%M UTC"),
        "news_block_active": news_block,
        "news_events_upcoming": upcoming,
        "critical_events": critical,
        "pair_context": pair_context,
        "system_mode": "NEWS_BLOCK" if news_block else "NORMAL",
        "block_all": news_block
    }

    os.makedirs(os.path.dirname(CONTEXT_FILE), exist_ok=True)
    with open(CONTEXT_FILE, "w") as f:
        json.dump(context, f, indent=2)

    # Detect changes and notify
    detect_changes_and_notify(prev_ctx, context)

    # Save current as previous for next run
    with open(PREV_CONTEXT, "w") as f:
        json.dump(context, f, indent=2)

    log(f"Context written — mode: {context['system_mode']}")
    return context


def print_summary(ctx):
    print("\n" + "=" * 54)
    print("  WAVEEDGE BRAIN 3 v2 — MARKET INTELLIGENCE")
    print(f"  {ctx['updated_human']}")
    print("=" * 54)
    print(f"  System Mode  : {ctx['system_mode']}")
    print(f"  News Block   : {'YES ⚠️' if ctx['news_block_active'] else 'No'}")
    print(f"  Email alerts : {'ON ✅' if EMAIL_ENABLED else 'OFF — set GMAIL_APP_PASS'}")
    if ctx["news_events_upcoming"]:
        print("  Upcoming events:")
        for e in ctx["news_events_upcoming"][:5]:
            bar = "🔴" if e["impact"] == "HIGH" else "🟡"
            print(f"  {bar} {e['mins_away']:3}min | {e['country']} | {e['title'][:40]}")
    print("-" * 54)
    print("  PAIR INTELLIGENCE:")
    for pair, ctx_p in ctx["pair_context"].items():
        status = "🔴 BLOCKED" if ctx_p["block"] else f"🟡 +{ctx_p['score_adjustment']}" if ctx_p["score_adjustment"] > 0 else "🟢 CLEAR"
        print(f"  {pair:8} | {ctx_p['regime']:12} | D1:{ctx_p['bias_d1']:4} | "
              f"WR:{ctx_p['recent_wr']:4.0f}% | {status}")
    print("=" * 54)


if __name__ == "__main__":
    ctx = build_context()
    print_summary(ctx)
