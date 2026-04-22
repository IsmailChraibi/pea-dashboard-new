"""
PEA Performance Dashboard — v2
==============================
In-app transaction editor with Google Sheets persistence.

Tabs:
  1. MASTER       — KPIs, auto market update, per-instrument performance
  2. Performance  — portfolio value trajectory, TWR vs benchmark, annual P&L
  3. Analysis     — Sharpe, volatility, max drawdown, geographic exposure
  4. Transactions — add/edit/delete trades (the primary data entry)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

import storage

# =====================================================================
# Config
# =====================================================================

st.set_page_config(
    page_title="PEA Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

INSTRUMENTS: dict[str, dict] = {
    "EPA:C40":   {"yf": "C40.PA",   "name": "Amundi CAC 40 ESG UCITS ETF",            "asset": "Equity ETF", "geo": "France"},
    "EPA:ALO":   {"yf": "ALO.PA",   "name": "Alstom SA",                               "asset": "Equity",     "geo": "France"},
    "EPA:OBLI":  {"yf": "OBLI.PA",  "name": "Amundi Euro Government Bond ETF",         "asset": "Bond ETF",   "geo": "Eurozone"},
    "EPA:P500H": {"yf": "P500H.PA", "name": "Amundi PEA S&P 500 ESG UCITS ETF Hedged", "asset": "Equity ETF", "geo": "United States"},
    "EPA:PE500": {"yf": "PE500.PA", "name": "Amundi PEA S&P 500 ESG UCITS ETF",        "asset": "Equity ETF", "geo": "United States"},
    "EPA:PLEM":  {"yf": "PLEM.PA",  "name": "Amundi PEA MSCI EM EMEA ESG Leaders",     "asset": "Equity ETF", "geo": "Emerging EMEA"},
    "EPA:CW8":   {"yf": "CW8.PA",   "name": "Amundi MSCI World Swap UCITS ETF",        "asset": "Equity ETF", "geo": "Developed Markets"},
    "EPA:HLT":   {"yf": "HLT.PA",   "name": "Amundi STOXX Europe 600 Healthcare ETF",  "asset": "Equity ETF", "geo": "Europe"},
    "EPA:PAEEM": {"yf": "PAEEM.PA", "name": "Amundi PEA MSCI EM ESG ETF",              "asset": "Equity ETF", "geo": "Emerging Markets"},
}

GEO_EXPOSURE: dict[str, dict[str, float]] = {
    "EPA:C40":   {"France": 1.00},
    "EPA:ALO":   {"France": 1.00},
    "EPA:OBLI":  {"Eurozone": 1.00},
    "EPA:P500H": {"United States": 1.00},
    "EPA:PE500": {"United States": 1.00},
    "EPA:PLEM":  {"Emerging Europe": 0.45, "South Africa": 0.25, "Middle East": 0.30},
    "EPA:CW8":   {"United States": 0.72, "Japan": 0.06, "United Kingdom": 0.04,
                  "France": 0.03, "Canada": 0.03, "Germany": 0.025,
                  "Switzerland": 0.025, "Other Developed": 0.065},
    "EPA:HLT":   {"United Kingdom": 0.30, "Switzerland": 0.25, "Denmark": 0.20,
                  "France": 0.10, "Germany": 0.08, "Other Europe": 0.07},
    "EPA:PAEEM": {"China": 0.30, "India": 0.19, "Taiwan": 0.18, "South Korea": 0.13,
                  "Brazil": 0.06, "Saudi Arabia": 0.04, "Other EM": 0.10},
}

# Current-price fallbacks only (not used for cost basis).
FALLBACK_CURRENT_PRICES: dict[str, float] = {
    "EPA:C40": 140.0, "EPA:ALO": 17.2, "EPA:OBLI": 84.0,
    "EPA:P500H": 44.7, "EPA:PE500": 50.0, "EPA:PLEM": 20.5,
    "EPA:CW8": 595.0, "EPA:HLT": 165.0, "EPA:PAEEM": 31.5,
}

TRADING_DAYS = 252
LOCAL_CACHE = Path(__file__).parent / ".local_transactions.csv"

# =====================================================================
# Session-state init
# =====================================================================

if "transactions" not in st.session_state:
    st.session_state.transactions = pd.DataFrame(columns=storage.REQUIRED_COLUMNS)
if "loaded_once" not in st.session_state:
    st.session_state.loaded_once = False
if "last_save_ok" not in st.session_state:
    st.session_state.last_save_ok = None

# =====================================================================
# Price fetching
# =====================================================================

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_prices(tickers: tuple[str, ...], start: date, end: date) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame()
    yf_tickers = [INSTRUMENTS[t]["yf"] for t in tickers if t in INSTRUMENTS]
    if not yf_tickers:
        return pd.DataFrame()

    try:
        raw = yf.download(yf_tickers, start=start, end=end + timedelta(days=1),
                          auto_adjust=True, progress=False, group_by="column")
    except Exception as e:
        st.warning(f"Yahoo Finance call failed: {e}")
        return pd.DataFrame()

    if raw.empty:
        return pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" in raw.columns.get_level_values(0):
            prices = raw["Close"]
        else:
            prices = raw.xs("Close", axis=1, level=0, drop_level=True)
    else:
        prices = raw[["Close"]] if "Close" in raw.columns else raw
        if isinstance(prices, pd.DataFrame) and prices.shape[1] == 1:
            prices.columns = [yf_tickers[0]]

    inv_map = {v["yf"]: k for k, v in INSTRUMENTS.items()}
    prices = prices.rename(columns=inv_map)
    prices = prices[[t for t in tickers if t in prices.columns]]
    idx = pd.bdate_range(start=start, end=end)
    return prices.reindex(idx).ffill()


def apply_fallbacks(prices: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    idx = prices.index if not prices.empty else pd.bdate_range(
        start=date.today() - timedelta(days=365 * 6), end=date.today())
    out = prices.copy() if not prices.empty else pd.DataFrame(index=idx)
    for t in tickers:
        if t not in out.columns or out[t].isna().all():
            out[t] = FALLBACK_CURRENT_PRICES.get(t, np.nan)
    return out.ffill().bfill()

# =====================================================================
# Portfolio computations (prices come from USER, not Yahoo, for cost basis)
# =====================================================================

def build_positions(transactions: pd.DataFrame, price_index: pd.DatetimeIndex) -> pd.DataFrame:
    tickers = sorted(transactions["Ticker"].unique())
    tx = transactions.copy()
    tx["Date"] = pd.to_datetime(tx["Date"])
    daily = tx.pivot_table(index="Date", columns="Ticker", values="Quantity",
                           aggfunc="sum", fill_value=0)
    daily = daily.reindex(price_index, fill_value=0)
    for t in tickers:
        if t not in daily.columns:
            daily[t] = 0
    return daily[tickers].cumsum()


def compute_portfolio_series(
    transactions: pd.DataFrame,
    positions: pd.DataFrame,
    prices: pd.DataFrame,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Portfolio value uses Yahoo Close for mark-to-market, but cashflows use the
    user-entered transaction prices (Price column)."""
    common = [t for t in positions.columns if t in prices.columns]
    pos = positions[common]
    px = prices[common]
    total_value = (pos * px).sum(axis=1)

    # Cashflow on each transaction day = qty * user-entered Price
    tx = transactions.copy()
    tx["Date"] = pd.to_datetime(tx["Date"])
    tx["Cashflow"] = tx["Quantity"] * tx["Price"]
    daily_cf = (tx.groupby("Date")["Cashflow"].sum()
                  .reindex(total_value.index, fill_value=0.0))
    net_invested = daily_cf.cumsum()

    prev_v = total_value.shift(1)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = (total_value - daily_cf) / prev_v - 1
    r = r.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return total_value, net_invested, daily_cf, r


def period_return(daily_ret: pd.Series, start: pd.Timestamp | None) -> float:
    s = daily_ret if start is None else daily_ret.loc[daily_ret.index > start]
    if s.empty:
        return 0.0
    return float((1 + s).prod() - 1)


def annualize(total_return: float, days: float) -> float:
    if days <= 0 or total_return <= -1:
        return 0.0
    years = max(days / 365.25, 1 / 365.25)
    return (1 + total_return) ** (1 / years) - 1


def max_drawdown(r: pd.Series) -> float:
    if r.empty:
        return 0.0
    cum = (1 + r).cumprod()
    return float(((cum - cum.cummax()) / cum.cummax()).min())


# =====================================================================
# Formatting
# =====================================================================

def fmt_eur(x, d=0):
    if pd.isna(x):
        return "—"
    sign = "-" if x < 0 else ""
    return f"{sign}€{abs(x):,.{d}f}"


def fmt_pct(x, d=2):
    if pd.isna(x):
        return "—"
    return f"{x*100:+.{d}f}%"

# =====================================================================
# Data loading (load from Sheets once per session, then work in-memory)
# =====================================================================

def load_initial_data():
    """Load transactions from Sheets (or local cache) at session start."""
    if st.session_state.loaded_once:
        return

    if storage.sheets_available():
        try:
            df = storage.load_from_sheets()
            st.session_state.transactions = df
            st.session_state.loaded_once = True
            st.session_state.storage_backend = "sheets"
            return
        except Exception as e:
            st.sidebar.error(f"Could not read Google Sheet: {e}")
            st.session_state.storage_backend = "local"
    else:
        st.session_state.storage_backend = "local"

    # Local fallback
    df = storage.load_from_disk_cache(LOCAL_CACHE)
    st.session_state.transactions = df
    st.session_state.loaded_once = True


def persist(df: pd.DataFrame) -> tuple[bool, str]:
    """Save the current transactions to whichever backend is configured."""
    df = storage.normalize_transactions(df)
    backend = st.session_state.get("storage_backend", "local")
    try:
        if backend == "sheets":
            storage.save_to_sheets(df)
            return True, "Saved to Google Sheet."
        else:
            storage.save_to_disk_cache(df, LOCAL_CACHE)
            return True, "Saved locally (no Google Sheet configured)."
    except Exception as e:
        return False, f"Save failed: {e}"


load_initial_data()

# =====================================================================
# Sidebar
# =====================================================================

with st.sidebar:
    st.title("⚙️ Settings")

    backend = st.session_state.get("storage_backend", "local")
    if backend == "sheets":
        st.success("📗 Connected to Google Sheet")
    else:
        st.warning("💾 Using local storage (not persistent on Streamlit Cloud)")

    st.caption(f"Transactions loaded: **{len(st.session_state.transactions)}**")

    rf = st.number_input("Risk-free rate (€STR proxy)", value=0.022,
                         min_value=0.0, max_value=0.10, step=0.001, format="%.3f")

    st.markdown("---")
    if st.button("🔄 Refresh prices (bypass cache)"):
        fetch_prices.clear()
        st.rerun()

    if st.button("🔁 Reload from storage"):
        st.session_state.loaded_once = False
        load_initial_data()
        st.rerun()

    with st.expander("ℹ️ About"):
        st.markdown(
            "- Transactions persisted to Google Sheets (via service account)\n"
            "- Current prices from Yahoo Finance, cached 1h\n"
            "- Returns are **time-weighted** (cashflow-neutral)\n"
            "- Cost basis uses **your entered Price** — no auto-fill"
        )

# =====================================================================
# Header
# =====================================================================

st.title("📊 PEA Performance Dashboard")
today = date.today()
st.caption(
    f"As of {today.strftime('%A, %B %d, %Y')}  •  "
    f"{len(st.session_state.transactions)} transactions  •  "
    "prices refresh hourly"
)

# Empty state
if st.session_state.transactions.empty:
    st.info(
        "👋 **No transactions yet.** Head to the **Transactions** tab to add "
        "your trades, or upload a CSV/XLSX to import them in bulk."
    )

# =====================================================================
# Compute everything (if we have data)
# =====================================================================

transactions = st.session_state.transactions.copy()

if not transactions.empty:
    tickers_used = sorted(transactions["Ticker"].unique().tolist())
    unknown = [t for t in tickers_used if t not in INSTRUMENTS]
    if unknown:
        st.warning(
            f"⚠️ Unknown tickers (will be ignored in analytics): {unknown}. "
            "Add them to `INSTRUMENTS` in `app.py` to include them."
        )
        tickers_used = [t for t in tickers_used if t in INSTRUMENTS]
        transactions = transactions[transactions["Ticker"].isin(tickers_used)].reset_index(drop=True)

    start_date = transactions["Date"].min() - timedelta(days=10)

    with st.spinner("Fetching prices…"):
        prices_raw = fetch_prices(tuple(tickers_used), start_date, today)
    prices = apply_fallbacks(prices_raw, tickers_used)

    positions = build_positions(transactions, prices.index)
    total_value, net_invested, daily_cf, daily_ret = compute_portfolio_series(
        transactions, positions, prices
    )

# =====================================================================
# Tabs
# =====================================================================

tab_master, tab_perf, tab_analysis, tab_tx = st.tabs(
    ["🏠 MASTER", "📈 Performance", "🔍 Analysis", "✏️ Transactions"]
)

# -----------------------------------------------------------------
# MASTER
# -----------------------------------------------------------------
with tab_master:
    if transactions.empty:
        st.write("Add transactions first.")
    else:
        today_ts = total_value.index[-1]
        cur_value = float(total_value.iloc[-1])
        cur_invested = float(net_invested.iloc[-1])
        pnl = cur_value - cur_invested

        one_month_ago = today_ts - pd.Timedelta(days=30)
        ytd_start = pd.Timestamp(year=today_ts.year, month=1, day=1) - pd.Timedelta(days=1)

        si_ret = period_return(daily_ret, None)
        ytd_ret = period_return(daily_ret, ytd_start)
        mtd_ret = period_return(daily_ret, one_month_ago)
        last_day_ret = float(daily_ret.iloc[-1]) if len(daily_ret) else 0.0

        days_held = (total_value.index[-1] - total_value.index[0]).days
        ann_ret_si = annualize(si_ret, days_held)
        ann_vol = float(daily_ret.std() * np.sqrt(TRADING_DAYS))

        c1, c2, c3 = st.columns(3)
        c1.metric("Current Value", fmt_eur(cur_value), fmt_eur(pnl))
        c2.metric("Net Invested", fmt_eur(cur_invested))
        c3.metric("Total P&L", fmt_eur(pnl), fmt_pct(si_ret),
                  delta_color="normal" if pnl >= 0 else "inverse")

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Since Inception", fmt_pct(si_ret), f"ann. {fmt_pct(ann_ret_si)}")
        c2.metric("YTD", fmt_pct(ytd_ret))
        c3.metric("Last 30d", fmt_pct(mtd_ret))
        c4.metric("Last Day", fmt_pct(last_day_ret, 3))
        c5.metric("Ann. Vol.", fmt_pct(ann_vol))

        st.markdown("---")
        st.subheader("📰 Market Update")

        per_inst = {}
        for t in tickers_used:
            qty = float(positions[t].iloc[-1]) if t in positions.columns else 0
            if qty == 0 or t not in prices.columns:
                continue
            p_now = float(prices[t].iloc[-1])
            try:
                p_ytd = float(prices[t].loc[prices[t].index >= ytd_start].iloc[0])
                ytd_i = (p_now - p_ytd) / p_ytd if p_ytd else 0
            except (IndexError, KeyError):
                ytd_i = 0
            per_inst[t] = {"qty": qty, "ytd": ytd_i, "value": qty * p_now,
                           "name": INSTRUMENTS[t]["name"]}

        if per_inst:
            best = max(per_inst.items(), key=lambda kv: kv[1]["ytd"])
            worst = min(per_inst.items(), key=lambda kv: kv[1]["ytd"])
            largest = max(per_inst.items(), key=lambda kv: kv[1]["value"])
            bt, bv = best; wt, wv = worst; lt, lv = largest
            st.info(
                f"**Portfolio YTD:** {fmt_pct(ytd_ret)} ({fmt_eur(pnl)} total P&L).  \n"
                f"**Best YTD:** {bt} — {bv['name']}: {fmt_pct(bv['ytd'])}.  \n"
                f"**Worst YTD:** {wt} — {wv['name']}: {fmt_pct(wv['ytd'])}.  \n"
                f"**Largest position:** {lt} at {fmt_eur(lv['value'])} "
                f"({lv['value']/cur_value*100:.1f}% of portfolio).  \n"
                f"**Last day:** {fmt_pct(last_day_ret, 3)}.  \n"
                f"**Annualized since inception:** {fmt_pct(ann_ret_si)} "
                f"over {days_held/365.25:.1f} years."
            )

        st.markdown("---")
        st.subheader("📋 Per-Instrument Performance")

        rows = []
        for t in tickers_used:
            qty = float(positions[t].iloc[-1]) if t in positions.columns else 0.0
            cur_px = float(prices[t].iloc[-1]) if t in prices.columns else np.nan

            # Weighted avg cost uses USER-ENTERED prices
            buys = transactions[(transactions["Ticker"] == t) & (transactions["Quantity"] > 0)]
            if buys["Quantity"].sum() > 0:
                wavg_cost = (buys["Quantity"] * buys["Price"]).sum() / buys["Quantity"].sum()
            else:
                wavg_cost = np.nan

            if qty == 0:
                sells = transactions[(transactions["Ticker"] == t) & (transactions["Quantity"] < 0)]
                total_sold = -sells["Quantity"].sum()
                if total_sold > 0 and wavg_cost:
                    wavg_sell = (-sells["Quantity"] * sells["Price"]).sum() / total_sold
                    ret_si = (wavg_sell - wavg_cost) / wavg_cost
                else:
                    ret_si = 0
                mv = cost_basis = pnl_t = 0
            else:
                ret_si = (cur_px - wavg_cost) / wavg_cost if wavg_cost and wavg_cost > 0 else 0
                mv = qty * cur_px
                cost_basis = qty * wavg_cost
                pnl_t = mv - cost_basis

            if buys["Quantity"].sum() > 0:
                buy_dates = pd.to_datetime(buys["Date"])
                wavg_date = (pd.to_numeric(buy_dates) * buys["Quantity"]).sum() / buys["Quantity"].sum()
                days_open = (today_ts - pd.to_datetime(wavg_date)).days
            else:
                days_open = 1
            ann_ret = annualize(ret_si, days_open)

            rows.append({
                "Ticker": t,
                "Instrument": INSTRUMENTS[t]["name"],
                "Qty": qty,
                "Avg Cost": wavg_cost,
                "Cur Price": cur_px,
                "Market Value": mv,
                "Cost Basis": cost_basis,
                "P&L": pnl_t,
                "Return (SI)": ret_si * 100,
                "Annualized": ann_ret * 100,
            })

        df_inst = pd.DataFrame(rows)
        st.dataframe(
            df_inst, hide_index=True, use_container_width=True,
            column_config={
                "Qty": st.column_config.NumberColumn(format="%.0f"),
                "Avg Cost": st.column_config.NumberColumn(format="€%.2f"),
                "Cur Price": st.column_config.NumberColumn(format="€%.2f"),
                "Market Value": st.column_config.NumberColumn(format="€%.0f"),
                "Cost Basis": st.column_config.NumberColumn(format="€%.0f"),
                "P&L": st.column_config.NumberColumn(format="€%.0f"),
                "Return (SI)": st.column_config.NumberColumn(format="%.2f%%"),
                "Annualized": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )

# -----------------------------------------------------------------
# Performance
# -----------------------------------------------------------------
with tab_perf:
    if transactions.empty:
        st.write("Add transactions first.")
    else:
        st.subheader("Cumulative Portfolio Value vs. Net Invested")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=total_value.index, y=total_value.values,
                                 mode="lines", name="Portfolio Value",
                                 line=dict(color="#2E5CB8", width=2.5)))
        fig.add_trace(go.Scatter(x=net_invested.index, y=net_invested.values,
                                 mode="lines", name="Net Invested",
                                 line=dict(color="#C0392B", width=2, dash="dash")))
        fig.update_layout(hovermode="x unified", height=420,
                          margin=dict(l=20, r=20, t=30, b=20),
                          xaxis_title=None, yaxis_title="EUR",
                          legend=dict(orientation="h", y=-0.15))
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("---")
        st.subheader("Cumulative Time-Weighted Return")
        cum_twr = (1 + daily_ret).cumprod() - 1
        fig_twr = go.Figure()
        fig_twr.add_trace(go.Scatter(x=cum_twr.index, y=cum_twr.values * 100,
                                     mode="lines", name="Portfolio TWR",
                                     line=dict(color="#1D7F3E", width=2.5)))
        if "EPA:CW8" in prices.columns:
            bench = prices["EPA:CW8"].dropna()
            bench_ret = bench / bench.iloc[0] - 1
            fig_twr.add_trace(go.Scatter(x=bench_ret.index, y=bench_ret.values * 100,
                                         mode="lines", name="MSCI World (CW8)",
                                         line=dict(color="#888", width=1.5, dash="dot")))
        fig_twr.update_layout(hovermode="x unified", height=380,
                              margin=dict(l=20, r=20, t=30, b=20),
                              yaxis_title="%", xaxis_title=None,
                              legend=dict(orientation="h", y=-0.15))
        st.plotly_chart(fig_twr, use_container_width=True)

        st.markdown("---")
        st.subheader("Annual P&L (€)")
        yearly_value = total_value.resample("YE").last()
        yearly_invested = net_invested.resample("YE").last()
        prev_v = yearly_value.shift(1).fillna(0)
        prev_i = yearly_invested.shift(1).fillna(0)
        annual_pnl = (yearly_value - prev_v) - (yearly_invested - prev_i)
        annual_pnl.index = annual_pnl.index.year
        colors = ["#1D7F3E" if v >= 0 else "#C0392B" for v in annual_pnl.values]
        fig_ann = go.Figure(go.Bar(x=annual_pnl.index.astype(str), y=annual_pnl.values,
                                    marker_color=colors,
                                    text=[fmt_eur(v) for v in annual_pnl.values],
                                    textposition="outside"))
        fig_ann.update_layout(height=360, margin=dict(l=20, r=20, t=30, b=20),
                              yaxis_title="€", xaxis_title="Year", showlegend=False)
        st.plotly_chart(fig_ann, use_container_width=True)

        st.markdown("---")
        st.subheader("Cumulative Market Value per Instrument")
        vpi = positions * prices[positions.columns]
        vpi = vpi.fillna(0)
        fig_inst = go.Figure()
        for t in vpi.columns:
            if vpi[t].sum() > 0:
                fig_inst.add_trace(go.Scatter(x=vpi.index, y=vpi[t].values,
                                               mode="lines", name=t, stackgroup="one"))
        fig_inst.update_layout(hovermode="x unified", height=420,
                               margin=dict(l=20, r=20, t=30, b=20),
                               yaxis_title="EUR", xaxis_title=None,
                               legend=dict(orientation="h", y=-0.15))
        st.plotly_chart(fig_inst, use_container_width=True)

# -----------------------------------------------------------------
# Analysis
# -----------------------------------------------------------------
with tab_analysis:
    if transactions.empty:
        st.write("Add transactions first.")
    else:
        st.subheader("Risk & Return Metrics")
        sharpe = (ann_ret_si - rf) / ann_vol if ann_vol > 0 else 0
        mdd = max_drawdown(daily_ret)
        best_day = daily_ret.max() if not daily_ret.empty else 0
        worst_day = daily_ret.min() if not daily_ret.empty else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Annualized Return", fmt_pct(ann_ret_si))
        c2.metric("Annualized Volatility", fmt_pct(ann_vol))
        c3.metric("Sharpe Ratio", f"{sharpe:.2f}", f"rf = {fmt_pct(rf)}")
        c4.metric("Max Drawdown", fmt_pct(mdd))

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Best Day", fmt_pct(best_day))
        c2.metric("Worst Day", fmt_pct(worst_day))
        pos_days = (daily_ret > 0).sum() / max(len(daily_ret[daily_ret != 0]), 1)
        c3.metric("% Positive Days", f"{pos_days*100:.1f}%")
        mv_by = {t: float(positions[t].iloc[-1]) * float(prices[t].iloc[-1])
                 for t in tickers_used if t in prices.columns}
        tot_mv = sum(mv_by.values())
        concentration = max(mv_by.values()) / tot_mv if tot_mv > 0 else 0
        c4.metric("Top Position Share", f"{concentration*100:.1f}%")

        st.markdown("---")
        st.subheader("Drawdown Curve")
        cum = (1 + daily_ret).cumprod()
        dd = (cum - cum.cummax()) / cum.cummax()
        fig_dd = go.Figure(go.Scatter(x=dd.index, y=dd.values * 100, mode="lines",
                                       fill="tozeroy",
                                       line=dict(color="#C0392B", width=1.5),
                                       fillcolor="rgba(192,57,43,0.2)"))
        fig_dd.update_layout(hovermode="x unified", height=280,
                             margin=dict(l=20, r=20, t=30, b=20),
                             yaxis_title="%", showlegend=False)
        st.plotly_chart(fig_dd, use_container_width=True)

        st.markdown("---")
        st.subheader("Geographic Exposure (Look-Through)")
        geo_agg = {}
        tot_mv_nz = tot_mv if tot_mv > 0 else 1
        for t in tickers_used:
            mv = mv_by.get(t, 0)
            w = mv / tot_mv_nz
            for region, wr in GEO_EXPOSURE.get(t, {}).items():
                geo_agg[region] = geo_agg.get(region, 0) + w * wr

        if geo_agg:
            geo_df = pd.DataFrame(sorted(geo_agg.items(), key=lambda kv: -kv[1]),
                                  columns=["Region", "Weight"])
            geo_df["Weight %"] = geo_df["Weight"] * 100
            col1, col2 = st.columns([2, 1])
            with col1:
                fig_geo = px.pie(geo_df, values="Weight", names="Region",
                                 color_discrete_sequence=px.colors.qualitative.Set2)
                fig_geo.update_traces(textposition="inside", textinfo="percent+label")
                fig_geo.update_layout(height=400, margin=dict(l=0, r=0, t=20, b=20),
                                      showlegend=False)
                st.plotly_chart(fig_geo, use_container_width=True)
            with col2:
                st.dataframe(geo_df[["Region", "Weight %"]], hide_index=True,
                             use_container_width=True,
                             column_config={"Weight %": st.column_config.NumberColumn(format="%.1f%%")})

# -----------------------------------------------------------------
# Transactions (editor)
# -----------------------------------------------------------------
with tab_tx:
    st.subheader("✏️ Transactions")
    st.caption(
        "Add a new trade below, or edit the table directly. "
        "Click **Save** to persist changes."
    )

    # --- Quick-add form ---
    with st.expander("➕ Add a new transaction", expanded=st.session_state.transactions.empty):
        with st.form("add_tx_form", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns([1.2, 1.5, 1, 1.2])
            new_date = c1.date_input("Date", value=date.today(), format="YYYY-MM-DD")
            new_ticker = c2.selectbox(
                "Ticker",
                options=list(INSTRUMENTS.keys()),
                format_func=lambda t: f"{t} — {INSTRUMENTS[t]['name'][:40]}",
            )
            new_qty = c3.number_input("Quantity (negative = sell)", value=0.0, step=1.0, format="%.4f")
            new_price = c4.number_input("Unit price (€)", value=0.0, step=0.01, format="%.4f", min_value=0.0)

            submitted = st.form_submit_button("Add transaction", type="primary")
            if submitted:
                if new_qty == 0:
                    st.warning("Quantity cannot be zero.")
                elif new_price <= 0:
                    st.warning("Unit price must be greater than zero.")
                else:
                    new_row = pd.DataFrame([{
                        "Date": new_date, "Ticker": new_ticker,
                        "Quantity": new_qty, "Price": new_price,
                    }])
                    st.session_state.transactions = pd.concat(
                        [st.session_state.transactions, new_row], ignore_index=True
                    )
                    st.session_state.transactions = storage.normalize_transactions(
                        st.session_state.transactions
                    )
                    ok, msg = persist(st.session_state.transactions)
                    st.session_state.last_save_ok = (ok, msg)
                    st.rerun()

    # --- Inline editor ---
    st.markdown("**All transactions** (editable — tick the checkbox to add rows)")

    edited = st.data_editor(
        st.session_state.transactions,
        num_rows="dynamic",
        use_container_width=True,
        key="tx_editor",
        column_config={
            "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD", required=True),
            "Ticker": st.column_config.SelectboxColumn(
                "Ticker", options=list(INSTRUMENTS.keys()), required=True
            ),
            "Quantity": st.column_config.NumberColumn("Quantity", format="%.4f", required=True),
            "Price": st.column_config.NumberColumn("Unit Price (€)", format="%.4f",
                                                    min_value=0.0, required=True),
        },
        hide_index=True,
    )

    c1, c2, c3 = st.columns([1, 1, 3])
    if c1.button("💾 Save changes", type="primary"):
        try:
            cleaned = storage.normalize_transactions(edited)
            st.session_state.transactions = cleaned
            ok, msg = persist(cleaned)
            st.session_state.last_save_ok = (ok, msg)
            if ok:
                st.success(msg)
            else:
                st.error(msg)
            st.rerun()
        except Exception as e:
            st.error(f"Could not save: {e}")

    if c2.button("↶ Discard changes"):
        st.session_state.loaded_once = False
        load_initial_data()
        st.rerun()

    if st.session_state.last_save_ok:
        ok, msg = st.session_state.last_save_ok
        (st.success if ok else st.error)(msg)

    st.markdown("---")
    st.markdown("**Bulk import from CSV / XLSX**")
    st.caption("Replace all current transactions with rows from an uploaded file. "
               "Required columns: Date, Ticker, Quantity, Price.")

    uploaded = st.file_uploader("Upload file", type=["csv", "xlsx", "xls"])
    if uploaded is not None:
        try:
            imported = storage.load_from_csv_bytes(uploaded)
            st.write(f"Parsed **{len(imported)}** transactions:")
            st.dataframe(imported.head(10), use_container_width=True, hide_index=True)
            if st.button("Replace all transactions with this file", type="primary"):
                st.session_state.transactions = imported
                ok, msg = persist(imported)
                st.session_state.last_save_ok = (ok, msg)
                st.rerun()
        except Exception as e:
            st.error(f"Could not parse file: {e}")

    st.markdown("---")
    st.markdown("**Export / download**")
    if not st.session_state.transactions.empty:
        csv_bytes = st.session_state.transactions.to_csv(index=False).encode()
        st.download_button("⬇️ Download as CSV", csv_bytes,
                           file_name=f"transactions_{date.today().isoformat()}.csv",
                           mime="text/csv")
