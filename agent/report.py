"""Renders the research note: one self-contained HTML page with the 8 required elements."""
from __future__ import annotations

import html
import json
import time

E = lambda s: html.escape("" if s is None else str(s))  # noqa: E731

GROUP_LABEL = {
    "company_disclosure": "Company filing",
    "management_commentary": "Management commentary",
    "rating_agency": "Rating agency",
    "regulator": "Regulator",
    "peer_disclosure": "Peer",
    "news": "News",
    "market_data": "Market data",
    "other": "Other",
}


def _link(url: str, text: str) -> str:
    if not str(url).startswith(("http://", "https://")):
        return E(text)
    return f'<a href="{E(url)}" target="_blank" rel="noopener">{E(text)}</a>'


def _claim_row(c: dict) -> str:
    val = "" if c.get("value") is None else f"{c['value']:g} {c.get('unit') or ''}"
    page = f" · p.{c['page']}" if c.get("page") else ""
    tick = '<span class="ok">✓ quote verified</span>' if c.get("verified") else '<span class="bad">unverified</span>'
    return f"""<tr id="{E(c['id'])}"><td class="mono">{E(c['id'])}</td>
<td><b>{E(c['statement'])}</b><blockquote>“{E(c['quote'])}”</blockquote>
<div class="src"><span class="pill">{E(GROUP_LABEL.get(c.get('group'), c.get('group')))}</span> {_link(c['url'], c.get('title') or c['url'])}{E(page)} · {tick}</div></td>
<td class="mono nowrap">{E(c.get('period') or '')}<br>{E(val)}</td></tr>"""


def _refs(ids) -> str:
    return " ".join(f'<a class="ref" href="#{E(i)}">{E(i)}</a>' for i in ids or [])


def render(r: dict) -> str:
    p, f, h = r.get("profile", {}), r.get("final", {}), r.get("chosen")
    name = p.get("company_name") or r["company"]
    strength = h["strength"] if h else "None"
    used = r.get("used_claims", [])
    sup_ids = set(h["supporting_claims"]) if h else set()
    counter_ids = set(h.get("judge", {}).get("counter_claim_ids", [])) if h else set()
    support_rows = "".join(_claim_row(c) for c in used if c["id"] in sup_ids)
    counter_ids -= sup_ids
    counter_rows = "".join(_claim_row(c) for c in used if c["id"] in counter_ids)
    other_rows = "".join(_claim_row(c) for c in used if c["id"] not in sup_ids | counter_ids)

    if h:
        why = "".join(f"<li>{E(x)}</li>" for x in (f.get("why_it_matters") or []) if isinstance(f.get("why_it_matters"), list))
        tl = "".join(
            f'<li><span class="when">{E(ev.get("period"))}</span><span>{E(ev.get("event"))} {_refs(ev.get("claim_ids"))}</span></li>'
            for ev in f.get("timeline", []))
        nums = "".join(
            f"<tr><td>{E(n.get('label'))}<div class='muted'>{E(n.get('note'))}</div></td>"
            f"<td class='big mono'>{n['value']:,.2f} {E(n.get('unit'))}</td>"
            f"<td class='mono muted'>{E(n.get('formula'))}<br>{_refs(n.get('inputs'))}</td></tr>"
            for n in f.get("numbers", []))
        alts = "".join(
            f"<li><b>{E(a.get('name'))}</b> — explains it away: <i>{E(a.get('explains_away'))}</i>. {E(a.get('finding'))} {_refs(a.get('claim_ids'))}</li>"
            for a in h.get("judge", {}).get("alternatives_assessed", []) if isinstance(a, dict))
        main = f"""
<section class="hero">
  <div class="eyebrow">The discovery</div>
  <h1>{E(f.get('discovery') or h['hypothesis'])}</h1>
  <div class="badges"><span class="strength s-{E(strength.lower())}">Evidence strength: {E(strength)}</span>
  <span class="muted">{E(f.get('strength_reasoning') or '; '.join(h.get('strength_reasons', [])))}</span></div>
  <p class="muted" style="margin:10px 0 0"><b>Already public?</b> {E(h.get('judge', {}).get('already_public', 'partly'))} —
  {E(h.get('judge', {}).get('already_public_note', ''))}</p>
</section>
<div class="grid">
  <section><h2>Why it matters</h2><ul>{why}</ul></section>
  <section><h2>Timeline</h2><ol class="timeline">{tl or '<li class="muted">No dated steps.</li>'}</ol></section>
</div>
<section><h2>Charts</h2><div class="charts" id="charts"></div><p class="muted" id="nochart"></p></section>
<section><h2>Numbers</h2><p class="muted">Computed in code from the cited figures, not by the AI model.</p>
<table class="nums">{nums or '<tr><td class="muted">No calculations survived checking.</td></tr>'}</table></section>
<div class="grid">
  <section class="counter"><h2>Strongest counterargument</h2><p>{E(f.get('counterargument') or h.get('judge', {}).get('strongest_counterargument'))}</p>
  <h3>Alternatives the red team tested</h3><ul>{alts}</ul></section>
  <section class="falsify"><h2>Falsification condition</h2><p>{E(f.get('falsification_condition'))}</p>
  <h3>Kill criteria set before the evidence was gathered</h3><ul>{''.join(f'<li>{E(k)}</li>' for k in h.get('kill_criteria', []))}</ul></section>
</div>
<section><h2>Evidence trail</h2>
<h3>Supporting evidence</h3><table class="ev">{support_rows}</table>
<h3>Counter-evidence</h3><table class="ev">{counter_rows or '<tr><td class="muted">None cited.</td></tr>'}</table>
{'<h3>Other evidence used in the timeline and numbers</h3><table class="ev">' + other_rows + '</table>' if other_rows else ''}
</section>"""
    elif r.get("candidates"):
        main = """<section class="hero"><div class="eyebrow">No discovery</div>
<h1>No hypothesis survived the red team.</h1><p>Every candidate was explained away by an ordinary cause or contradicted
by evidence. The reasoning log below shows what was tested and why each failed.</p></section>"""
    else:
        main = """<section class="hero"><div class="eyebrow">No discovery</div>
<h1>Not enough verified evidence to form a hypothesis.</h1><p>Too few claims could be checked word for word against
their sources. The sources read and the run trace below show where the research fell short.</p></section>"""

    cands = "".join(
        f"""<tr><td class="mono">{E(c['id'])}</td><td>{E(c['hypothesis'])}<div class="muted">{E(c.get('judge', {}).get('reason'))}</div></td>
<td><span class="strength s-{E(c.get('strength', 'weak').lower())}">{E(c.get('strength'))}</span></td></tr>"""
        for c in r.get("candidates", []))
    angles = "".join(f"<li><b>{E(a['name'])}</b>: {E(a.get('question'))} <span class='muted'>({E(a.get('why'))})</span></li>"
                     for a in r.get("angles", []))
    contra = "".join(f"<li>{E(x['entity'])} · {E(x['metric'])} · {E(x['period'])}: {E(x['detail'])}</li>"
                     for x in r.get("contradictions", [])) or "<li class='muted'>None found.</li>"
    sources = "".join(f"<li class='mono'>{E(d['id'])} <span class='pill'>{E(d['source_type'])}</span> {_link(d['url'], d.get('title') or d['url'])} <span class='muted'>({E(d['origin'])})</span></li>"
                      for d in r.get("docs", []))
    trace = "\n".join(f"{t['time']}  {t['msg']}" for t in r.get("trace", []))
    st = r.get("stats", {})
    charts = f.get("charts", []) if h else []
    prices = r.get("prices")
    chart_data = json.dumps({"series": charts, "prices": prices}, ensure_ascii=False).replace("</", "<\\/")
    peers = ", ".join(x["name"] for x in r.get("peers", []))

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Falsify · {E(name)}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{{--bg:#F5F2EA;--card:#FBFAF6;--ink:#1F2A3D;--head:#14213D;--soft:#5A6270;--rule:#DCD6C8;--acc:#A84F12;--blue:#2F5F8F;--good:#2E6B3F;--bad:#A3341F;--warn:#8A6100}}
@media (prefers-color-scheme:dark){{:root{{--bg:#101B2D;--card:#16233A;--ink:#DCE3EC;--head:#F4F1EA;--soft:#9AA7B8;--rule:#2C3B55;--acc:#F0A458;--blue:#7FB0E0;--good:#7CC48F;--bad:#F08A73;--warn:#E8C26A}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:28px 16px 60px}}
header.top{{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;border-bottom:2px solid var(--head);padding-bottom:12px;margin-bottom:20px}}
.brand{{font:700 26px Georgia,serif;color:var(--head)}} .muted{{color:var(--soft);font-size:13px}}
section{{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:18px 20px;margin-bottom:16px;min-width:0}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}} .grid>section{{margin-bottom:0}} .grid{{margin-bottom:16px}}
@media (max-width:760px){{.grid{{grid-template-columns:1fr}}}}
h1{{font:700 26px/1.3 Georgia,serif;color:var(--head);margin:6px 0 12px}} h2{{font-size:13px;letter-spacing:1.5px;text-transform:uppercase;color:var(--acc);margin:0 0 10px}}
h3{{font-size:14px;margin:16px 0 8px;color:var(--head)}} .eyebrow{{font-size:12px;letter-spacing:2px;text-transform:uppercase;color:var(--acc);font-weight:700}}
.hero{{border-left:5px solid var(--acc)}} .badges{{display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
.strength{{display:inline-block;padding:3px 10px;border-radius:99px;font-weight:700;font-size:13px;border:1.5px solid currentColor}}
.s-strong{{color:var(--good)}} .s-moderate{{color:var(--blue)}} .s-weak{{color:var(--warn)}} .s-killed{{color:var(--bad)}} .s-none{{color:var(--soft)}}
.timeline{{list-style:none;padding:0;margin:0}} .timeline li{{display:grid;grid-template-columns:90px 1fr;gap:10px;padding:6px 0;border-bottom:1px solid var(--rule)}}
.when{{font-weight:700;color:var(--blue);font-family:ui-monospace,Menlo,monospace;font-size:13px}}
table{{width:100%;border-collapse:collapse}} td{{padding:9px 8px;border-bottom:1px solid var(--rule);vertical-align:top}}
.nums .big{{font-size:20px;font-weight:700;color:var(--head);white-space:nowrap}} .mono{{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}} .nowrap{{white-space:nowrap}}
blockquote{{margin:6px 0;padding:4px 10px;border-left:3px solid var(--rule);color:var(--soft);font-size:13.5px}}
.src{{font-size:12.5px}} .pill{{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;background:var(--bg);border:1px solid var(--rule);color:var(--soft)}}
.ok{{color:var(--good)}} .bad{{color:var(--bad)}} a{{color:var(--blue)}} a.ref{{font-family:ui-monospace,Menlo,monospace;font-size:12px;text-decoration:none;border:1px solid var(--rule);border-radius:4px;padding:0 4px}}
.counter{{border-top:4px solid var(--bad)}} .falsify{{border-top:4px solid var(--blue)}}
.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}} .chartbox{{position:relative;height:260px}}
details summary{{cursor:pointer;font-weight:600;color:var(--head)}} pre{{white-space:pre-wrap;font-size:12px;max-height:420px;overflow:auto;background:var(--bg);padding:10px;border-radius:6px}}
tr:target{{background:color-mix(in srgb,var(--acc) 12%,transparent)}}
</style></head><body><div class="wrap">
<header class="top"><div><div class="brand">Falsify</div><div class="muted">Autonomous research note · Track A</div></div>
<div><b>{E(name)}</b> <span class="muted">({E(r.get('subtype', '').replace('_', ' '))}{' · ' + E(p.get('nse_symbol')) if p.get('nse_symbol') else ''})</span><br>
<span class="muted">Peers: {E(peers)} · {time.strftime('%d %b %Y, %H:%M')}</span></div></header>
{main}
<section><h2>Reasoning log</h2>
<h3>Angles investigated</h3><ul>{angles}</ul>
<h3>Hypotheses tested</h3><table>{cands}</table>
<h3>Numeric mismatches between sources</h3><ul>{contra}</ul>
<details><summary>All {len(r.get('docs', []))} sources read</summary><ul>{sources}</ul></details>
<details><summary>Full run trace</summary><pre>{E(trace)}</pre></details>
<p class="muted">{st.get('sources')} sources · {st.get('verified')} of {st.get('claims')} extracted claims had their quote verified in the source ·
{st.get('llm_calls')} model calls (Gemini {st.get('gemini_calls', '?')}, Groq {st.get('groq_calls', '?')}) · {st.get('minutes')} min. Automated research from public sources; check the linked sources before relying on it. Not investment advice.</p>
</section></div>
<script>
const D = {chart_data};
(function(){{
  const box = document.getElementById('charts'); if(!box) return;
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue('--soft').trim(), grid = css.getPropertyValue('--rule').trim();
  const colors = [css.getPropertyValue('--acc').trim(), css.getPropertyValue('--blue').trim()];
  // Built-in chart used when the interactive charting library can't load (no internet): same data, drawn as SVG
  function svgChart(labels, values, color){{
    const W = 600, H = 250, L = 64, R = 20, T = 14, B = 34, n = values.length;
    let lo = Math.min(...values), hi = Math.max(...values);
    if (lo === hi) {{ lo -= 1; hi += 1; }}
    const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    const x = i => L + (n === 1 ? (W - L - R) / 2 : i * (W - L - R) / (n - 1));
    const y = v => T + (hi - v) * (H - T - B) / (hi - lo);
    const fmt = v => Math.abs(v) >= 1000 ? Math.round(v).toLocaleString('en-IN') : (+v.toFixed(2)).toString();
    const pts = values.map((v, i) => x(i).toFixed(1) + ',' + y(v).toFixed(1)).join(' ');
    let g = '';
    for (let k = 0; k <= 3; k++) {{
      const v = lo + (hi - lo) * k / 3, yy = y(v).toFixed(1);
      g += `<line x1="${{L}}" x2="${{W - R}}" y1="${{yy}}" y2="${{yy}}" stroke="${{grid}}" stroke-width="1"/>` +
           `<text x="${{L - 8}}" y="${{yy}}" fill="${{ink}}" font-size="12" text-anchor="end" dominant-baseline="middle">${{fmt(v)}}</text>`;
    }}
    const step = Math.max(1, Math.ceil(n / 6));
    labels.forEach((lab, i) => {{
      if (i % step === 0 || i === n - 1)
        g += `<text x="${{x(i).toFixed(1)}}" y="${{H - 10}}" fill="${{ink}}" font-size="12" text-anchor="${{i === n - 1 && n > 1 ? 'end' : (i === 0 && n > 1 ? 'start' : 'middle')}}">${{String(lab).replace(/[<>&]/g, '')}}</text>`;
    }});
    const dots = n <= 24 ? values.map((v, i) => `<circle cx="${{x(i).toFixed(1)}}" cy="${{y(v).toFixed(1)}}" r="3.5" fill="${{color}}"/>`).join('') : '';
    return `<svg viewBox="0 0 ${{W}} ${{H}}" width="100%" height="100%" role="img">${{g}}` +
           `<polyline points="${{pts}}" fill="none" stroke="${{color}}" stroke-width="2.5" stroke-linejoin="round"/>${{dots}}</svg>`;
  }}
  function add(title, labels, values, color){{
    const wrap = document.createElement('div');
    wrap.innerHTML = '<div class="muted" style="margin-bottom:6px"></div><div class="chartbox"><canvas></canvas></div>';
    wrap.firstChild.textContent = title; box.appendChild(wrap);
    if (typeof Chart === 'undefined') {{ wrap.lastChild.innerHTML = svgChart(labels, values, color); return; }}
    new Chart(wrap.querySelector('canvas'), {{type:'line', data:{{labels, datasets:[{{data:values, borderColor:color, backgroundColor:color, tension:.25, pointRadius:3}}]}},
      options:{{maintainAspectRatio:false, plugins:{{legend:{{display:false}}}}, scales:{{x:{{ticks:{{color:ink}}, grid:{{color:grid}}}}, y:{{ticks:{{color:ink}}, grid:{{color:grid}}}}}}}}}});
  }}
  (D.series||[]).forEach((s,i)=>add(s.label, s.points.map(p=>p.period), s.points.map(p=>p.value), colors[i%2]));
  if (D.prices && D.prices.points) add('Share price, monthly close (Yahoo Finance, ' + D.prices.symbol + ')', D.prices.points.map(p=>p.date), D.prices.points.map(p=>p.close), colors[1]);
  if (!box.children.length) document.getElementById('nochart').textContent = 'Not enough multi-period data for a chart.';
}})();
</script></body></html>"""
