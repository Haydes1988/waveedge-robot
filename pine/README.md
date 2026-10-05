# WaveEdge Live Alert Scripts (v5)

TradingView **indicator** scripts that send webhook JSON to the live robot with
confidence-tiered leg counts.

| Script | Chart TF | Min / Med / High score | Legs | Full risk % | TP R |
|--------|----------|------------------------|------|-------------|------|
| Day    | 15m      | 7 / 9 / 12             | 1/2/3 | 0.75 | 1.5 / 3 / 5 |
| Scalp  | 5m       | 7 / 9 / 11             | 1/2/3 | 0.60 | 1 / 2 / 3 |
| Swing  | 60m      | 8 / 10 / 13            | 1/2/3 | 1.00 | 2 / 4 / 6 |

Score thresholds are **inputs** (tune on the chart). They are score tiers, not
measured win-rate percentages.

Risk sent = `fullRiskPct * legs / 3` (low confidence → less dollars at risk).

Webhook URL: `https://waveedge.pulseandprosper.com/webhook`
