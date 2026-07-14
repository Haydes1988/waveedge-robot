#!/usr/bin/env python3
"""
WaveEdge Brain 3 v7 — closed-candle direction authority and market intelligence
Runs every 15 minutes via cron.
NEW in v5:
  - Multi-timeframe trend engine: H1 / H4 / D1 trend per pair (UP/DOWN/SIDEWAYS)
  - Trend alignment verdict: ALIGNED UPTREND, ALIGNED DOWNTREND, PULLBACK, MIXED/CROSS
  - TREND_CHANGE alerts when a pair's alignment state flips
Retains v4: liquidity map, TP ladder, breaking/reversal points, LEVEL_APPROACH alerts,
dual news source, change-detected alerts to notifications.json. No email.
"""

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request

# ─── CONFIG ───────────────────────────────────────────────────────────────────
HOME = os.getenv("WAVEEDGE_HOME", "/root/waveedge")
CONTEXT_FILE  = os.getenv("WAVEEDGE_CONTEXT_FILE", f"{HOME}/data/market_context.json")
PREV_CONTEXT  = os.getenv("WAVEEDGE_PREV_CONTEXT_FILE", f"{HOME}/data/market_context_prev.json")
NOTIFY_FILE   = os.getenv("WAVEEDGE_NOTIFY_FILE", f"{HOME}/data/notifications.json")
TRADES_FILE   = os.getenv("WAVEEDGE_TRADES_FILE", f"{HOME}/data/trades.json")
LOG_FILE      = os.getenv("WAVEEDGE_BRAIN3_LOG", f"{HOME}/logs/brain3.log")
NEWS_FALLBACK = os.getenv("WAVEEDGE_NEWS_FILE", f"{HOME}/data/news_calendar.json")

PAIRS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X",
    "XAUUSD": "GC=F"   # NEW: gold, tracked for trend visibility only.
                       # NOT tradeable — webhook has no XAUUSD in its
                       # MARKET_MAP and will keep blocking any gold order
                       # regardless of what Brain 3 reports here. This
                       # gives you the same trend picture for gold that
                       # the FX pairs get, nothing more.
}

PIP_SIZES = {
    "EURUSD": 0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01,
    "AUDUSD": 0.0001, "USDCAD": 0.0001, "USDCHF": 0.0001,
    "NZDUSD": 0.0001,
    "XAUUSD": 0.01   # $0.01 tick. Distances shown as "pips" for gold are
                     # informational estimates, not a validated convention
                     # like the FX pairs — flagged, not fixed, since gold
                     # can't execute anyway.
}

ROUND_STEP = {
    "EURUSD": 0.01, "GBPUSD": 0.01, "USDJPY": 1.0,
    "AUDUSD": 0.01, "USDCAD": 0.01, "USDCHF": 0.01, "NZDUSD": 0.01,
    "XAUUSD": 25.0   # gold round levels cluster at $25/$50/$100, not cents
}

LEVEL_APPROACH_PIPS = 15


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── LOGGING ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = utcnow().isoformat()
    line = f"{ts} | BRAIN3 | {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def atomic_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".brain3-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


# ─── ALERT SYSTEM (log only, no email) ───────────────────────────────────────

def save_alert(alert_type, title, body):
    try:
        import fcntl
        os.makedirs(os.path.dirname(NOTIFY_FILE), exist_ok=True)
        with open(NOTIFY_FILE + ".lock", "a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            alerts = []
            if os.path.exists(NOTIFY_FILE):
                try:
                    with open(NOTIFY_FILE, "r", encoding="utf-8") as f:
                        alerts = json.load(f)
                except Exception:
                    alerts = []
            alerts.append({
                "time": utcnow().isoformat(),
                "time_human": utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                "type": alert_type, "title": title, "body": body
            })
            atomic_json(NOTIFY_FILE, alerts[-100:])
        log(f"ALERT [{alert_type}] {title}")
    except Exception as e:
        log(f"Alert save error: {e}")


# ─── DUAL NEWS SOURCE ─────────────────────────────────────────────────────────

def fetch_forexfactory():
    try:
        req = Request(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
        events = []
        for e in raw:
            impact = str(e.get("impact", "")).strip().lower()
            if impact not in ("high", "medium"):
                continue
            events.append({
                "title":   e.get("title", e.get("name", "Unknown")),
                "country": e.get("country", e.get("currency", "")),
                "impact":  impact.capitalize(),
                "date":    e.get("date", e.get("time", "")),
                "source":  "ForexFactory"
            })
        log(f"ForexFactory: {len(events)} med/high events OK")
        return events, True
    except Exception as e:
        log(f"ForexFactory FAILED: {e}")
        return [], False


def fetch_investing():
    try:
        import requests
        from bs4 import BeautifulSoup
        from datetime import date

        today   = date.today()
        date_to = today + timedelta(days=2)
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept":          "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type":    "application/x-www-form-urlencoded",
            "Referer":         "https://www.investing.com/economic-calendar/",
            "Origin":          "https://www.investing.com",
        }
        payload = {
            "country[]":    ["5", "22", "25", "32", "6", "35", "4", "12"],
            "importance[]": ["3"],
            "currentTab":   "custom",
            "dateFrom":     today.strftime("%Y-%m-%d"),
            "dateTo":       date_to.strftime("%Y-%m-%d"),
            "timeZone":     "8",
            "timeFilter":   "timeRemain",
            "limit_from":   "0"
        }
        resp = requests.post(
            "https://economic-calendar.investing.com/economic-calendar/"
            "Service/getCalendarFilteredData",
            headers=headers, data=payload, timeout=15
        )
        if resp.status_code != 200:
            log(f"Investing.com HTTP {resp.status_code} — skipping")
            return [], False
        html = resp.json().get("data", "")
        if not html:
            log("Investing.com empty response — skipping")
            return [], False
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr", {"class": lambda c: c and "js-event-item" in c})
        events = []
        for row in rows:
            try:
                event_cell   = row.find("td", {"class": "event"})
                country_cell = row.find("td", {"class": "flagCur"})
                impact_cell  = row.find("td", {"class": "sentiment"})
                if not event_cell or not impact_cell:
                    continue
                bulls = len(impact_cell.find_all("i", {"class": "grayFullBullishIcon"}))
                if bulls < 3:
                    continue
                events.append({
                    "title":   event_cell.get_text(strip=True),
                    "country": country_cell.get_text(strip=True)[:3].upper() if country_cell else "",
                    "impact":  "High",
                    "date":    row.get("data-event-datetime", ""),
                    "source":  "Investing.com"
                })
            except Exception:
                continue
        log(f"Investing.com: {len(events)} HIGH events OK")
        return events, True
    except ImportError:
        log("Investing.com: requests/bs4 not available — skipping")
        return [], False
    except Exception as e:
        log(f"Investing.com FAILED: {e} — skipping")
        return [], False


def fetch_news_fallback():
    try:
        if not os.path.exists(NEWS_FALLBACK):
            return [], False
        with open(NEWS_FALLBACK, "r") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            events = payload.get("events", payload.get("high_impact_today", []))
            checked = payload.get("checked_at")
            fresh = False
            if checked:
                try:
                    stamp = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
                    if stamp.tzinfo is not None:
                        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
                    fresh = utcnow() - stamp <= timedelta(hours=6)
                except Exception:
                    fresh = False
            return (events if isinstance(events, list) else []), fresh
        return (payload if isinstance(payload, list) else []), True
    except Exception:
        return [], False


def merge_news(ff_events, inv_events):
    def parse_dt(s):
        return parse_event_time(s)

    def word_set(t):
        return set(t.lower().split())

    merged = list(ff_events)
    for inv in inv_events:
        inv_dt = parse_dt(inv.get("date", ""))
        inv_w  = word_set(inv.get("title", ""))
        dup    = False
        for ff in ff_events:
            ff_dt = parse_dt(ff.get("date", ""))
            ff_w  = word_set(ff.get("title", ""))
            if inv_w and ff_w:
                overlap = len(inv_w & ff_w) / max(len(inv_w), len(ff_w), 1)
                if overlap > 0.4:
                    if inv_dt and ff_dt:
                        if abs((inv_dt - ff_dt).total_seconds()) <= 900:
                            dup = True
                            break
                    else:
                        dup = True
                        break
        if not dup:
            merged.append(inv)
    return merged


def fetch_all_news():
    ff, ff_ok = fetch_forexfactory()
    inv, inv_ok = fetch_investing()
    if ff_ok or inv_ok:
        merged = merge_news(ff, inv)
        log(f"News merged: {len(merged)} total ({len(ff)} FF + {len(inv)} Inv unique)")
        return merged, True
    log("Both live sources failed — using Brain 2 fallback file")
    return fetch_news_fallback()


# ─── NEWS BLOCK CHECK ─────────────────────────────────────────────────────────

def parse_event_time(s):
    if not s:
        return None
    value = str(s).strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(value[:19], fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


CURRENCY_ALIASES = {
    "US": "USD", "USA": "USD", "UNITED STATES": "USD",
    "EU": "EUR", "EMU": "EUR", "EURO ZONE": "EUR",
    "UK": "GBP", "GB": "GBP", "UNITED KINGDOM": "GBP",
    "JP": "JPY", "JAPAN": "JPY", "CA": "CAD", "CANADA": "CAD",
    "AU": "AUD", "AUSTRALIA": "AUD", "NZ": "NZD", "NEW ZEALAND": "NZD",
    "CH": "CHF", "SWITZERLAND": "CHF",
}


def event_currency(event):
    raw = str(event.get("country", event.get("currency", ""))).strip().upper()
    return CURRENCY_ALIASES.get(raw, raw[:3])


def event_affects_pair(event, pair):
    currency = event_currency(event)
    return not currency or currency in (pair[:3], pair[3:6])


def check_news_block(news_data):
    now          = utcnow()
    block_window = now + timedelta(minutes=45)
    post_window  = now - timedelta(minutes=15)
    warn_window  = now + timedelta(hours=2)
    block, upcoming, critical = False, [], []

    for e in (news_data or []):
        impact = str(e.get("impact", "")).strip().lower()
        if impact not in ("high", "medium"):
            continue
        evt_dt = parse_event_time(e.get("date", e.get("time", "")))
        if not evt_dt:
            continue
        if post_window <= evt_dt <= warn_window:
            mins = int((evt_dt - now).total_seconds() / 60)
            entry = {
                "title": e.get("title", "Unknown"),
                "country": e.get("country", e.get("currency", "")),
                "time_utc": evt_dt.strftime("%H:%M UTC"),
                "mins_away": mins,
                "impact": impact.upper(),
                "source": e.get("source", "")
            }
            upcoming.append(entry)
            if post_window <= evt_dt <= block_window and impact == "high":
                block = True
                critical.append(entry)
    return block, upcoming, critical


# ─── MARKET DATA ──────────────────────────────────────────────────────────────

def fetch_ohlcv(symbol, period, interval):
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        if df is None or df.empty:
            return None
        try:
            if df.index.tz is not None:
                df.index = df.index.tz_convert("UTC").tz_localize(None)
        except Exception:
            pass
        return df
    except Exception as e:
        log(f"OHLCV error {symbol}: {e}")
        return None


def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calc_adx(high, low, close, period=14):
    import pandas as pd
    import numpy as np
    up   = high - high.shift(1)
    down = low.shift(1) - low
    pdm  = pd.Series(np.where((up > down) & (up > 0), up, 0), index=high.index)
    ndm  = pd.Series(np.where((down > up) & (down > 0), down, 0), index=low.index)
    tr   = pd.concat([(high - low), (high - close.shift(1)).abs(),
                      (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr  = tr.ewm(span=period, adjust=False).mean()
    pdi  = 100 * pdm.ewm(span=period, adjust=False).mean() / (atr + 1e-10)
    ndi  = 100 * ndm.ewm(span=period, adjust=False).mean() / (atr + 1e-10)
    dx   = 100 * (pdi - ndi).abs() / (pdi + ndi + 1e-10)
    return dx.ewm(span=period, adjust=False).mean()


# ─── TREND ENGINE (NEW in v5) ─────────────────────────────────────────────────

def classify_trend(close_series):
    """
    UP       = price above EMA20 and EMA20 rising
    DOWN     = price below EMA20 and EMA20 falling
    SIDEWAYS = anything else (price/EMA disagree, or flat EMA)
    """
    try:
        if close_series is None or len(close_series) < 25:
            return "UNKNOWN"
        ema    = calc_ema(close_series, 20)
        c      = float(close_series.iloc[-1])
        e_now  = float(ema.iloc[-1])
        e_prev = float(ema.iloc[-4])
        rising  = e_now > e_prev
        falling = e_now < e_prev
        if c > e_now and rising:
            return "UP"
        if c < e_now and falling:
            return "DOWN"
        return "SIDEWAYS"
    except Exception:
        return "UNKNOWN"


def trend_alignment(t_h1, t_h4, t_d1):
    """Combine the three timeframes into one plain-English verdict."""
    ts = [t_h1, t_h4, t_d1]
    if all(t == "UP" for t in ts):
        return "UPTREND — ALL ALIGNED"
    if all(t == "DOWN" for t in ts):
        return "DOWNTREND — ALL ALIGNED"
    if t_d1 == "UP" and t_h1 == "DOWN":
        return "PULLBACK within UPTREND"
    if t_d1 == "DOWN" and t_h1 == "UP":
        return "PULLBACK within DOWNTREND"
    if t_d1 == "UP":
        return "UPTREND — partial (lower TFs mixed)"
    if t_d1 == "DOWN":
        return "DOWNTREND — partial (lower TFs mixed)"
    return "SIDEWAYS / CROSS-TREND"


def get_adx_regime_and_trends(symbol):
    """Single H1 fetch feeds ADX regime + H1 trend + H4 trend."""
    try:
        h1 = fetch_ohlcv(symbol, "30d", "1h")
        if h1 is None or len(h1) < 60:
            return None, "UNKNOWN", "UNKNOWN", "UNKNOWN"
        # Yahoo timestamps intraday bars at OPEN. Remove the still-forming H1,
        # then expose each [00:00,04:00) H4 candle only at its 04:00 close.
        if h1.index[-1] + timedelta(hours=1) > utcnow():
            h1 = h1.iloc[:-1]
        h4 = h1.resample("4h", label="right", closed="left").agg(
            {"High": "max", "Low": "min", "Close": "last"}
        ).dropna()
        h4 = h4[h4.index <= utcnow()]
        if len(h4) < 20:
            return None, "UNKNOWN", "UNKNOWN", "UNKNOWN"
        adx = calc_adx(h4["High"], h4["Low"], h4["Close"])
        v   = round(float(adx.iloc[-1]), 1)
        # Same 20 ADX boundary used by the historical engine.
        regime = "TRENDING" if v >= 20 else "WEAK_TREND" if v >= 15 else "RANGING"
        t_h1 = classify_trend(h1["Close"])
        t_h4 = classify_trend(h4["Close"])
        return v, regime, t_h1, t_h4
    except Exception as e:
        log(f"ADX/trend error {symbol}: {e}")
        return None, "UNKNOWN", "UNKNOWN", "UNKNOWN"


def get_d1_bias_and_daily(symbol):
    d1 = fetch_ohlcv(symbol, "3mo", "1d")
    if d1 is None or len(d1) < 25:
        return "NEUTRAL", None
    try:
        # The current calendar day's D1 candle is incomplete and cannot be the
        # higher-timeframe authority.
        if d1.index[-1].date() >= utcnow().date():
            d1 = d1.iloc[:-1]
        if len(d1) < 25:
            return "NEUTRAL", None
        ema   = calc_ema(d1["Close"], 20)
        close = float(d1["Close"].iloc[-1])
        e20   = float(ema.iloc[-1])
        if abs(close - e20) / e20 < 0.0003:
            return "NEUTRAL", d1
        return ("BULL" if close > e20 else "BEAR"), d1
    except Exception:
        return "NEUTRAL", d1


def get_recent_wr(pair_name, lookback=15):
    try:
        if not os.path.exists(TRADES_FILE):
            return 50.0, 0, 0
        with open(TRADES_FILE, "r") as f:
            trades = json.load(f)
        executed = [
            t for t in trades
            if t.get("symbol") == pair_name
            and t.get("status") == "executed"
        ]
        groups = {}
        for trade in executed:
            key = trade.get("signal_id") or f"legacy:{trade.get('timestamp')}:{len(groups)}"
            groups.setdefault(key, []).append(trade)
        races = []
        for group in groups.values():
            expected = max(int(t.get("race_size", 1) or 1) for t in group)
            if len(group) < expected or any(
                    str(t.get("result", "")).upper() not in ("WIN", "LOSS")
                    for t in group):
                continue
            timestamp = min(str(t.get("timestamp", "")) for t in group)
            pnl = 0.0
            for trade in group:
                if isinstance(trade.get("realized_pnl"), (int, float)):
                    pnl += float(trade["realized_pnl"])
                else:
                    pnl += float(trade.get("risk_dollars") or 0.0) * float(trade.get("outcome_r") or 0.0)
            races.append((timestamp, pnl))
        recent = sorted(races)[-lookback:]
        if not recent:
            return 50.0, 0, 0
        wins = sum(1 for _, pnl in recent if pnl > 0)
        return round(wins / len(recent) * 100, 1), wins, len(recent) - wins
    except Exception:
        return 50.0, 0, 0


def score_adjustment(wr, regime):
    adj, reasons = 0, []
    if wr < 30:
        adj += 2; reasons.append(f"poor WR ({wr:.0f}%)")
    elif wr < 40:
        adj += 1; reasons.append(f"below-avg WR ({wr:.0f}%)")
    if regime == "RANGING":
        adj += 2; reasons.append("market RANGING")
    elif regime == "WEAK_TREND":
        adj += 1; reasons.append("weak trend")
    return adj, (", ".join(reasons) or "conditions normal")


def trade_permission(t_h1, t_h4, t_d1, regime):
    """D1+H4 command direction; H1 may confirm an early reversal."""
    if "UNKNOWN" in (t_h1, t_h4, t_d1):
        return "WAIT", "missing closed-candle trend data"
    if t_d1 == "UP" and t_h4 == "UP":
        return "BUY_ONLY", "D1 and H4 aligned up"
    if t_d1 == "DOWN" and t_h4 == "DOWN":
        return "SELL_ONLY", "D1 and H4 aligned down"
    # Confirmed lower-timeframe reversal may switch before the slower D1 EMA.
    if t_h4 == "UP" and t_h1 == "UP" and t_d1 == "DOWN" and regime != "RANGING":
        return "BUY_ONLY", "confirmed H1+H4 bullish reversal"
    if t_h4 == "DOWN" and t_h1 == "DOWN" and t_d1 == "UP" and regime != "RANGING":
        return "SELL_ONLY", "confirmed H1+H4 bearish reversal"
    return "BOTH", "sideways/cross-timeframe market"


# ─── LIQUIDITY MAP (from v4) ─────────────────────────────────────────────────

def build_liquidity_map(pair_name, d1, bias):
    if d1 is None or len(d1) < 10:
        return None
    try:
        pip  = PIP_SIZES.get(pair_name, 0.0001)
        step = ROUND_STEP.get(pair_name, 0.01)

        price      = float(d1["Close"].iloc[-1])
        today_high = float(d1["High"].iloc[-1])
        today_low  = float(d1["Low"].iloc[-1])

        pdh = float(d1["High"].iloc[-2])
        pdl = float(d1["Low"].iloc[-2])

        wk  = d1.resample("W", label="right", closed="right").agg(
            {"High": "max", "Low": "min"}).dropna()
        pwh = float(wk["High"].iloc[-2]) if len(wk) >= 2 else None
        pwl = float(wk["Low"].iloc[-2])  if len(wk) >= 2 else None

        cw = d1.iloc[-min(5, len(d1)):]
        cur_wk_high = float(cw["High"].max())
        cur_wk_low  = float(cw["Low"].min())

        ext  = d1.iloc[-min(20, len(d1)):]
        hi20 = float(ext["High"].max())
        lo20 = float(ext["Low"].min())

        rn_above = math.ceil(price / step) * step
        if rn_above <= price:
            rn_above += step
        rn_below = math.floor(price / step) * step
        if rn_below >= price:
            rn_below -= step

        def lvl(name, value, mitigated):
            if value is None:
                return None
            return {
                "name": name,
                "level": round(value, 5 if pip == 0.0001 else 3),
                "pips": round(abs(value - price) / pip, 1),
                "status": "mitigated" if mitigated else "unmitigated"
            }

        candidates_above = [
            lvl("PDH", pdh, today_high >= pdh),
            lvl("PWH", pwh, pwh is not None and cur_wk_high >= pwh),
            lvl("RN",  rn_above, False),
            lvl("20D_HIGH", hi20, False if hi20 > today_high else True),
        ]
        candidates_below = [
            lvl("PDL", pdl, today_low <= pdl),
            lvl("PWL", pwl, pwl is not None and cur_wk_low <= pwl),
            lvl("RN",  rn_below, False),
            lvl("20D_LOW", lo20, False if lo20 < today_low else True),
        ]

        above = sorted(
            [l for l in candidates_above if l and l["level"] > price],
            key=lambda x: x["level"])
        below = sorted(
            [l for l in candidates_below if l and l["level"] < price],
            key=lambda x: -x["level"])

        projection = None
        if bias in ("BULL", "BEAR"):
            with_bias = above if bias == "BULL" else below
            against   = below if bias == "BULL" else above
            targets   = [l for l in with_bias if l["status"] == "unmitigated"]
            ladder    = (targets + [l for l in with_bias if l["status"] == "mitigated"])[:3]
            breaking  = targets[0] if targets else (with_bias[0] if with_bias else None)
            reversal  = against[0] if against else None
            projection = {
                "direction": bias,
                "tp_ladder": ladder,
                "breaking_point": breaking,
                "reversal_point": reversal
            }

        return {
            "price": round(price, 5 if pip == 0.0001 else 3),
            "above": above,
            "below": below,
            "projection": projection
        }
    except Exception as e:
        log(f"Liquidity map error {pair_name}: {e}")
        return None


# ─── CHANGE DETECTION + ALERTING ──────────────────────────────────────────────

def load_prev():
    try:
        if os.path.exists(PREV_CONTEXT):
            with open(PREV_CONTEXT, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def detect_and_alert(prev, new):
    if prev is None:
        return

    if new.get("block_all") and not prev.get("block_all"):
        critical = new.get("critical_events", [])
        if critical:
            lines = ["Trades PAUSED — high-impact news incoming:\n"]
            for e in critical:
                lines.append(f"  {e['impact']} | {e['country']} | {e['title']} — {e['time_utc']} ({e['mins_away']} min)")
            save_alert("NEWS_BLOCK", "NEWS BLOCK ACTIVATED — trades paused", "\n".join(lines))
        else:
            save_alert("NEWS_DATA_OFFLINE", "Calendar unavailable — trades paused",
                       "Both live news sources and the fresh local cache are unavailable.")

    if prev.get("block_all") and not new.get("block_all"):
        save_alert("NEWS_CLEAR", "News block cleared — trading resumed",
                   "All high-impact events passed. Robot accepting signals again.")

    prev_titles = {e.get("title", "") for e in prev.get("news_events_upcoming", [])}
    for e in new.get("news_events_upcoming", []):
        if (e.get("impact") == "HIGH"
                and e.get("title") not in prev_titles
                and e.get("mins_away", 999) <= 90):
            save_alert(
                "NEWS_WARN",
                f"HIGH IMPACT: {e['title']} in {e['mins_away']} min",
                f"Event:  {e['title']} ({e['country']})\n"
                f"Time:   {e['time_utc']} ({e['mins_away']} min away)\n"
                f"Source: {e.get('source', 'unknown')}\n"
                f"Brain 3 will block trades within 45 min of this event."
            )

    for pair, ctx in new.get("pair_context", {}).items():
        prev_ctx = prev.get("pair_context", {}).get(pair, {})

        if ctx.get("news_block") and not prev_ctx.get("news_block"):
            save_alert("PAIR_NEWS_BLOCK", f"{pair} paused for related high-impact news",
                       ctx.get("news_block_reason", "High-impact news window"))
        if not ctx.get("news_block") and prev_ctx.get("news_block"):
            save_alert("PAIR_NEWS_CLEAR", f"{pair} news window cleared",
                       f"{pair} may trade again subject to direction and score rules.")

        if ctx.get("block") and not prev_ctx.get("block"):
            save_alert(
                "PAIR_BLOCK",
                f"{pair} BLOCKED — {ctx.get('block_reason', 'conditions unfavourable')}",
                f"ADX: {ctx.get('adx_4h')} ({ctx.get('regime')}) | D1: {ctx.get('bias_d1')} | WR: {ctx.get('recent_wr')}%"
            )
        if not ctx.get("block") and prev_ctx.get("block"):
            save_alert("PAIR_CLEAR", f"{pair} unblocked — conditions improved",
                       f"{pair} is accepting signals again.")

        # LEVEL_APPROACH (from v4)
        lm      = ctx.get("liquidity_map") or {}
        proj    = lm.get("projection") or {}
        bp      = proj.get("breaking_point") or {}
        prev_lm = prev_ctx.get("liquidity_map") or {}
        prev_bp = (prev_lm.get("projection") or {}).get("breaking_point") or {}
        pips_now  = bp.get("pips", 9999)
        pips_prev = prev_bp.get("pips", 9999)
        if pips_now <= LEVEL_APPROACH_PIPS < pips_prev and bp.get("name"):
            save_alert(
                "LEVEL_APPROACH",
                f"{pair} approaching breaking point {bp['name']} @ {bp['level']} ({pips_now} pips)",
                f"Pair:   {pair}\n"
                f"Price:  {lm.get('price')}\n"
                f"Level:  {bp['name']} at {bp['level']} — {pips_now} pips away\n"
                f"Bias:   {proj.get('direction')}\n"
                f"Break-and-close beyond = continuation toward next target.\n"
                f"Wick-through-and-close-back = sweep/reversal."
            )

        # TREND_CHANGE (NEW in v5)
        new_state  = ctx.get("trend_state", "")
        prev_state = prev_ctx.get("trend_state", "")
        if prev_state and new_state and new_state != prev_state:
            save_alert(
                "TREND_CHANGE",
                f"{pair} trend shift: {prev_state} -> {new_state}",
                f"Pair: {pair}\n"
                f"Was:  {prev_state}\n"
                f"Now:  {new_state}\n"
                f"H1:{ctx.get('trend_h1')} H4:{ctx.get('trend_h4')} D1:{ctx.get('trend_d1')}\n"
                f"ADX:  {ctx.get('adx_4h')} ({ctx.get('regime')})"
            )


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def build_context():
    log("Building market context...")
    now      = utcnow()
    prev_ctx = load_prev()

    news, news_available = fetch_all_news()
    block, upcoming, critical = check_news_block(news)
    if not news_available:
        log("NEWS DATA UNAVAILABLE: fail-closed global block")
    elif block:
        log(f"NEWS BLOCK: {[e['title'] for e in critical]}")
    else:
        log(f"News clear — {len(upcoming)} events upcoming (none blocking now)")

    pair_context = {}
    for pair_name, symbol in PAIRS.items():
        adx_val, regime, t_h1, t_h4 = get_adx_regime_and_trends(symbol)
        bias, d1         = get_d1_bias_and_daily(symbol)
        t_d1             = classify_trend(d1["Close"]) if d1 is not None else "UNKNOWN"
        t_state          = trend_alignment(t_h1, t_h4, t_d1)
        permission, permission_reason = trade_permission(t_h1, t_h4, t_d1, regime)
        wr, wins, losses = get_recent_wr(pair_name)
        adj, reason      = score_adjustment(wr, regime)
        liq_map          = build_liquidity_map(pair_name, d1, bias)

        pair_critical = [event for event in critical if event_affects_pair(event, pair_name)]
        news_block = bool(pair_critical)
        pair_block   = (regime == "RANGING" and wr < 30) or permission == "WAIT"
        block_reason = f"ranging market + poor performance ({wr:.0f}% WR)" if pair_block else ""
        if permission == "WAIT":
            block_reason = permission_reason

        pair_context[pair_name] = {
            "adx_4h": adx_val, "regime": regime, "bias_d1": bias,
            "trend_h1": t_h1, "trend_h4": t_h4, "trend_d1": t_d1,
            "trend_state": t_state,
            "trade_permission": permission,
            "permission_reason": permission_reason,
            "recent_wr": wr, "recent_wins": wins, "recent_losses": losses,
            "score_adjustment": adj, "reason": reason,
            "block": pair_block, "block_reason": block_reason,
            "news_block": news_block,
            "news_block_reason": (f"High-impact {event_currency(pair_critical[0])} news: "
                                  f"{pair_critical[0]['title']}" if pair_critical else ""),
            "liquidity_map": liq_map
        }
        status = "BLOCKED" if pair_block else f"+{adj}" if adj else "CLEAR"
        log(f"{pair_name}: ADX={adx_val} {regime} | D1:{bias} | "
            f"trend[H1:{t_h1} H4:{t_h4} D1:{t_d1}] -> {permission} | WR:{wr:.0f}% | {status}")

    context = {
        "updated":              now.isoformat(),
        "updated_human":        now.strftime("%Y-%m-%d %H:%M UTC"),
        "news_status":          "AVAILABLE" if news_available else "UNAVAILABLE",
        "news_block_active":    block,
        "news_events_upcoming": upcoming,
        "critical_events":      critical,
        "pair_context":         pair_context,
        "system_mode":          "NEWS_OFFLINE" if not news_available else
                                "NEWS_BLOCK" if block else "NORMAL",
        # High-impact events are pair/currency specific. Only a news-data
        # outage blocks every pair.
        "block_all":            not news_available
    }

    atomic_json(CONTEXT_FILE, context)

    detect_and_alert(prev_ctx, context)

    atomic_json(PREV_CONTEXT, context)

    log(f"Context saved — mode: {context['system_mode']}")
    return context


def print_summary(ctx):
    print("\n" + "=" * 58)
    print("  WAVEEDGE BRAIN 3 v7 — DIRECTION + NEWS + LIQUIDITY")
    print(f"  {ctx['updated_human']}")
    print("=" * 58)
    print(f"  Mode       : {ctx['system_mode']}")
    print(f"  News Block : {'YES' if ctx['news_block_active'] else 'No'}")
    upcoming = ctx.get("news_events_upcoming", [])
    if upcoming:
        print(f"  Upcoming ({len(upcoming)}):")
        for e in upcoming[:5]:
            icon = "[H]" if e["impact"] == "HIGH" else "[M]"
            print(f"    {icon} {e['mins_away']:3}min | {e['country']:3} | {e['title'][:38]}")
    print("-" * 58)
    print("  TREND ENGINE (H1 / H4 / D1 -> verdict):")
    for pair, p in ctx["pair_context"].items():
        print(f"  {pair:8}| {p.get('trend_h1','?'):8}| {p.get('trend_h4','?'):8}| "
              f"{p.get('trend_d1','?'):8}| {p.get('trade_permission','WAIT')}")
    print("-" * 58)
    print("  PAIR INTELLIGENCE:")
    for pair, p in ctx["pair_context"].items():
        status = "BLOCKED" if p["block"] else f"+{p['score_adjustment']}" if p["score_adjustment"] else "CLEAR"
        print(f"  {pair:8}| {p['regime']:11}| D1:{p['bias_d1']:4}| WR:{p['recent_wr']:4.0f}% | {status}")
    print("-" * 58)
    print("  WEEKLY PROJECTIONS (break -> targets | defense):")
    for pair, p in ctx["pair_context"].items():
        lm = p.get("liquidity_map")
        if not lm or not lm.get("projection"):
            print(f"  {pair:8}| NEUTRAL bias — no directional projection")
            continue
        proj   = lm["projection"]
        ladder = proj.get("tp_ladder", [])
        bp     = proj.get("breaking_point")
        rp     = proj.get("reversal_point")
        tps    = " -> ".join(f"{t['name']}@{t['level']}" for t in ladder) or "none"
        bp_txt = f"{bp['name']}@{bp['level']} ({bp['pips']}p)" if bp else "n/a"
        rp_txt = f"{rp['name']}@{rp['level']} ({rp['pips']}p)" if rp else "n/a"
        print(f"  {pair:8}| {proj['direction']:4} | break: {bp_txt}")
        print(f"          | TPs: {tps}")
        print(f"          | defense/reversal: {rp_txt}")
    print("=" * 58)


if __name__ == "__main__":
    ctx = build_context()
    print_summary(ctx)
