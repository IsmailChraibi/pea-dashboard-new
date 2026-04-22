"""
Storage backends for PEA transactions.

Primary: Google Sheets (via gspread + service account).
Fallback for local dev / demo: a CSV file on disk.

The app always operates on an in-memory pandas DataFrame.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

REQUIRED_COLUMNS = ["Date", "Ticker", "Quantity", "Price"]


def normalize_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce types and keep only the required columns."""
    out = pd.DataFrame(index=df.index)
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            out[col] = pd.NA
        else:
            out[col] = df[col].values

    # Types
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce").dt.date
    out["Ticker"] = out["Ticker"].astype(str).str.strip()
    out["Quantity"] = pd.to_numeric(out["Quantity"], errors="coerce")
    out["Price"] = pd.to_numeric(out["Price"], errors="coerce")

    # Drop fully empty rows or rows missing critical fields
    out = out.dropna(subset=["Date", "Ticker"], how="any")
    out = out[out["Ticker"] != ""]
    out = out[out["Ticker"].str.lower() != "nan"]
    out = out.sort_values("Date").reset_index(drop=True)
    return out


# =====================================================================
# Google Sheets
# =====================================================================

def _gspread_client():
    """Return an authenticated gspread client using Streamlit secrets."""
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def _get_worksheet():
    """Open the configured sheet and worksheet, creating the worksheet if missing."""
    import gspread

    client = _gspread_client()
    sheet_url = st.secrets["gsheets"]["sheet_url"]
    worksheet_name = st.secrets["gsheets"].get("worksheet", "transactions")

    sh = client.open_by_url(sheet_url)
    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=200, cols=10)
        ws.update(values=[REQUIRED_COLUMNS], range_name="A1")
    return ws


def sheets_available() -> bool:
    """Check whether Google Sheets backend is fully configured."""
    return "gcp_service_account" in st.secrets and "gsheets" in st.secrets


def load_from_sheets() -> pd.DataFrame:
    """Read transactions from the configured Google Sheet."""
    ws = _get_worksheet()
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)
    df = pd.DataFrame(records)
    return normalize_transactions(df)


def save_to_sheets(df: pd.DataFrame) -> None:
    """Overwrite the Google Sheet with the current transactions."""
    ws = _get_worksheet()
    df = normalize_transactions(df)

    # Prepare rows: header + data (convert dates to strings)
    header = REQUIRED_COLUMNS
    rows = []
    for _, r in df.iterrows():
        rows.append([
            r["Date"].isoformat() if pd.notna(r["Date"]) else "",
            str(r["Ticker"]),
            float(r["Quantity"]) if pd.notna(r["Quantity"]) else 0,
            float(r["Price"]) if pd.notna(r["Price"]) else 0,
        ])

    # Clear everything then write header+data in one call
    ws.clear()
    ws.update(values=[header] + rows, range_name="A1")


# =====================================================================
# CSV (fallback / import)
# =====================================================================

def load_from_csv_bytes(uploaded) -> pd.DataFrame:
    """Read an uploaded CSV or XLSX into a normalized transactions DataFrame."""
    name = uploaded.name.lower()
    if name.endswith((".xlsx", ".xls")):
        try:
            df = pd.read_excel(uploaded, header=1)
            df = df.dropna(axis=1, how="all")
            if df.shape[1] >= 3:
                df = df.iloc[:, :4] if df.shape[1] >= 4 else df.iloc[:, :3]
                cols = ["Date", "Ticker", "Quantity"]
                if df.shape[1] == 4:
                    cols.append("Price")
                df.columns = cols
        except Exception:
            uploaded.seek(0)
            df = pd.read_excel(uploaded)
    else:
        df = pd.read_csv(uploaded)
    return normalize_transactions(df)


def save_to_disk_cache(df: pd.DataFrame, path: Path) -> None:
    """Optional: persist a local CSV copy as fallback."""
    df.to_csv(path, index=False)


def load_from_disk_cache(path: Path) -> pd.DataFrame:
    """Optional: read from a local CSV if Sheets isn't configured."""
    if not path.exists():
        return pd.DataFrame(columns=REQUIRED_COLUMNS)
    df = pd.read_csv(path)
    return normalize_transactions(df)
