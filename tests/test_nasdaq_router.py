import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "brain2"))
import nasdaq_router as n

def test_aliases():
    for s in ["NAS100","US100","NDX","USTEC","NQ1!","OANDA:NAS100USD","CME_MINI:NQ1!","MNQ1!","NQZ2026","US100.cash"]:
        assert n.normalize_symbol(s) == "NAS100", s
    assert n.normalize_symbol("EURUSD") == "EURUSD"
    assert n.normalize_symbol("XAUUSD") == "XAUUSD"

def test_modes(monkeypatch):
    for k in ["WAVEEDGE_NASDAQ_FX_ENABLED","WAVEEDGE_NASDAQ_FUTURES_ENABLED","WAVEEDGE_NAS100_FX_MARKET_ID","WAVEEDGE_NASDAQ_ALLOW_BOTH"]:
        monkeypatch.delenv(k, raising=False)
    assert n.route_mode()[0] is None
    monkeypatch.setenv("WAVEEDGE_NASDAQ_FX_ENABLED","1")
    assert n.route_mode()[0] is None          # no market id -> fail closed
    monkeypatch.setenv("WAVEEDGE_NAS100_FX_MARKET_ID","123")
    assert n.route_mode()[0] == "fx"
    monkeypatch.setenv("WAVEEDGE_NASDAQ_FUTURES_ENABLED","1")
    assert n.route_mode()[0] is None          # both on refused
    monkeypatch.setenv("WAVEEDGE_NASDAQ_FX_ENABLED","0")
    assert n.route_mode()[0] == "futures"

def test_mnq_sizing(tmp_path, monkeypatch):
    assert n.mnq_contracts(200, 20000, 19950) == (2, 50)   # $200 / (50pt*$2)
    monkeypatch.setattr(n, "FUTURES_JOBS_FILE", str(tmp_path/"j.json"))
    r = n.route_futures("NAS100","buy",1.0,19950,[20050,0,0],20000,20000)
    assert r["dry_run"] and not r["executed"] and r["futures_job"]["contracts"] == 2
