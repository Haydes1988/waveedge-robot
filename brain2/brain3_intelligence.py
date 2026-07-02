#!/usr/bin/env python3
"""
WaveEdge Brain 3 v4 — Market Intelligence Hub + Liquidity Map
Runs every 15 minutes via cron.
NEW in v4:
  - Liquidity map per pair: PDH/PDL, PWH/PWL, round numbers, 20-day extremes
  - Mitigation tracking (swept levels drop off the target list)
  - Weekly projection: TP1/TP2/TP3 ladder in bias direction
  - Breaking point + reversal point classification
  - LEVEL_APPROACH alerts when price nears the breaking point
Dual news source: ForexFactory (primary), Investing.com (secondary, may fail on VPS DNS).
No email — alerts saved to notifications.json + brain3.log.
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request

# ─── CONFIG ───────────────────────────────────────────────────────────────────
CONTEXT_FILE  = "/root/waveedge/data/market_context.json"
PREV_CONTEXT  = "/root/waveedge/data/market_context_prev.json"
NOTIFY_FILE   = "/root/waveedge/data/notifications.json"
TRADES_FILE   = "/root/waveedge/data/trades.json"
LOG_FILE      = "/root/waveedge/logs/brain3.log"
NEWS_FALLBACK = "/root/waveedge/data/news_calendar.json"

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

# Big-figure step per pair (round-number granularity)
ROUND_STEP = {
    "EURUSD": 0.01, "GBPUSD": 0.01, "USDJPY": 1.0,
    "AUDUSD": 0.01, "USDCAD": 0.01, "USDCHF": 0.01, "NZDUSD": 0.01
}

LEVEL_APPROACH_PIPS = 15   # alert when price within this of the breaking point


def utcnow():
    """Timezone-clean UTC now (kills the DeprecationWarning spam)."""
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


# ─── ALERT SYSTEM (log only, no email) ───────────────────────────────────────

def save_alert(alert_type, title, body):
    try:
        alerts = []
        if os.path.exists(NOTIFY_FILE):
            with open(NOTIFY_FILE, "r") as f:
                try:
                    alerts = json.load(f)
                except Exception:
                    alerts = []
        alerts.append({
            "time": utcnow().isoformat(),
            "time_human": utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "type": alert_type,
            "title": title,
            "body": body
        })
        alerts = alerts[-50:]
        with open(NOTIFY_FILE, "w") as f:
            json.dump(alerts, f, indent=2)
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
        return events
    except Exception as e:
        log(f"ForexFactory FAILED: {e}")
        return []


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
            return []
        html = resp.json().get("data", "")
        if not html:
            log("Investing.com empty response — skipping")
            return []
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
        return events
    except ImportError:
        log("Investing.com: requests/bs4 not available — skipping")
        return []
    except Exception as e:
        log(f"Investing.com FAILED: {e} — skipping")
        return []


def fetch_news_fallback():
    try:
        if not os.path.exists(NEWS_FALLBACK):
            return []
        with open(NEWS_FALLBACK, "r") as f:
            return json.load(f)
    except Exception:
        return []


def merge_news(ff_events, inv_events):
    def parse_dt(s):
        if not s:
            return None
        try:
            s = s.strip()
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            tz = s[19:].replace(":", "")
            if tz and tz not in ("Z", "+0000", "-0000", ""):
                sign = 1 if "+" in s[19:] else -1
                tz2  = tz.lstrip("+-")
                if len(tz2) >= 4:
                    dt -= sign * timedelta(hours=int(tz2[:2]), minutes=int(tz2[2:4]))
            return dt
        except Exception:
            return None

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
    ff  = fetch_forexfactory()
    inv = fetch_investing()
    if ff or inv:
        merged = merge_news(ff, inv)
        log(f"News merged: {len(merged)} total ({len(ff)} FF + {len(inv)} Inv unique)")
        return merged
    log("Both live sources failed — using Brain 2 fallback file")
    return fetch_news_fallback()


# ─── NEWS BLOCK CHECK ─────────────────────────────────────────────────────────

def parse_event_time(s):
    if not s:
        return None
    try:
        s  = s.strip()
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        tz = s[19:].replace(":", "")
        if tz and tz not in ("Z", "+0000", "-0000", ""):
            sign = 1 if "+" in s[19:] else -1
            tz2  = tz.lstrip("+-")
            if len(tz2) >= 4:
                dt -= sign * timedelta(hours=int(tz2[:2]), minutes=int(tz2[2:4]))
        return dt
    except Exception:
        return None


def check_news_block(news_data):
    now          = utcnow()
    block_window = now + timedelta(minutes=45)
    warn_window  = now + timedelta(hours=2)
    block, upcoming, critical = False, [], []

    for e in (news_data or []):
        impact = str(e.get("impact", "")).strip().lower()
        if impact not in ("high", "medium"):
            continue
        evt_dt = parse_event_time(e.get("date", e.get("time", "")))
        if not evt_dt:
            continue
        if now <= evt_dt <= warn_window:
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
            if evt_dt <= block_window and impact == "high":
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
                df.index = df.index.tz_localize(None)
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


def get_adx_regime(symbol):
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
        v   = round(float(adx.iloc[-1]), 1)
        return v, "TRENDING" if v >= 25 else "WEAK_TREND" if v >= 18 else "RANGING"
    except Exception as e:
        log(f"ADX error {symbol}: {e}")
        return None, "UNKNOWN"


def get_d1_bias_and_daily(symbol):
    """Returns (bias, d1_dataframe) so the liquidity map can reuse the data."""
    d1 = fetch_ohlcv(symbol, "3mo", "1d")
    if d1 is None or len(d1) < 25:
        return "NEUTRAL", None
    try:
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
            and ("WIN" in str(t.get("result", "")).upper()
                 or "LOSS" in str(t.get("result", "")).upper())
        ]
        recent = executed[-lookback:]
        if not recent:
            return 50.0, 0, 0
        wins = sum(1 for t in recent if "WIN" in str(t.get("result", "")).upper())
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


# ─── LIQUIDITY MAP (NEW in v4) ────────────────────────────────────────────────

def build_liquidity_map(pair_name, d1, bias):
    """
    Builds the per-pair liquidity map from daily data:
      PDH/PDL, PWH/PWL, next round numbers, 20-day extremes.
    Marks mitigation, classifies breaking point + reversal point,
    projects a TP1->TP3 ladder in the bias direction.
    """
    if d1 is None or len(d1) < 10:
        return None
    try:
        pip  = PIP_SIZES.get(pair_name, 0.0001)
        step = ROUND_STEP.get(pair_name, 0.01)

        price = float(d1["Close"].iloc[-1])
        today_high = float(d1["High"].iloc[-1])
        today_low  = float(d1["Low"].iloc[-1])

        # Previous day (last COMPLETED bar)
        pdh = float(d1["High"].iloc[-2])
        pdl = float(d1["Low"].iloc[-2])

        # Previous week via weekly resample
        wk  = d1.resample("W", label="right", closed="right").agg(
            {"High": "max", "Low": "min"}).dropna()
        pwh = float(wk["High"].iloc[-2]) if len(wk) >= 2 else None
        pwl = float(wk["Low"].iloc[-2])  if len(wk) >= 2 else None

        # Current week's range so far (rough: last 5 daily bars)
        cw = d1.iloc[-min(5, len(d1)):]
        cur_wk_high = float(cw["High"].max())
        cur_wk_low  = float(cw["Low"].min())

        # 20-day extremes = bigger external liquidity
        ext = d1.iloc[-min(20, len(d1)):]
        hi20 = float(ext["High"].max())
        lo20 = float(ext["Low"].min())

        # Next round numbers
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

        # Mitigation: has price already traded through it recently?
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

        # Projection only when we have a directional bias
        projection = None
        if bias in ("BULL", "BEAR"):
            with_bias   = above if bias == "BULL" else below
            against     = below if bias == "BULL" else above
            targets     = [l for l in with_bias if l["status"] == "unmitigated"]
            # pad ladder with mitigated levels if fewer than 3 clean targets
            ladder = (targets + [l for l in with_bias if l["status"] == "mitigated"])[:3]
            breaking = targets[0] if targets else (with_bias[0] if with_bias else None)
            reversal = against[0] if against else None
            projection = {
                "direction": bias,
                "tp_ladder": ladder,           # TP1, TP2, TP3 candidates in order
                "breaking_point": breaking,    # first unmitigated with-bias level
                "reversal_point": reversal     # nearest against-bias defense level
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
        lines    = ["Trades PAUSED — high-impact news incoming:\n"]
        for e in critical:
            lines.append(f"  {e['impact']} | {e['country']} | {e['title']} — {e['time_utc']} ({e['mins_away']} min)")
        save_alert("NEWS_BLOCK", "NEWS BLOCK ACTIVATED — trades paused", "\n".join(lines))

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
        if ctx.get("block") and not prev_ctx.get("block"):
            save_alert(
                "PAIR_BLOCK",
                f"{pair} BLOCKED — {ctx.get('block_reason', 'conditions unfavourable')}",
                f"ADX: {ctx.get('adx_4h')} ({ctx.get('regime')}) | D1: {ctx.get('bias_d1')} | WR: {ctx.get('recent_wr')}%"
            )
        if not ctx.get("block") and prev_ctx.get("block"):
            save_alert("PAIR_CLEAR", f"{pair} unblocked — conditions improved",
                       f"{pair} is accepting signals again.")

        # NEW: LEVEL_APPROACH — price closing in on the breaking point
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
                f"If price breaks and CLOSES beyond this level, expect continuation\n"
                f"toward the next target. A wick-through-and-close-back = sweep/reversal."
            )


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def build_context():
    log("Building market context...")
    now      = utcnow()
    prev_ctx = load_prev()

    news = fetch_all_news()
    block, upcoming, critical = check_news_block(news)
    if block:
        log(f"NEWS BLOCK: {[e['title'] for e in critical]}")
    else:
        log(f"News clear — {len(upcoming)} events upcoming (none blocking now)")

    pair_context = {}
    for pair_name, symbol in PAIRS.items():
        adx_val, regime  = get_adx_regime(symbol)
        bias, d1         = get_d1_bias_and_daily(symbol)
        wr, wins, losses = get_recent_wr(pair_name)
        adj, reason      = score_adjustment(wr, regime)
        liq_map          = build_liquidity_map(pair_name, d1, bias)

        pair_block   = (regime == "RANGING" and wr < 30)
        block_reason = f"ranging market + poor performance ({wr:.0f}% WR)" if pair_block else ""

        pair_context[pair_name] = {
            "adx_4h": adx_val, "regime": regime, "bias_d1": bias,
            "recent_wr": wr, "recent_wins": wins, "recent_losses": losses,
            "score_adjustment": adj, "reason": reason,
            "block": pair_block, "block_reason": block_reason,
            "liquidity_map": liq_map
        }
        status = "BLOCKED" if pair_block else f"+{adj}" if adj else "CLEAR"
        bp_txt = ""
        if liq_map and liq_map.get("projection") and liq_map["projection"].get("breaking_point"):
            bp = liq_map["projection"]["breaking_point"]
            bp_txt = f" | break: {bp['name']}@{bp['level']} ({bp['pips']}p)"
        log(f"{pair_name}: ADX={adx_val} {regime} | D1:{bias} | WR:{wr:.0f}% | {status}{bp_txt}")

    context = {
        "updated":              now.isoformat(),
        "updated_human":        now.strftime("%Y-%m-%d %H:%M UTC"),
        "news_block_active":    block,
        "news_events_upcoming": upcoming,
        "critical_events":      critical,
        "pair_context":         pair_context,
        "system_mode":          "NEWS_BLOCK" if block else "NORMAL",
        "block_all":            block
    }

    os.makedirs(os.path.dirname(CONTEXT_FILE), exist_ok=True)
    with open(CONTEXT_FILE, "w") as f:
        json.dump(context, f, indent=2)

    detect_and_alert(prev_ctx, context)

    with open(PREV_CONTEXT, "w") as f:
        json.dump(context, f, indent=2)

    log(f"Context saved — mode: {context['system_mode']}")
    return context


def print_summary(ctx):
    print("\n" + "=" * 58)
    print("  WAVEEDGE BRAIN 3 v4 — INTELLIGENCE + LIQUIDITY MAP")
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
