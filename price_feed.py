"""
Live price feed for Euronext Paris instruments.

Waterfall strategy — tries each source in order, returns the freshest
successful result. NEVER returns a hardcoded fallback; if all sources
fail for a ticker, returns None and the caller surfaces a clear error.

Sources (in priority order):
  1. Stooq CSV API (https://stooq.com)   — no auth, simple format
  2. Yahoo Finance chart JSON API        — reliable timestamps
  3. Boursorama scrape (fallback)        — French broker, always fresh

Each source returns a (price, timestamp, source_name) tuple on success,
or None on failure. The caller picks the freshest.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import pandas as pd
import requests

HTTP_TIMEOUT = 8  # seconds
HTTP_HEADERS = {
    # Pretend to be a normal browser so we don't get 403'd on scrapes
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
}

# Map our internal EPA:XXX codes to each source's ticker format.
# If a value is None, that source is not supported for that ticker.
SOURCE_TICKERS: dict[str, dict] = {
    # ticker      stooq      yahoo        boursorama URL slug
    "EPA:C40":    {"stooq": "c40.fr",     "yahoo": "C40.PA",    "boursorama": "1rTC40"},
    "EPA:ALO":    {"stooq": "alo.fr",     "yahoo": "ALO.PA",    "boursorama": "1rPALO"},
    "EPA:OBLI":   {"stooq": "obli.fr",    "yahoo": "OBLI.PA",   "boursorama": "1rTOBLI"},
    "EPA:P500H":  {"stooq": "p500h.fr",   "yahoo": "P500H.PA",  "boursorama": "1rTP500H"},
    "EPA:PE500":  {"stooq": "pe500.fr",   "yahoo": "PE500.PA",  "boursorama": "1rTPE500"},
    "EPA:PLEM":   {"stooq": "plem.fr",    "yahoo": "PLEM.PA",   "boursorama": "1rTPLEM"},
    "EPA:CW8":    {"stooq": "cw8.fr",     "yahoo": "CW8.PA",    "boursorama": "1rTCW8"},
    "EPA:HLT":    {"stooq": "hlt.fr",     "yahoo": "HLT.PA",    "boursorama": "1rTHLT"},
    "EPA:PAEEM":  {"stooq": "paeem.fr",   "yahoo": "PAEEM.PA",  "boursorama": "1rTPAEEM"},
}


@dataclass
class Quote:
    """A single price observation."""
    price: float
    as_of: datetime
    source: str
    ticker: str

    @property
    def staleness_days(self) -> float:
        now = datetime.now(timezone.utc)
        aware = self.as_of if self.as_of.tzinfo else self.as_of.replace(tzinfo=timezone.utc)
        return (now - aware).total_seconds() / 86400.0


# =====================================================================
# Source 1 — Stooq
# =====================================================================

def fetch_stooq(ticker: str) -> Quote | None:
    """Fetch current price from Stooq CSV API.
    Endpoint: https://stooq.com/q/l/?s=<symbol>&f=sd2t2ohlcv&h&e=csv
    Returns columns: Symbol,Date,Time,Open,High,Low,Close,Volume.
    """
    sym = SOURCE_TICKERS.get(ticker, {}).get("stooq")
    if not sym:
        return None
    url = f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
        r.raise_for_status()
        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            return None
        headers = [h.strip().lower() for h in lines[0].split(",")]
        values = [v.strip() for v in lines[1].split(",")]
        row = dict(zip(headers, values))
        # Stooq returns "N/D" in all fields if the ticker is unknown
        if row.get("close", "N/D") in ("N/D", "", "0"):
            return None
        price = float(row["close"])
        if price <= 0:
            return None
        # Parse date + time (UTC assumption — stooq is actually US/ET for close times,
        # but for a daily close this is fine within a day's precision)
        d_str = row.get("date", "")
        t_str = row.get("time", "00:00:00")
        try:
            ts = datetime.strptime(f"{d_str} {t_str}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts = datetime.strptime(d_str, "%Y-%m-%d") if d_str else datetime.utcnow()
        ts = ts.replace(tzinfo=timezone.utc)
        return Quote(price=price, as_of=ts, source="Stooq", ticker=ticker)
    except Exception:
        return None


# =====================================================================
# Source 2 — Yahoo Finance chart JSON API
# =====================================================================

def fetch_yahoo(ticker: str) -> Quote | None:
    """Fetch current price from Yahoo Finance chart endpoint.
    Endpoint: https://query1.finance.yahoo.com/v8/finance/chart/<ticker>
    """
    sym = SOURCE_TICKERS.get(ticker, {}).get("yahoo")
    if not sym:
        return None
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    try:
        r = requests.get(
            url,
            timeout=HTTP_TIMEOUT,
            headers=HTTP_HEADERS,
            params={"interval": "1d", "range": "5d"},
        )
        r.raise_for_status()
        data = r.json()
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        ts_unix = meta.get("regularMarketTime")
        if price is None or not price or ts_unix is None:
            return None
        ts = datetime.fromtimestamp(int(ts_unix), tz=timezone.utc)
        return Quote(price=float(price), as_of=ts, source="Yahoo", ticker=ticker)
    except Exception:
        return None


def fetch_yahoo_history(ticker: str, start: date, end: date) -> pd.Series | None:
    """Fetch historical daily closes from Yahoo's chart endpoint.
    Returns a pd.Series indexed by date, or None on failure."""
    sym = SOURCE_TICKERS.get(ticker, {}).get("yahoo")
    if not sym:
        return None
    # Use period1/period2 unix timestamps
    p1 = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    try:
        r = requests.get(
            url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS,
            params={"period1": p1, "period2": p2, "interval": "1d"},
        )
        r.raise_for_status()
        data = r.json()
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None
        res = result[0]
        timestamps = res.get("timestamp") or []
        closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        if not timestamps or not closes:
            return None
        idx = pd.to_datetime([datetime.fromtimestamp(t, tz=timezone.utc).date() for t in timestamps])
        s = pd.Series(closes, index=idx, name=ticker).dropna()
        return s
    except Exception:
        return None


# =====================================================================
# Source 3 — Boursorama scrape
# =====================================================================

# Boursorama HTML patterns (try multiple — their structure varies by page version).
# 1. Attribute form: data-ist-last="633.06"
# 2. French display form:  633,06 EUR
# 3. Near-"Valeur liquidative" form:  633,0600\n(Valeur liquidative au ...)
BOURSORAMA_PATTERNS = [
    re.compile(r'data-ist-last=["\']([0-9]+(?:[.,][0-9]+)?)["\']', re.IGNORECASE),
    re.compile(r'([0-9]{1,5}[.,][0-9]{2,6})\s*EUR', re.IGNORECASE),
    re.compile(r'([0-9]{1,5}[.,][0-9]{2,6})\s*\n?\s*\(?\s*Valeur liquidative', re.IGNORECASE),
]
BOURSORAMA_ASOF_RE = re.compile(
    r"(\d{2}\.\d{2}\.\d{2,4})\s*/\s*(\d{1,2}:\d{2}(?::\d{2})?)",
)


def _parse_french_number(s: str) -> float | None:
    """Parse '633,06' or '633.06' → 633.06. Returns None if invalid."""
    try:
        return float(s.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def fetch_boursorama(ticker: str) -> Quote | None:
    """Scrape current price from Boursorama. Tries multiple HTML patterns."""
    slug = SOURCE_TICKERS.get(ticker, {}).get("boursorama")
    if not slug:
        return None
    url = f"https://www.boursorama.com/bourse/trackers/cours/{slug}/"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
        r.raise_for_status()
        html = r.text

        # Try each pattern in order; first match wins
        price = None
        for pat in BOURSORAMA_PATTERNS:
            m = pat.search(html)
            if m:
                p = _parse_french_number(m.group(1))
                if p is not None and p > 0:
                    price = p
                    break
        if price is None:
            return None

        # Try to extract the "as of" timestamp
        ts = datetime.now(timezone.utc)
        m2 = BOURSORAMA_ASOF_RE.search(html)
        if m2:
            d_str, t_str = m2.group(1), m2.group(2)
            parts = d_str.split(".")
            if len(parts) == 3:
                dd, mm, yy = parts
                year = int(yy)
                if year < 100:
                    year += 2000
                t_parts = t_str.split(":")
                try:
                    hour = int(t_parts[0])
                    minute = int(t_parts[1]) if len(t_parts) > 1 else 0
                    second = int(t_parts[2]) if len(t_parts) > 2 else 0
                    ts = datetime(year, int(mm), int(dd),
                                  hour, minute, second, tzinfo=timezone.utc)
                except (ValueError, IndexError):
                    pass
        return Quote(price=price, as_of=ts, source="Boursorama", ticker=ticker)
    except Exception:
        return None


# =====================================================================
# Orchestration
# =====================================================================

def fetch_current_price(ticker: str) -> Quote | None:
    """Try each source in order, return the freshest result found.
    If all sources fail, returns None.

    Order: Yahoo JSON (most reliable for EUR ETFs), then Boursorama
    (French broker, always fresh), then Stooq (tertiary, may be rate-limited).
    """
    quotes: list[Quote] = []
    for fetcher in (fetch_yahoo, fetch_boursorama, fetch_stooq):
        q = fetcher(ticker)
        if q is not None:
            quotes.append(q)
            # If this source is fresh (within ~1 trading day), we can stop
            if q.staleness_days < 1.5:
                break

    if not quotes:
        return None
    return min(quotes, key=lambda x: x.staleness_days)


def fetch_all_current_prices(tickers: list[str]) -> dict[str, Quote | None]:
    """Fetch current prices for many tickers. Returns {ticker: Quote or None}."""
    out: dict[str, Quote | None] = {}
    for t in tickers:
        out[t] = fetch_current_price(t)
        # Be a good web citizen
        time.sleep(0.1)
    return out


def fetch_history(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    """Fetch historical daily closes. Currently uses Yahoo only (the only source
    with a clean daily-history API). Returns a DataFrame indexed by date with
    one column per internal ticker. Missing data is NaN, forward-filled at the
    end so all business days are present."""
    series_list = []
    for t in tickers:
        s = fetch_yahoo_history(t, start, end)
        if s is not None and len(s) > 0:
            s.name = t
            series_list.append(s)

    if not series_list:
        # Return empty DF with a reasonable business-day index
        return pd.DataFrame(index=pd.bdate_range(start=start, end=end))

    df = pd.concat(series_list, axis=1)
    idx = pd.bdate_range(start=start, end=end)
    df = df.reindex(idx).ffill()
    return df
