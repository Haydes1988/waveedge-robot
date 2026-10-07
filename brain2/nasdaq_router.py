"""NASDAQ 100 routing for WaveEdge: two independently switchable paths.

A) FX-broker path  (WAVEEDGE_NASDAQ_FX_ENABLED, default off)
   Uses the existing GAIN/FOREX.com CIAPI broker IF it offers a NASDAQ 100
   market. Set WAVEEDGE_NAS100_FX_MARKET_ID to the broker market id. Audit
   2026-10-07: the live account's market search returns FX pairs only (no
   index CFDs), so this path stays fail-closed until a market id exists.
B) Futures path    (WAVEEDGE_NASDAQ_FUTURES_ENABLED, default off)
   CME Micro E-mini Nasdaq-100 (MNQ). Scaffold only: validates, sizes in
   contracts and writes a dry-run job to data/futures_jobs.json. No broker
   connection is wired yet (IBKR API is the intended adapter).
Both on at once is refused unless WAVEEDGE_NASDAQ_ALLOW_BOTH=1.
"""
import json, math, os, re, uuid
from datetime import datetime, timezone

NAS100_ALIASES = {"NAS100", "US100", "NDX", "USTEC", "NQ1!", "NQ", "MNQ1!", "MNQ",
                  "NAS100USD", "US100USD", "USTECH", "USTECH100", "NASDAQ100", "NDX100", "US100CASH"}
POINT = 1.0                 # index point
MNQ_USD_PER_POINT = 2.0     # CME MNQ multiplier
MNQ_TICK = 0.25
FUTURES_JOBS_FILE = os.getenv("WAVEEDGE_FUTURES_JOBS_FILE", "/root/waveedge/data/futures_jobs.json")


def _flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def normalize_symbol(raw):
    s = str(raw or "").upper().replace("/", "").replace(" ", "")
    if ":" in s:                       # e.g. OANDA:NAS100USD, CME_MINI:NQ1!
        s = s.split(":", 1)[1]
    s = s.replace(".CASH", "CASH").replace(".", "")
    if s in NAS100_ALIASES or re.fullmatch(r"M?NQ([FGHJKMNQUVXZ]\d{2,4}|\d!)?", s):
        return "NAS100"
    return s


def fx_market():
    mid = os.getenv("WAVEEDGE_NAS100_FX_MARKET_ID", "").strip()
    if not mid.isdigit():
        return None
    return {"id": int(mid), "name": "NASDAQ 100", "pip": POINT, "usd_quote": True,
            "asset": "index"}


def route_mode():
    """Return ('fx'|'futures'|None, reason)."""
    fx, fut = _flag("WAVEEDGE_NASDAQ_FX_ENABLED"), _flag("WAVEEDGE_NASDAQ_FUTURES_ENABLED")
    if fx and fut and not _flag("WAVEEDGE_NASDAQ_ALLOW_BOTH"):
        return None, "both NASDAQ paths enabled; set only one (or WAVEEDGE_NASDAQ_ALLOW_BOTH=1)"
    if fut:
        return "futures", "futures path enabled"
    if fx:
        if fx_market() is None:
            return None, "FX path enabled but broker has no NASDAQ market (WAVEEDGE_NAS100_FX_MARKET_ID unset)"
        return "fx", "fx path enabled"
    return None, "NASDAQ disabled (both WAVEEDGE_NASDAQ_FX_ENABLED and WAVEEDGE_NASDAQ_FUTURES_ENABLED off)"


def mnq_contracts(risk_dollars, entry, sl):
    pts = abs(entry - sl)
    if pts <= 0:
        raise ValueError("stop distance must be > 0 points")
    return int(math.floor(risk_dollars / (pts * MNQ_USD_PER_POINT))), pts


def route_futures(symbol, action, risk_pct, sl, targets, entry_hint=None, balance=None, dry_run=True):
    """Scaffold: record a dry-run MNQ job. Never sends orders."""
    job = {"job_id": uuid.uuid4().hex[:12], "created": datetime.now(timezone.utc).isoformat(),
           "instrument": "MNQ", "exchange": "CME", "symbol": symbol, "action": action,
           "risk_pct": risk_pct, "sl": sl, "targets": targets, "status": "dry_run",
           "note": "no futures broker connected; scaffold only"}
    if entry_hint and sl and balance:
        n, pts = mnq_contracts(balance * risk_pct / 100.0, float(entry_hint), float(sl))
        job.update({"contracts": n, "stop_points": pts})
    try:
        os.makedirs(os.path.dirname(FUTURES_JOBS_FILE), exist_ok=True)
        jobs = json.load(open(FUTURES_JOBS_FILE)) if os.path.exists(FUTURES_JOBS_FILE) else []
        jobs.append(job)
        json.dump(jobs[-200:], open(FUTURES_JOBS_FILE, "w"), indent=1)
    except (OSError, ValueError):
        pass
    return {"executed": False, "dry_run": True, "futures_job": job, "legs": [],
            "block_stage": "futures_dry_run", "block_reason": "futures scaffold (no broker)"}
