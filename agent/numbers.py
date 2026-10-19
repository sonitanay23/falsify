"""Numbers done in code, not by the model: periods, time series, trend breaks,
cross-source reconciliation and safe evaluation of formulas over claim values."""
from __future__ import annotations

import ast
import operator
import re
from statistics import mean, pstdev

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _fy(y: str) -> int:
    n = int(y)
    return n + 2000 if n < 100 else n


def parse_period(s) -> tuple | None:
    """Indian financial-year periods -> (fy, q). q = 1-4 for quarters, 5 for full year.
    FY26 = April 2025 to March 2026. Accepts FY26Q1, Q1FY26, Q1 FY2026, FY25, FY2024-25, 2024-25,
    Jun 2025, June 30 2026, 31 March 2026, March 26. Half-years and nine-month figures return None."""
    if s is None:
        return None
    t = str(s).strip().lower().replace("’", "'")
    t = re.sub(r"[\s_\-–/']+", " ", t)
    if re.search(r"\b(h[12]|[69] ?m|nine months?|six months?|half year|ytd|9mfy|h[12]fy)", t):
        return None  # not a quarter or a full year: never mix with them
    m = re.search(r"fy\s*(\d{2,4})\s*q\s*([1-4])", t)
    if m:
        return (_fy(m.group(1)), int(m.group(2)))
    m = re.search(r"q\s*([1-4])\s*(?:of\s*)?fy\s*(\d{2,4})", t)
    if m:
        return (_fy(m.group(2)), int(m.group(1)))
    m = re.search(r"([1-4])q\s*fy\s*(\d{2,4})", t)
    if m:
        return (_fy(m.group(2)), int(m.group(1)))
    m = re.search(r"(?:fy\s*)?(20\d{2})\s+(\d{2}|20\d{2})\b", t)  # 2024-25 or FY2024-25
    if m:
        return (_fy(m.group(2)) if len(m.group(2)) == 4 else int(m.group(1)) + 1, 5)
    m = re.search(r"fy\s*(\d{2,4})\b", t)
    if m:
        return (_fy(m.group(1)), 5)
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?", t)
    if m:
        rest = t[m.end():]
        year = re.search(r"\b(20\d{2})\b", t)  # a four-digit year anywhere wins
        if year:
            yr = int(year.group(1))
        else:
            two = re.match(r"\s*,?\s*(\d{2})\b(?!\s*,?\s*\d)", rest)  # "March 26" = March 2026
            if not two:
                return None
            yr = 2000 + int(two.group(1))
        import datetime as _dt
        if not 2010 <= yr <= _dt.date.today().year + 1:  # "June 30" with no year is a day, not 2030
            return None
        mo = _MONTHS[m.group(1)]
        fy = yr + 1 if mo >= 4 else yr
        return (fy, ((mo - 4) % 12) // 3 + 1)
    return None


def period_label(p: tuple) -> str:
    fy, q = p
    return f"FY{fy % 100:02d}" if q == 5 else f"Q{q} FY{fy % 100:02d}"


def build_series(claims: list[dict]) -> dict:
    """Group verified numeric claims into series keyed by (entity, metric, unit, frequency)."""
    series: dict = {}
    for c in claims:
        if not c.get("verified") or not isinstance(c.get("value"), (int, float)):
            continue
        p = parse_period(c.get("period"))
        if not p:
            continue
        freq = "annual" if p[1] == 5 else "quarterly"
        ent = c["entity_key"] + (f"|{c['basis']}" if c.get("basis") else "")
        key = (ent, c["metric"], (c.get("unit") or "").strip().lower(), freq)
        pts = series.setdefault(key, {})
        pts.setdefault(p, []).append(c)
    out = {}
    for key, pts in series.items():
        ordered = sorted(pts.items())
        out[key] = [{"period": period_label(p), "key": p, "value": cs[0]["value"], "claim_ids": [x["id"] for x in cs]}
                    for p, cs in ordered]
    return out


def series_stats(points: list[dict]) -> dict:
    vals = [p["value"] for p in points]
    st = {"n": len(points), "first": points[0]["period"], "last": points[-1]["period"],
          "first_value": vals[0], "last_value": vals[-1], "change": vals[-1] - vals[0]}
    if vals[0]:
        st["change_pct"] = round((vals[-1] - vals[0]) / abs(vals[0]) * 100, 2)
    if len(vals) >= 5:
        best, where = 0.0, None
        sd = pstdev(vals) or 1.0
        for k in range(2, len(vals) - 1):
            shift = abs(mean(vals[k:]) - mean(vals[:k])) / sd
            if shift > best:
                best, where = shift, points[k]["period"]
        if where and best >= 1.0:
            st["trend_break_at"] = where
            st["trend_break_strength"] = round(best, 2)
    return st


def reconcile(claims: list[dict], tolerance: float = 0.02) -> list[dict]:
    """Same entity, metric and period reported with different values by different documents."""
    groups: dict = {}
    for c in claims:
        if not c.get("verified") or not isinstance(c.get("value"), (int, float)):
            continue
        if c.get("source_type") == "market_data":
            continue  # Yahoo's definitions differ from company filings; not a fair comparison
        p = parse_period(c.get("period"))
        if not p:
            continue
        groups.setdefault((c["entity_key"], c.get("basis") or "", c["metric"], (c.get("unit") or "").lower(), p), []).append(c)
    issues = []
    for (ent, basis, metric, unit, p), cs in groups.items():
        docs = {c["doc_id"] for c in cs}
        if len(docs) < 2:
            continue
        vals = [c["value"] for c in cs]
        lo, hi = min(vals), max(vals)
        if hi == lo:
            continue
        base = max(abs(lo), abs(hi)) or 1.0
        if (hi - lo) / base > tolerance:
            issues.append({
                "type": "numeric_mismatch",
                "entity": cs[0]["entity"],
                "metric": metric,
                "period": period_label(p),
                "detail": "; ".join(f"{c['value']} {c.get('unit') or ''} ({c['id']}, {c['source_type']})" for c in cs),
                "claim_ids": [c["id"] for c in cs],
            })
    return issues


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def eval_formula(expr: str, values: dict) -> float:
    """Evaluate arithmetic over claim ids, e.g. '(C12 - C7) / C7 * 100'. Nothing else is allowed."""
    tree = ast.parse(expr, mode="eval")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name) and n.id in values:
            return float(values[n.id])
        raise ValueError(f"not allowed in a formula: {ast.dump(n)[:60]}")

    return ev(tree)


def latest_periods(today=None, results_lag_days: int = 50, annual_lag_days: int = 75) -> dict:
    """Which quarter and financial year should already be reported by `today` (Indian FY)."""
    import datetime as _dt

    today = today or _dt.date.today()
    ends = []
    for y in (today.year - 1, today.year):
        ends += [(_dt.date(y, 3, 31), 4, y), (_dt.date(y, 6, 30), 1, y + 1),
                 (_dt.date(y, 9, 30), 2, y + 1), (_dt.date(y, 12, 31), 3, y + 1)]
    done = [(e, q, fy) for e, q, fy in ends if (today - e).days >= results_lag_days]
    e, q, fy = max(done)
    fy_done = today.year if (today - _dt.date(today.year, 3, 31)).days >= annual_lag_days else today.year - 1
    lq = period_label((fy, q))
    lf = period_label((fy_done, 5))
    yy = fy_done % 100
    tokens = {lq.lower(), lq.lower().replace(" ", ""), lf.lower(), f"fy{fy_done}", f"{fy_done - 1}-{yy:02d}",
              f"fy{(fy % 100):02d}", f"{fy - 1}-{fy % 100:02d}", str(today.year)}
    return {"today": today.isoformat(), "latest_quarter": lq, "latest_year": lf,
            "previous_year": period_label((fy_done - 1, 5)), "recent_tokens": sorted(tokens)}
