#!/usr/bin/env python3
"""WaveEdge Sunday command center.

This is the only weekly entry point. It runs the full-fidelity engine and then
passes that exact result file to loss forensics, preventing two backtest
implementations from drifting into two different trading brains.
"""

import json
import os
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

REPORT_DIR = os.getenv("WAVEEDGE_REPORT_DIR", "/root/waveedge/reports")
LOG_FILE = os.getenv("WAVEEDGE_BACKTEST_LOG", "/root/waveedge/logs/backtest.log")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(message):
    line = f"{utcnow().isoformat()} | WEEKLY | {message}"
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    print(line)


def atomic_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp.{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def main():
    from full_fidelity_backtest import main as run_full_fidelity
    from loss_forensics import main as run_forensics

    os.makedirs(REPORT_DIR, exist_ok=True)
    started = utcnow()
    log("Starting the single full-fidelity weekly chain")

    backtest = run_full_fidelity()
    results_path = backtest.get("results_path")
    if not results_path or not os.path.exists(results_path):
        raise RuntimeError("Full-fidelity backtest did not produce a results file")

    forensic = run_forensics(results_path=results_path)
    summary = {
        "started": started.isoformat(),
        "finished": utcnow().isoformat(),
        "status": "completed",
        "backtest": backtest,
        "forensics": forensic,
    }
    summary_path = os.path.join(
        REPORT_DIR, f"weekly_chain_{utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    )
    atomic_json(summary_path, summary)
    log(f"Weekly chain complete: {summary_path}")
    return summary


if __name__ == "__main__":
    main()
