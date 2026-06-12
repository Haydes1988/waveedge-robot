import requests
import json
from datetime import datetime

URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

def fetch_news():
    try:
        response = requests.get(URL, timeout=10)
        response.raise_for_status()
        events = response.json()
    except Exception as e:
        print(f"ERROR fetching calendar: {e}")
        return []

    today = datetime.utcnow().strftime("%Y-%m-%d")
    high_impact_today = []

    for event in events:
        event_date = event.get("date", "")[:10]
        impact = event.get("impact", "")
        if event_date == today and impact == "High":
            high_impact_today.append({
                "title": event.get("title"),
                "country": event.get("country"),
                "date": event.get("date"),
                "impact": impact
            })

    return high_impact_today

def save_news(events):
    output = {
        "checked_at": datetime.utcnow().isoformat(),
        "high_impact_today": events
    }
    with open("/root/waveedge/data/news_today.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"Saved {len(events)} high-impact event(s).")

if __name__ == "__main__":
    events = fetch_news()
    save_news(events)
    for e in events:
        print(f"{e['date']} | {e['country']} | {e['title']}")
