"""Optional Yahoo Finance data via yfinance (free, no key). Skipped quietly if unavailable."""
from __future__ import annotations

from . import log

# Tests can replace this with a fake module.
YF = None


def _yf():
    if YF is not None:
        return YF
    try:
        import yfinance

        return yfinance
    except ImportError:
        return None


def symbol_for(nse_symbol: str | None) -> str | None:
    if not nse_symbol:
        return None
    s = nse_symbol.strip().upper().replace("NSE:", "").replace(" ", "")
    return s if s.endswith((".NS", ".BO")) else s + ".NS"


def price_history(symbol: str | None) -> dict | None:
    """Monthly closing prices for five years, plus simple changes."""
    yf = _yf()
    if not yf or not symbol:
        return None
    try:
        hist = yf.Ticker(symbol).history(period="5y", interval="1mo")
        if hist is None or len(hist) < 6:
            return None
        pts = [{"date": idx.strftime("%Y-%m"), "close": round(float(row["Close"]), 2)}
               for idx, row in hist.iterrows() if row.get("Close") == row.get("Close")]
        out = {"symbol": symbol, "points": pts, "source": f"https://finance.yahoo.com/quote/{symbol}"}
        last = pts[-1]["close"]
        if len(pts) > 12 and pts[-13]["close"]:
            out["change_1y_pct"] = round((last / pts[-13]["close"] - 1) * 100, 1)
        if pts[0]["close"]:
            out["change_5y_pct"] = round((last / pts[0]["close"] - 1) * 100, 1)
        return out
    except Exception as e:
        log.detail(f"Yahoo Finance price data unavailable ({type(e).__name__})")
        return None


def annual_figures(symbol: str | None, entity: str) -> list[dict]:
    """Annual revenue and net income as claims tagged market_data."""
    yf = _yf()
    if not yf or not symbol:
        return []
    try:
        df = yf.Ticker(symbol).income_stmt
    except Exception as e:
        log.detail(f"Yahoo Finance financials unavailable ({type(e).__name__})")
        return []
    if df is None or getattr(df, "empty", True):
        return []
    rows = {"Total Revenue": "revenue", "Net Income": "pat"}
    out = []
    for row, metric in rows.items():
        if row not in df.index:
            continue
        for col in df.columns:
            v = df.loc[row, col]
            try:
                v = float(v)
            except Exception:
                continue
            if v != v:  # NaN
                continue
            fy = col.year + 1 if col.month >= 4 else col.year  # Indian FY ending in March
            period = f"FY{fy % 100:02d}"  # full-year figure, not a quarter
            out.append({
                "entity": entity, "basis": "consolidated", "metric": metric, "value": round(v / 1e7, 2), "unit": "rs crore",
                "period": period, "kind": "reported_figure",
                "statement": f"{entity} {row.lower()} for {period} (year ending {col.strftime('%b %Y')}): Rs {v / 1e7:,.0f} crore (Yahoo Finance).",
                "quote": f"Yahoo Finance income statement, {row}, {col.strftime('%Y-%m-%d')}", "page": None,
            })
    return out
