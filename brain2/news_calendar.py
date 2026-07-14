#!/usr/bin/env python3
"""Refresh Brain 3's local economic-calendar fallback without erasing good data."""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import requests

URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
HOME = os.getenv("WAVEEDGE_HOME", "/root/waveedge")
NEWS_FILE = os.getenv("WAVEEDGE_NEWS_FILE", f"{HOME}/data/news_calendar.json")


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_news():
    try:
        response = requests.get(URL, timeout=15, headers={"User-Agent": "WaveEdge/1.0"})
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, list):
            raise ValueError("calendar response is not a list")
    except Exception as exc:
        print(f"ERROR fetching calendar: {exc}")
        return None

    now = utcnow()
    horizon = now + timedelta(hours=48)
    events = []
    for event in raw:
        impact = str(event.get("impact", "")).strip().lower()
        when = parse_time(event.get("date", event.get("time")))
        if impact not in ("high", "medium") or when is None or not (now <= when <= horizon):
            continue
        events.append({
            "title": event.get("title", event.get("name", "Unknown")),
            "country": event.get("country", event.get("currency", "")),
            "date": when.isoformat(),
            "impact": impact.capitalize(),
            "source": "ForexFactory-cache",
        })
    return events


def save_news(events):
    os.makedirs(os.path.dirname(NEWS_FILE), exist_ok=True)
    payload = {"checked_at": utcnow().isoformat(), "events": events}
    fd, temp = tempfile.mkstemp(prefix=".news-", dir=os.path.dirname(NEWS_FILE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, NEWS_FILE)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    print(f"Saved {len(events)} medium/high-impact event(s) to {NEWS_FILE}.")


def main():
    events = fetch_news()
    if events is None:
        print("Existing news cache preserved because the refresh failed.")
        return {"status": "fetch_failed", "saved": False}
    save_news(events)
    return {"status": "ok", "saved": True, "events": len(events)}


if __name__ == "__main__":
    main()
