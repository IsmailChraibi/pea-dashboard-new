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

HTTP_TIMEOUT = 10  # seconds
HTTP_HEADERS = {
    # Real browser User-Agent + full header set to avoid bot detection
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Separate, lighter headers for JSON API calls
JSON_HEADERS = {
    "User-Agent": HTTP_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://finance.yahoo.com",
    "Referer": "https://finance.yahoo.com/",
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

def fetch_stooq(ticker: str) -> tuple[Quote | None, str | None]:
    """Fetch current price from Stooq CSV API.
    Returns (Quote, None) on success, (None, error_message) on failure."""
    sym = SOURCE_TICKERS.get(ticker, {}).get("stooq")
    if not sym:
        return None, "no Stooq symbol mapping"
    url = f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
        if r.status_code != 200:
            return None, f"Stooq HTTP {r.status_code}"
        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            return None, f"Stooq returned no data (body: {r.text[:80]!r})"
        headers = [h.strip().lower() for h in lines[0].split(",")]
        values = [v.strip() for v in lines[1].split(",")]
        row = dict(zip(headers, values))
        if row.get("close", "N/D") in ("N/D", "", "0"):
            return None, f"Stooq: ticker not found or no price ({row})"
        price = float(row["close"])
        if price <= 0:
            return None, f"Stooq: invalid price {price}"
        d_str = row.get("date", "")
        t_str = row.get("time", "00:00:00")
        try:
            ts = datetime.strptime(f"{d_str} {t_str}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts = datetime.strptime(d_str, "%Y-%m-%d") if d_str else datetime.utcnow()
        ts = ts.replace(tzinfo=timezone.utc)
        return Quote(price=price, as_of=ts, source="Stooq", ticker=ticker), None
    except requests.exceptions.Timeout:
        return None, "Stooq: request timed out"
    except requests.exceptions.ConnectionError as e:
        return None, f"Stooq: connection error ({type(e).__name__})"
    except Exception as e:
        return None, f"Stooq: {type(e).__name__}: {e}"


# =====================================================================
# Source 2 — Yahoo Finance chart JSON API
# =====================================================================

def fetch_yahoo(ticker: str) -> tuple[Quote | None, str | None]:
    """Fetch current price from Yahoo Finance chart endpoint."""
    sym = SOURCE_TICKERS.get(ticker, {}).get("yahoo")
    if not sym:
        return None, "no Yahoo symbol mapping"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    try:
        r = requests.get(
            url,
            timeout=HTTP_TIMEOUT,
            headers=JSON_HEADERS,
            params={"interval": "1d", "range": "5d"},
        )
        if r.status_code != 200:
            return None, f"Yahoo HTTP {r.status_code}"
        data = r.json()
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            err = (data.get("chart") or {}).get("error")
            return None, f"Yahoo: no result ({err})" if err else "Yahoo: empty result"
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        ts_unix = meta.get("regularMarketTime")
        if price is None:
            return None, "Yahoo: no price in response"
        if ts_unix is None:
            return None, "Yahoo: no timestamp in response"
        ts = datetime.fromtimestamp(int(ts_unix), tz=timezone.utc)
        return Quote(price=float(price), as_of=ts, source="Yahoo", ticker=ticker), None
    except requests.exceptions.Timeout:
        return None, "Yahoo: request timed out"
    except requests.exceptions.ConnectionError as e:
        return None, f"Yahoo: connection error ({type(e).__name__})"
    except ValueError as e:  # JSON decode error
        return None, f"Yahoo: invalid JSON ({e})"
    except Exception as e:
        return None, f"Yahoo: {type(e).__name__}: {e}"


def fetch_yahoo_history(ticker: str, start: date, end: date) -> pd.Series | None:
    """Historical daily closes from Yahoo."""
    sym = SOURCE_TICKERS.get(ticker, {}).get("yahoo")
    if not sym:
        return None
    p1 = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    try:
        r = requests.get(
            url, timeout=HTTP_TIMEOUT, headers=JSON_HEADERS,
            params={"period1": p1, "period2": p2, "interval": "1d"},
        )
        if r.status_code != 200:
            return None
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


def fetch_boursorama(ticker: str) -> tuple[Quote | None, str | None]:
    """Scrape current price from Boursorama. Tries multiple HTML patterns."""
    slug = SOURCE_TICKERS.get(ticker, {}).get("boursorama")
    if not slug:
        return None, "no Boursorama symbol mapping"
    url = f"https://www.boursorama.com/bourse/trackers/cours/{slug}/"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
        if r.status_code != 200:
            return None, f"Boursorama HTTP {r.status_code}"
        html = r.text

        price = None
        for pat in BOURSORAMA_PATTERNS:
            m = pat.search(html)
            if m:
                p = _parse_french_number(m.group(1))
                if p is not None and p > 0:
                    price = p
                    break
        if price is None:
            # Try an alternative URL for stocks (1rP prefix instead of 1rT)
            alt_slug = slug.replace("1rT", "1rP")
            if alt_slug != slug:
                r2 = requests.get(
                    f"https://www.boursorama.com/cours/{alt_slug}/",
                    timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS,
                )
                if r2.status_code == 200:
                    for pat in BOURSORAMA_PATTERNS:
                        m = pat.search(r2.text)
                        if m:
                            p = _parse_french_number(m.group(1))
                            if p is not None and p > 0:
                                price = p
                                html = r2.text
                                break
        if price is None:
            return None, "Boursorama: no price pattern matched in HTML"

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
        return Quote(price=price, as_of=ts, source="Boursorama", ticker=ticker), None
    except requests.exceptions.Timeout:
        return None, "Boursorama: request timed out"
    except requests.exceptions.ConnectionError as e:
        return None, f"Boursorama: connection error ({type(e).__name__})"
    except Exception as e:
        return None, f"Boursorama: {type(e).__name__}: {e}"


# =====================================================================
# Orchestration
# =====================================================================

def fetch_current_price(ticker: str) -> tuple[Quote | None, list[str]]:
    """Try each source in order, return (freshest Quote or None, list_of_errors).

    The errors list lets callers show users WHY a ticker failed.
    """
    quotes: list[Quote] = []
    errors: list[str] = []
    for fetcher in (fetch_yahoo, fetch_boursorama, fetch_stooq):
        q, err = fetcher(ticker)
        if q is not None:
            quotes.append(q)
            if q.staleness_days < 1.5:
                break
        elif err:
            errors.append(err)

    if not quotes:
        return None, errors
    return min(quotes, key=lambda x: x.staleness_days), errors


def fetch_all_current_prices(tickers: list[str]) -> dict[str, tuple[Quote | None, list[str]]]:
    """Fetch current prices for many tickers."""
    out: dict[str, tuple[Quote | None, list[str]]] = {}
    for t in tickers:
        out[t] = fetch_current_price(t)
        time.sleep(0.15)
    return out


def fetch_history(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    """Historical daily closes via Yahoo JSON."""
    series_list = []
    for t in tickers:
        s = fetch_yahoo_history(t, start, end)
        if s is not None and len(s) > 0:
            s.name = t
            series_list.append(s)

    if not series_list:
        return pd.DataFrame(index=pd.bdate_range(start=start, end=end))

    df = pd.concat(series_list, axis=1)
    idx = pd.bdate_range(start=start, end=end)
    df = df.reindex(idx).ffill()
    return df
