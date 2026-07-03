#!/usr/bin/env python3
"""
WaveEdge Loss Forensics — the Sunday Improvement Engine
Runs every Sunday 23:30 UTC (after the 23:00 backtest) via cron.

WHAT IT DOES:
  1. Loads the newest backtest results JSON.
  2. AUTOPSIES every loss: TOO_TIGHT_STOP / WRONG_DIRECTION / CHOP.
  3. Aggregates WR by session, hour, day, score band, key level, pair.
  4. Flags weak patterns — but a flag must appear TWO SUNDAYS RUNNING
     before it's stamped ACTIONABLE. One week = WATCH. This is the
     overfitting guard, enforced by code, not willpower.
  5. Saves report to /root/waveedge/reports/loss_forensics_<date>.txt

RULE (non-negotiable): maximum ONE strategy change per week, and only
from the ACTIONABLE list. Log every change in /root/waveedge/CHANGELOG.md.
"""

import glob
import json
import os
from datetime import datetime, timezone

REPORT_DIR = "/root/waveedge/reports"
FLAGS_FILE = "/root/waveedge/data/forensics_flags.json"
LOG_FILE   = "/root/waveedge/logs/forensics.log"

YF_SYMBOLS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X"
}
PIP_SIZES = {
    "EURUSD": 0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01,
    "AUDUSD": 0.0001, "USDCAD": 0.0001, "USDCHF": 0.0001, "NZDUSD": 0.0001
}
STRAT_INTERVAL = {"Scalp": "5m", "Day": "15m", "Swing": "1h"}

SESSIONS = [
    ("ASIA",       0, 7),
    ("LONDON",     7, 12),
    ("NY_OVERLAP", 12, 16),
    ("NY_LATE",    16, 21),
    ("ROLLOVER",   21, 24),
]


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(msg):
    ts = utcnow().isoformat()
    line = f"{ts} | FORENSICS | {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def fetch(symbol, interval):
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="14d", interval=interval)
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


def load_latest_results():
    files = sorted(glob.glob(f"{REPORT_DIR}/full_results_*.json"))
    if not files:
        return None, None
    path = files[-1]
    try:
        with open(path) as f:
            return json.load(f), path
    except Exception as e:
        log(f"Results load error: {e}")
        return None, None


def session_of(hour):
    for name, lo, hi in SESSIONS:
        if lo <= hour < hi:
            return name
    return "ROLLOVER"


def score_band(s):
    if s >= 11:
        return "11+"
    if s >= 9:
        return "9-10"
    if s >= 7:
        return "7-8"
    return "5-6"


def find_bar_index(df, tstr):
    """Locate the trade's bar; None if nothing within 3 bars' distance."""
    try:
        import pandas as pd
        ts = pd.Timestamp(tstr)
        idx = df.index.get_indexer([ts], method="nearest")[0]
        if idx < 0:
            return None
        gap = abs((df.index[idx] - ts).total_seconds())
        step = (df.index[1] - df.index[0]).total_seconds() if len(df) > 1 else 300
        if gap > step * 3:
            return None
        return int(idx)
    except Exception:
        return None


def classify_loss(df, i, entry, sl, tp, direction):
    """
    After the SL was hit, what did price actually do?
      TOO_TIGHT_STOP  -> reached our TP within 60 bars after stop-out
      WRONG_DIRECTION -> ran another 1R beyond the SL
      CHOP            -> neither; churn
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return "UNKNOWN"
    j_hit = None
    for j in range(i + 1, min(i + 41, len(df))):
        c = df.iloc[j]
        if direction == "BUY" and float(c["Low"]) <= sl:
            j_hit = j
            break
        if direction == "SELL" and float(c["High"]) >= sl:
            j_hit = j
            break
    if j_hit is None:
        return "UNKNOWN"
    seg = df.iloc[j_hit + 1: min(j_hit + 61, len(df))]
    if len(seg) == 0:
        return "UNKNOWN"
    if direction == "BUY":
        if float(seg["High"].max()) >= tp:
            return "TOO_TIGHT_STOP"
        if float(seg["Low"].min()) <= sl - risk:
            return "WRONG_DIRECTION"
    else:
        if float(seg["Low"].min()) <= tp:
            return "TOO_TIGHT_STOP"
        if float(seg["High"].max()) >= sl + risk:
            return "WRONG_DIRECTION"
    return "CHOP"


def wr_of(trades):
    if not trades:
        return 0.0, 0
    w = sum(1 for t in trades if t["result"] == "WIN")
    return round(w / len(trades) * 100, 1), len(trades)


def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    results, path = load_latest_results()
    if not results:
        log("No backtest results found — run the backtest first")
        return
    log(f"Loaded {len(results)} trades from {os.path.basename(path)}")

    # Enrich each trade with time features
    for t in results:
        try:
            ts = datetime.strptime(t["time"], "%Y-%m-%d %H:%M")
            t["_hour"]    = ts.hour
            t["_session"] = session_of(ts.hour)
            t["_dow"]     = ts.strftime("%a")
        except Exception:
            t["_hour"], t["_session"], t["_dow"] = -1, "UNKNOWN", "UNK"
        t["_band"] = score_band(int(t.get("score", 0)))

    overall_wr, total = wr_of(results)
    losses = [t for t in results if t["result"] == "LOSS"]

    # ── Loss autopsy (needs entry/sl/tp — present from backtest v4.1 onward)
    autopsy = {"TOO_TIGHT_STOP": 0, "WRONG_DIRECTION": 0, "CHOP": 0, "UNKNOWN": 0}
    autopsy_by_strat = {}
    have_prices = all(k in losses[0] for k in ("entry", "sl", "tp")) if losses else False

    if have_prices:
        cache = {}
        classified = 0
        for t in losses:
            pair, strat = t["pair"], t["strategy"]
            key = (pair, strat)
            if key not in cache:
                interval = STRAT_INTERVAL.get(strat, "5m")
                cache[key] = fetch(YF_SYMBOLS.get(pair, ""), interval)
            df = cache[key]
            if df is None:
                cls = "UNKNOWN"
            else:
                i = find_bar_index(df, t["time"])
                cls = "UNKNOWN" if i is None else classify_loss(
                    df, i, float(t["entry"]), float(t["sl"]),
                    float(t["tp"]), t["direction"])
            t["_loss_class"] = cls
            autopsy[cls] += 1
            autopsy_by_strat.setdefault(strat, {"TOO_TIGHT_STOP": 0, "WRONG_DIRECTION": 0,
                                                "CHOP": 0, "UNKNOWN": 0})[cls] += 1
            classified += 1
        log(f"Autopsied {classified} losses")
    else:
        log("Results JSON lacks entry/sl/tp — deploy backtest v4.1; autopsy will run next week")

    # ── Aggregations
    def table(dim):
        keys = sorted(set(t[dim] for t in results))
        rows = []
        for k in keys:
            grp = [t for t in results if t[dim] == k]
            wr, n = wr_of(grp)
            pips = sum(t["pips"] for t in grp)
            rows.append((k, n, wr, pips))
        return rows

    sess_rows = table("_session")
    band_rows = table("_band")
    dow_rows  = table("_dow")
    pair_rows = table("pair")

    at_key  = [t for t in results if t.get("at_key_level")]
    off_key = [t for t in results if not t.get("at_key_level")]
    at_wr, at_n   = wr_of(at_key)
    off_wr, off_n = wr_of(off_key)

    # ── Flag weak patterns (evidence thresholds, minimum sample sizes)
    flags = {}

    for k, n, wr, pips in sess_rows:
        if n >= 20 and wr < overall_wr - 8:
            flags[f"session_{k}_weak"] = (
                f"{k} session: {wr}% WR over {n} trades vs {overall_wr}% overall "
                f"-> candidate: session filter (skip {k})")

    for k, n, wr, pips in band_rows:
        if k == "5-6" and n >= 20 and wr < overall_wr - 8:
            flags["low_score_band_weak"] = (
                f"Score 5-6 trades: {wr}% WR over {n} trades vs {overall_wr}% overall "
                f"-> candidate: raise Scalp min_score 5 -> 6")

    for k, n, wr, pips in pair_rows:
        if n >= 15 and wr < 45:
            flags[f"pair_{k}_weak"] = (
                f"{k}: {wr}% WR over {n} trades "
                f"-> candidate: pause Scalp on {k}")

    if off_n >= 30 and at_n >= 30 and off_wr > at_wr + 8:
        flags["key_level_bonus_backwards"] = (
            f"OFF-key-level trades ({off_wr}%) beat AT-key-level ({at_wr}%) "
            f"-> candidate: remove key-level score bonus")

    if have_prices:
        classified_total = sum(v for k, v in autopsy.items() if k != "UNKNOWN")
        if classified_total >= 20:
            tight_pct = autopsy["TOO_TIGHT_STOP"] / classified_total * 100
            wrong_pct = autopsy["WRONG_DIRECTION"] / classified_total * 100
            if tight_pct >= 35:
                flags["stops_too_tight"] = (
                    f"{tight_pct:.0f}% of losses reached TP after stopping out "
                    f"-> candidate: widen ATR stop multiplier one notch")
            if wrong_pct >= 45:
                flags["signal_quality_issue"] = (
                    f"{wrong_pct:.0f}% of losses were outright wrong direction "
                    f"-> candidate: tighten entry gates (raise min_score)")

    # ── Two-week persistence: WATCH vs ACTIONABLE
    prev_flags = set()
    try:
        if os.path.exists(FLAGS_FILE):
            with open(FLAGS_FILE) as f:
                prev_flags = set(json.load(f).get("flags", []))
    except Exception:
        pass

    actionable = {k: v for k, v in flags.items() if k in prev_flags}
    watching   = {k: v for k, v in flags.items() if k not in prev_flags}
    cleared    = prev_flags - set(flags.keys())

    with open(FLAGS_FILE, "w") as f:
        json.dump({"updated": utcnow().isoformat(),
                   "flags": sorted(flags.keys())}, f, indent=2)

    # ── Report
    L = []
    L.append("=" * 58)
    L.append("  WAVEEDGE LOSS FORENSICS — SUNDAY IMPROVEMENT ENGINE")
    L.append(f"  {utcnow().strftime('%Y-%m-%d %H:%M UTC')} | source: {os.path.basename(path)}")
    L.append("=" * 58)
    L.append(f"  Trades: {total} | Overall WR: {overall_wr}% | Losses: {len(losses)}")
    L.append("")
    L.append("-" * 58)
    L.append("LOSS AUTOPSY (why did losers lose?):")
    if have_prices:
        for k in ("TOO_TIGHT_STOP", "WRONG_DIRECTION", "CHOP", "UNKNOWN"):
            L.append(f"  {k:16}: {autopsy[k]:4}")
        for strat, d in autopsy_by_strat.items():
            L.append(f"    {strat}: tight:{d['TOO_TIGHT_STOP']} wrong:{d['WRONG_DIRECTION']} "
                     f"chop:{d['CHOP']} unk:{d['UNKNOWN']}")
    else:
        L.append("  (needs backtest v4.1 entry/sl/tp data — available next run)")
    L.append("")
    L.append("-" * 58)
    L.append("WR BY SESSION (UTC):")
    for k, n, wr, pips in sess_rows:
        L.append(f"  {k:11}: {n:4} trades | {wr:5.1f}% WR | {pips:+.1f} pips")
    L.append("")
    L.append("WR BY SCORE BAND:")
    for k, n, wr, pips in band_rows:
        L.append(f"  {k:11}: {n:4} trades | {wr:5.1f}% WR | {pips:+.1f} pips")
    L.append("")
    L.append("WR BY DAY:")
    for k, n, wr, pips in dow_rows:
        L.append(f"  {k:11}: {n:4} trades | {wr:5.1f}% WR | {pips:+.1f} pips")
    L.append("")
    L.append(f"KEY LEVEL: AT {at_wr}% ({at_n}) vs OFF {off_wr}% ({off_n})")
    L.append("")
    L.append("-" * 58)
    L.append("ACTIONABLE (seen 2+ Sundays running — pick AT MOST ONE):")
    if actionable:
        for k, v in actionable.items():
            L.append(f"  >> {v}")
    else:
        L.append("  none — no pattern has persisted two weeks yet")
    L.append("")
    L.append("WATCHING (first appearance — do NOT act yet):")
    if watching:
        for k, v in watching.items():
            L.append(f"  ?  {v}")
    else:
        L.append("  none")
    if cleared:
        L.append("")
        L.append("CLEARED (flagged last week, gone this week — variance, not signal):")
        for k in sorted(cleared):
            L.append(f"  ok {k}")
    L.append("")
    L.append("-" * 58)
    L.append("RULES: max ONE change per week, from ACTIONABLE only.")
    L.append("Log every change in /root/waveedge/CHANGELOG.md with date + reason.")
    L.append("Judge each change on 2 weeks of before/after data.")
    L.append("=" * 58)

    report = "\n".join(L)
    date_str = utcnow().strftime("%Y%m%d_%H%M")
    out = f"{REPORT_DIR}/loss_forensics_{date_str}.txt"
    with open(out, "w") as f:
        f.write(report)
    log(f"Report saved: {out}")
    print(report)


if __name__ == "__main__":
    main()
