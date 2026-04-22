# PEA Performance Dashboard — v3

## What's new vs v2

**Accurate live prices via a multi-source waterfall.** The dashboard now pulls
current prices from three different data sources and automatically uses the
freshest one:

1. **Yahoo Finance JSON API** (primary) — `query1.finance.yahoo.com/v8/finance/chart/`.
   This is a different endpoint from the `yfinance` library that v1/v2 used — it's
   fresher (backs Yahoo Finance's own live charts) and correctly timestamped.
2. **Boursorama** (fallback) — scrapes the live page from France's biggest retail
   broker. Always fresh, always in EUR.
3. **Stooq** (tertiary) — free CSV endpoint. May be rate-limited but worth trying.

**No more silent fallback values.** v1/v2 had a `FALLBACK_CURRENT_PRICES` dict
baked into the code — if the price feed failed, the app would silently use those
stale values, leading to wildly wrong P&L. v3 removes that entirely. If all three
sources fail for a ticker, the app shows a clear red error for that instrument
and excludes it from calculations.

**Price feed status panel on the MASTER tab** — shows which source returned the
price for each ticker, when it was observed, and a freshness indicator
(🟢 fresh / 🟡 1-3 days / 🔴 >3 days stale).

**MASTER table restructured** per your request:
- New columns: **P&L (€), YTD (€), 1M (€), 1D (€), % of Portfolio**
- **TOTAL row** at the bottom
- Annualization fixed: only computed when position held ≥180 days
  (annualizing a 6-week position produces nonsense numbers)

**New annual-returns-by-instrument table** on the MASTER tab:
- Rows: instruments, Columns: years (2020, 2021, ..., 2026 YTD)
- Cells: annual P&L in EUR, cashflow-adjusted
- Includes a "Total" column and a "TOTAL" row

## Setup

Same as v2 — see the v2 README for the full Google Cloud service account
walkthrough. **No setup changes** for the price feed: it works out of the box
from Streamlit Cloud, no additional API keys needed.

## Deploy

1. Unzip, push to GitHub (public repo is fine)
2. Deploy on [share.streamlit.io](https://share.streamlit.io)
3. Paste your Google Sheets secrets into Settings → Secrets
4. Lock app visibility to just your email

## Files

```
app.py              — main Streamlit application
price_feed.py       — multi-source price fetcher (new in v3)
storage.py          — Google Sheets transaction storage
requirements.txt    — dependencies (removed yfinance, added requests)
README.md           — this file
.streamlit/
  config.toml       — theme
  secrets.toml.example — secrets template
```

## Adding new instruments

Edit `SOURCE_TICKERS` in `price_feed.py`:

```python
"EPA:NEWTICKER": {
    "yahoo":      "NEWTICKER.PA",
    "boursorama": "1rTNEWTICKER",  # from the Boursorama page URL
    "stooq":      "newticker.fr",
},
```

And add to `INSTRUMENTS` + `GEO_EXPOSURE` in `app.py` as before.

## Troubleshooting

**"Could not fetch current prices for: [TICKER]"** — all three sources failed.
Check in a browser:
- Yahoo: `https://query1.finance.yahoo.com/v8/finance/chart/TICKER.PA`
- Boursorama: `https://www.boursorama.com/bourse/trackers/cours/<slug>/`

**Prices appear stale despite the live feed** — click **🔄 Refresh all prices now**
in the sidebar to clear the 30-minute cache.

**Boursorama scrape returns a weird number** — they occasionally change HTML
structure. The fetcher tries 3 different regex patterns; if none match, it
returns None and the app falls back to Stooq. Open an issue / patch the patterns
in `price_feed.py → BOURSORAMA_PATTERNS`.

## Methodology

- **Time-weighted returns**: daily portfolio value / previous day, cashflow-
  neutralized. Standard for comparing strategy performance regardless of
  deposit timing.
- **Annual P&L per instrument**: `EndYearValue - StartYearValue - CashflowsInYear`.
  Isolates pure price performance from contributions.
- **Annualization**: only applied when the weighted-average holding period is
  ≥ 180 days, to avoid misleading figures for recently-opened positions.
- **Portfolio value on the latest day** uses the live current price from the
  waterfall, not the last historical close, so your KPIs reflect real-time
  market value.
