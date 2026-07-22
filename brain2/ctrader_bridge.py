#!/usr/bin/env python3
"""
WaveEdge cTrader Bridge — gold execution via cTrader Open API.

ARCHITECTURE NOTE: cTrader's Open API (ctrader_open_api package) is built
on Twisted — an async, persistent-connection framework, unlike GCAPI's
simple request/response calls. It can't be called directly from webhook.py
the way place_race() calls GCAPI. Instead, this runs as its OWN always-on
process (like monitor.py), holding one persistent authenticated connection,
and watches a simple job file for orders to place. webhook.py's job, once
this is proven working, is just to WRITE a job here for XAUUSD signals —
not to talk to cTrader directly itself.

HONEST STATUS: written from the real, current OpenApiPy documentation and
message patterns — but never yet connected to a real account. The app
approval + a valid access token are required before this can be tested at
all. Treat every part of the connection/auth flow as UNVERIFIED until we
watch it succeed once, live, on the demo.
"""

import json
import os
import time
from datetime import datetime, timezone
from env_loader import load_env

load_env()

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq, ProtoOANewOrderReq,
    ProtoOASymbolsListReq, ProtoOAGetAccountListByAccessTokenReq
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType, ProtoOATradeSide
)
from twisted.internet import reactor

CLIENT_ID     = os.environ.get("CTRADER_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CTRADER_CLIENT_SECRET", "")
ACCESS_TOKEN  = os.environ.get("CTRADER_ACCESS_TOKEN", "")
ACCOUNT_ID    = int(os.environ.get("CTRADER_ACCOUNT_ID", "0"))
HOST_TYPE     = os.environ.get("CTRADER_HOST_TYPE", "demo")  # "demo" or "live"

JOBS_FILE   = "/root/waveedge/data/ctrader_jobs.json"
RESULT_FILE = "/root/waveedge/data/ctrader_results.json"
LOG_FILE    = "/root/waveedge/logs/ctrader_bridge.log"

SYMBOL_IDS = {}  # populated from ProtoOASymbolsListRes once connected


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(msg):
    # Print only — this script gets launched with "nohup ... >> logfile",
    # same pattern as webhook.py, so the redirect already captures this.
    # Writing directly to the file too caused the exact double-logging bug
    # already found and fixed in breakeven_manager/position_manager/
    # outcome_tracker/brain3_intelligence tonight.
    ts = utcnow().isoformat()
    line = f"{ts} | CTRADER | {msg}"
    print(line)


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


def record_result(job_id, status, detail):
    results = load_json(RESULT_FILE, {})
    results[job_id] = {"status": status, "detail": detail,
                       "time": utcnow().isoformat()}
    save_json(RESULT_FILE, results)


# ─── CONNECTION SETUP ─────────────────────────────────────────────────────────

host = EndPoints.PROTOBUF_DEMO_HOST if HOST_TYPE == "demo" else EndPoints.PROTOBUF_LIVE_HOST
client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)


def on_error(failure):
    log(f"CONNECTION ERROR: {failure}")


def on_disconnected(client, reason):
    log(f"DISCONNECTED: {reason} — will need manual restart")


def on_message_received(client, message):
    # Catches any message not explicitly handled by a callback below,
    # useful for spotting unexpected server responses during first tests.
    pass


def on_account_auth_success(result):
    parsed = Protobuf.extract(result)
    if parsed.__class__.__name__ == "ProtoOAErrorRes":
        log(f"ACCOUNT AUTH ACTUALLY FAILED (previous version printed a false "
            f"'authenticated' message here without checking) — errorCode: "
            f"{getattr(parsed, 'errorCode', 'unknown')} | description: "
            f"{getattr(parsed, 'description', 'none given')}")
        return
    log("Account authenticated (verified, not assumed) — bridge is live and ready for jobs")
    req = ProtoOASymbolsListReq()
    req.ctidTraderAccountId = ACCOUNT_ID
    deferred = client.send(req)
    deferred.addCallback(on_symbols_received)
    deferred.addErrback(on_error)


def on_symbols_received(result):
    try:
        parsed = Protobuf.extract(result)
        if parsed.__class__.__name__ == "ProtoOAErrorRes":
            log(f"SERVER REJECTED symbol list request — errorCode: "
                f"{getattr(parsed, 'errorCode', 'unknown')} | "
                f"description: {getattr(parsed, 'description', 'none given')}")
        elif hasattr(parsed, "symbol"):
            for sym in parsed.symbol:
                SYMBOL_IDS[sym.symbolName] = sym.symbolId
            log(f"Loaded {len(SYMBOL_IDS)} symbols. XAUUSD id: {SYMBOL_IDS.get('XAUUSD', 'NOT FOUND')}")
        else:
            log(f"Unexpected response type: {parsed.__class__.__name__} — full content: {parsed}")
    except Exception as e:
        log(f"Symbol list parse error: {e}")
    start_job_loop()


def on_account_list_received(result, expected_account_id):
    """Fetches the REAL account list tied to this access token, and checks
    whether the account ID we've been given actually matches one of them.
    This is the documented, correct sequence per cTrader's own forum
    guidance: Authorize App -> Get Accounts List -> Authenticate Account.
    Skipping straight to account auth with a UI-copied number risks using
    a display login instead of the real ctidTraderAccountId."""
    try:
        parsed = Protobuf.extract(result)
        if parsed.__class__.__name__ == "ProtoOAErrorRes":
            log(f"GET ACCOUNTS LIST REJECTED — errorCode: "
                f"{getattr(parsed, 'errorCode', 'unknown')} | "
                f"description: {getattr(parsed, 'description', 'none given')}")
            return
        real_ids = []
        for acc in getattr(parsed, "ctidTraderAccount", []):
            acc_id = acc.ctidTraderAccountId
            is_live = getattr(acc, "isLive", None)
            trader_login = getattr(acc, "traderLogin", None)
            real_ids.append(acc_id)
            log(f"REAL ACCOUNT FOUND: ctidTraderAccountId={acc_id} "
                f"| isLive={is_live} | traderLogin={trader_login} "
                f"{'<-- MATCHES your .env value' if acc_id == expected_account_id else ''}")
        if expected_account_id not in real_ids:
            log(f"⚠️ MISMATCH: your CTRADER_ACCOUNT_ID ({expected_account_id}) does "
                f"NOT match any real account ID above. Update .env to one of the "
                f"real ctidTraderAccountId values shown, then restart this script.")
        else:
            log(f"Confirmed: {expected_account_id} is a real, valid account ID for this token")
    except Exception as e:
        log(f"Account list parse error: {e}")


def on_app_auth_success(result):
    parsed = Protobuf.extract(result)
    if parsed.__class__.__name__ == "ProtoOAErrorRes":
        log(f"APP AUTH ACTUALLY FAILED (previous version wouldn't have caught this) "
            f"— errorCode: {getattr(parsed, 'errorCode', 'unknown')} | "
            f"description: {getattr(parsed, 'description', 'none given')}")
        return
    log("Application authenticated — fetching real account list before authenticating account")
    req = ProtoOAGetAccountListByAccessTokenReq()
    req.accessToken = ACCESS_TOKEN
    deferred = client.send(req)
    deferred.addCallback(lambda r: on_account_list_received(r, ACCOUNT_ID))
    deferred.addErrback(on_error)

    # Proceed with account auth using the ID we have -- the account list
    # check above runs in parallel purely to confirm/flag a mismatch.
    req2 = ProtoOAAccountAuthReq()
    req2.ctidTraderAccountId = ACCOUNT_ID
    req2.accessToken = ACCESS_TOKEN
    deferred2 = client.send(req2)
    deferred2.addCallback(on_account_auth_success)
    deferred2.addErrback(on_error)


def on_connected(client):
    log(f"TCP connected to cTrader ({HOST_TYPE}) — authenticating application")
    req = ProtoOAApplicationAuthReq()
    req.clientId = CLIENT_ID
    req.clientSecret = CLIENT_SECRET
    deferred = client.send(req)
    deferred.addCallback(on_app_auth_success)
    deferred.addErrback(on_error)


# ─── JOB PROCESSING ───────────────────────────────────────────────────────────

def place_order(job):
    symbol_id = SYMBOL_IDS.get(job["symbol"])
    if symbol_id is None:
        record_result(job["job_id"], "failed", f"Unknown symbol {job['symbol']} — check spelling/availability")
        return

    req = ProtoOANewOrderReq()
    req.ctidTraderAccountId = ACCOUNT_ID
    req.symbolId = symbol_id
    req.orderType = ProtoOAOrderType.MARKET
    req.tradeSide = ProtoOATradeSide.BUY if job["action"] == "buy" else ProtoOATradeSide.SELL
    req.volume = int(job["volume"])  # cTrader volume units — confirm scale on first live test
    if job.get("sl"):
        req.stopLoss = float(job["sl"])
    if job.get("tp"):
        req.takeProfit = float(job["tp"])

    deferred = client.send(req)

    def on_order_result(result):
        try:
            parsed = Protobuf.extract(result)
            log(f"ORDER RESULT for {job['job_id']}: {parsed}")
            record_result(job["job_id"], "sent", str(parsed))
        except Exception as e:
            record_result(job["job_id"], "unknown", f"Sent but response parse failed: {e}")

    deferred.addCallback(on_order_result)
    deferred.addErrback(lambda f: record_result(job["job_id"], "failed", str(f)))


def check_jobs():
    jobs = load_json(JOBS_FILE, [])
    pending = [j for j in jobs if j.get("status") == "pending"]
    for job in pending:
        log(f"Processing job {job['job_id']}: {job['action']} {job['symbol']}")
        place_order(job)
        job["status"] = "processed"
    if pending:
        save_json(JOBS_FILE, jobs)
    reactor.callLater(5, check_jobs)  # poll every 5 seconds while connected


def start_job_loop():
    reactor.callLater(1, check_jobs)


# ─── START ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not CLIENT_ID or not CLIENT_SECRET or not ACCESS_TOKEN or not ACCOUNT_ID:
        log("MISSING CREDENTIALS — set CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET, "
            "CTRADER_ACCESS_TOKEN, CTRADER_ACCOUNT_ID in security/.env before starting")
        raise SystemExit(1)

    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(on_disconnected)
    client.setMessageReceivedCallback(on_message_received)
    client.startService()
    log(f"Starting cTrader bridge ({HOST_TYPE} environment)...")
    reactor.run()
