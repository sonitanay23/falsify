"""The research run: profile -> plan -> gather -> extract -> timeline -> hypothesize -> challenge -> grade -> write."""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, log, market, numbers, playbook, verify, web
from .llm import CALLS, LLMError, QuotaError, ask_json, available

FINANCE_RULES = (
    " Lending-metric rules you must respect: Gross NPA / Gross Stage 3 is BEFORE provisions, so provisions do not "
    "lower it; only write-offs, recoveries, upgrades or loan-book growth (the denominator) lower the gross ratio. "
    "Provisions lower NET NPA and raise credit cost. A falling NPA ratio during fast loan growth may be a denominator "
    "effect. Consolidated and standalone figures differ and must not be compared as if they were the same. Changes in "
    "ratios are expressed in percentage points or basis points (bps), not as a percent of the ratio. "
)
ANALYST = (
    "You are a meticulous equity research analyst covering Indian listed financial-services companies. "
    "Use ONLY the material given to you. Never invent numbers, quotes, dates, sources or URLs. "
    "Indian financial years: FY26 = April 2025 to March 2026; Q1 = Apr-Jun, Q2 = Jul-Sep, Q3 = Oct-Dec, Q4 = Jan-Mar. "
    "Write periods as 'Q1 FY26' for quarters and 'FY25' for full years. "
    "Do not give investment recommendations. Reply with valid JSON only." + FINANCE_RULES
)
RED_TEAM = (
    "You are a sceptical red-team analyst. Your only job is to find reasons a financial hypothesis is WRONG, "
    "overstated, or explained by something more ordinary. Be concrete and fair: if the evidence holds up, say so. "
    "Use ONLY the material given to you; never invent facts or sources. Reply with valid JSON only." + FINANCE_RULES
)

KIND_COMMENTARY = {"management_statement", "guidance"}
PRIMARY_GROUPS = {"company_disclosure", "regulator"}  # Yahoo Finance data is useful but not a primary document


def ekey(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


_STRIP_WORDS = {"limited", "ltd", "the", "india", "co", "company", "consolidated", "standalone", "group", "inc",
                "plc", "corp", "corporation", "s"}
_SELF_REFS = {"", "bank", "company", "we", "our", "us", "nbfc", "lender", "insurer", "firm", "management", "hfc"}
_GENERIC_ALIASES = {"bank", "finance", "financial", "financials", "insurance", "life", "limited", "india", "ltd",
                    "nbfc", "company", "group", "capital", "services", "holdings", "the"}


def _core(name: str) -> str:
    """Name with legal suffixes and filler removed: 'The Bajaj Finance Ltd.' -> 'bajajfinance'."""
    return "".join(w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _STRIP_WORDS)


def _same_entity(name: str, alias: str) -> bool:
    e, a = _core(name), _core(alias)
    if not e or not a:
        return False
    if e == a:
        return True
    # a short form of the alias ("Shriram" for "Shriram Finance"), never a longer name that only contains it
    # ("Bajaj Housing Finance" is a different company from "Bajaj Finance")
    return len(e) >= 5 and a.startswith(e) and len(a) - len(e) <= 12


def _ids(x) -> list[str]:
    """Normalise model-supplied claim ids: 'C12', ' c12', 12, ['C1, C2'] -> ['C12'] etc."""
    if x is None:
        return []
    items = x if isinstance(x, list) else [x]
    out = []
    for it in items:
        for m in re.findall(r"[Cc]?\s*(\d+)", str(it)):
            cid = f"C{int(m)}"
            if cid not in out:
                out.append(cid)
    return out


def _quoted_numbers(quote: str) -> set:
    return {float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", quote or "")}


def _list(x) -> list:
    if isinstance(x, list):
        return [i for i in x if i not in (None, "")]
    return [x] if isinstance(x, str) and x.strip() else []


def _norm_unit(unit: str, value):
    u = (unit or "").strip().lower().replace("₹", "rs ").replace("inr", "rs").replace(".", "")
    u = re.sub(r"\s+", " ", u).strip()
    if u in ("%", "percent", "per cent", "pct", "% yoy", "percentage"):
        return "%", value
    if u in ("bps", "bp", "basis points", "basis point"):
        return "bps", value
    if "lakh crore" in u or "lakh cr" in u:
        return "rs crore", (value * 100000 if isinstance(value, (int, float)) else value)
    if re.fullmatch(r"(rs )?(crore|crores|cr)", u) or u in ("rs crore", "rs cr", "crore rs"):
        return "rs crore", value
    if u in ("x", "times"):
        return "x", value
    return u[:20], value


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").replace("%", "").replace("₹", "").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


class Run:
    def __init__(self, company: str):
        self.company = company.strip()
        self.started = time.time()
        self.profile: dict = {}
        self.subtype = "other_financial"
        self.aliases: list[str] = [self.company]
        self.peers: list[dict] = []
        self.angles: list[dict] = []
        self.docs: dict = {}
        self.claims: list[dict] = []
        self.contradictions: list[dict] = []
        self.series: dict = {}
        self.candidates: list[dict] = []
        self.chosen: dict | None = None
        self.final: dict = {}
        self.prices: dict | None = None
        self._doc_n = 0
        self._claim_n = 0
        self._lock = threading.RLock()
        self.when = numbers.latest_periods()
        w = self.when
        date_line = (f" Today is {w['today']}. The most recent reported quarter should be {w['latest_quarter']} and the "
                     f"latest full financial year {w['latest_year']}. Prefer the newest data and treat older periods as "
                     "background. Never describe something that has already happened as a future event.")
        self.analyst = ANALYST + date_line
        self.red = RED_TEAM + date_line

    # ---------- helpers ----------
    def entity_key(self, name: str, about: str | None = None) -> str:
        """'company', 'peer:<name>' or the entity's own key. `about` is whose document the claim came from."""
        if _core(name) in _SELF_REFS:  # "the Bank", "Company", "we": whoever's document this is
            if about == "company":
                return "company"
            for p in self.peers:
                if about == p["name"]:
                    return "peer:" + ekey(p["name"])
            return ekey(name) or "unknown"
        if any(_same_entity(name, a) for a in self.aliases):
            return "company"
        for p in self.peers:
            if any(_same_entity(name, a) for a in [p["name"]] + _list(p.get("aliases"))):
                return "peer:" + ekey(p["name"])
        return ekey(name) or "unknown"

    def entity_name(self, key: str) -> str:
        if "|" in key:  # series keys carry the basis, e.g. "company|consolidated"
            base, basis = key.split("|", 1)
            return f"{self.entity_name(base)} ({basis})"
        if key == "company":
            return self.profile.get("company_name") or self.company
        if key.startswith("peer:"):
            for p in self.peers:
                if "peer:" + ekey(p["name"]) == key:
                    return p["name"]
        return key

    def relevant(self, item: dict) -> bool:
        blob = f"{item.get('title', '')} {item.get('snippet', '')} {item.get('url', '')}".lower()
        flat = ekey(blob)
        return any(len(_core(a)) >= 3 and (a.lower() in blob or _core(a) in flat) for a in self.aliases)

    def group_of(self, c: dict) -> str:
        if c["source_type"] == "market_data":
            return "market_data"
        if c["entity_key"].startswith("peer:"):
            return "peer_disclosure"
        if c.get("kind") in KIND_COMMENTARY:
            return "management_commentary"
        return {
            "company": "company_disclosure",
            "exchange_filing": "company_disclosure",
            "rating_agency": "rating_agency",
            "regulator": "regulator",
            "news": "news",
        }.get(c["source_type"], "other")

    def claim_line(self, c: dict, full: bool = False, short: bool = False) -> str:
        val = "" if c.get("value") is None else f"{c['value']:g} {c.get('unit') or ''}".strip()
        stmt = c["statement"][:150] if short else c["statement"]
        parts = [c["id"], c["entity"] + (f" ({c['basis']})" if c.get("basis") else ""), c["metric"],
                 c.get("period") or "-", val or "-", c.get("kind") or "-", c["group"], stmt]
        line = " | ".join(str(p) for p in parts)
        if full:
            line += f"\n    quote: \"{c['quote']}\"\n    source: {c['url']}" + (f" (page {c['page']})" if c.get("page") else "")
        return line

    def claims_by_id(self) -> dict:
        return {c["id"]: c for c in self.claims}

    # ---------- 1. profile ----------
    def step_profile(self) -> None:
        log.stage("1 · Profile")
        results = web.search(f"{self.company} investor relations") + web.search(f"{self.company} company business segments")
        listing = "\n".join(f"- {r['title']} | {r['url']} | {r['snippet'][:200]}" for r in results[:14])
        prompt = f"""Company given by the user: "{self.company}"

Web search results:
{listing}

Identify the company. Return JSON:
{{"company_name": "official name",
  "search_aliases": ["names and short forms used in news, e.g. abbreviations"],
  "nse_symbol": "NSE ticker or null",
  "subtype": one of {list(playbook.SUBTYPES)},
  "description": "one sentence on what it does",
  "website_domain": "its own website domain, e.g. example.com, or null",
  "segments": ["main business segments"],
  "peers": [{{"name": "listed Indian competitor of the same sub-type and similar size", "aliases": ["short forms"], "why": "one line"}}]}}
Give exactly 3 peers. Use your general knowledge only to identify the company and its peers."""
        p = ask_json(self.analyst, prompt, tag="profile")
        self.profile = p if isinstance(p, dict) else {}
        st = str(self.profile.get("subtype", "")).lower().strip()
        self.subtype = st if st in playbook.SUBTYPES else "other_financial"
        names = [self.company, self.profile.get("company_name") or self.company] + \
            [a for a in _list(self.profile.get("search_aliases")) if isinstance(a, str)]
        self.aliases = list(dict.fromkeys(a.strip() for a in names
                                          if len(_core(a)) >= 2 and _core(a) not in _GENERIC_ALIASES))
        peers = []
        for pp in _list(self.profile.get("peers")):
            if not isinstance(pp, dict) or not pp.get("name"):
                continue
            if any(_same_entity(pp["name"], a) for a in self.aliases):
                continue  # the model listed the company as its own peer
            pp["aliases"] = [a for a in _list(pp.get("aliases")) if isinstance(a, str) and _core(a) not in _GENERIC_ALIASES]
            peers.append(pp)
        self.peers = peers[:3]
        log.info(f"{self.profile.get('company_name', self.company)} — sub-type: {self.subtype.replace('_', ' ')}")
        log.detail(self.profile.get("description", ""))
        log.info("Peers: " + (", ".join(pp["name"] for pp in self.peers) or "none found"))

    # ---------- 2. plan ----------
    def step_plan(self) -> None:
        log.stage("2 · Plan")
        options = playbook.angles_for(self.subtype)
        news = web.search(f"{self.company} latest quarterly results highlights")
        listing = "\n".join(f"- {r['title']} | {r['snippet'][:220]}" for r in news[:8])
        menu = "\n".join(f"- {a['id']}: {a['question']}" for a in options)
        prompt = f"""Company: {self.profile.get('company_name', self.company)} ({self.subtype})
Recent headlines:
{listing}

Research angles available for this kind of company:
{menu}

Pick the 3 angles most likely to reveal something NON-OBVIOUS about this company (not what the headlines already say),
and write 3 specific web search queries for each (include the company name; target results, annual reports,
rating rationales, earnings calls, regulator data). Put the latest periods ({self.when['latest_quarter']}, {self.when['latest_year']})
in at least two queries per angle so the research is current. Return JSON:
{{"angles": [{{"id": "angle id from the list", "why": "one line", "queries": ["q1", "q2", "q3"]}}]}}"""
        plan = ask_json(self.analyst, prompt, tag="plan")
        by_id = {a["id"]: a for a in options}
        chosen = []
        for a in (plan.get("angles", []) if isinstance(plan, dict) else []):
            if isinstance(a, dict) and a.get("id") in by_id and a["id"] not in [c["id"] for c in chosen]:
                base = dict(by_id[a["id"]])
                qs = [q for q in _list(a.get("queries")) if isinstance(q, str) and q.strip()][:3]
                base["queries_used"] = qs or [q.format(c=self.company) for q in base["queries"]]
                base["why"] = a.get("why", "")
                chosen.append(base)
        for a in options:  # fallback if the model chose fewer than 3
            if len(chosen) >= 3:
                break
            if a["id"] not in [c["id"] for c in chosen]:
                chosen.append({**a, "queries_used": [q.format(c=self.company) for q in a["queries"]], "why": "default"})
        self.angles = chosen[:3]
        for a in self.angles:
            log.info(f"Angle: {a['name']} — {a.get('why', '')}")

    # ---------- 3. gather ----------
    def keywords(self) -> list[str]:
        kws = ["npa", "asset quality", "deposit", "borrowing", "profit", "growth", "quarter"]
        for a in self.angles:
            kws += a["keywords"]
        return list(dict.fromkeys(kws))

    def add_doc(self, doc: dict, about: str, origin: str) -> str:
        with self._lock:
            self._doc_n += 1
            did = f"D{self._doc_n}"
            doc = dict(doc)
            doc.update({"id": did, "about": about, "origin": origin,
                        "source_type": web.source_type(doc["url"], [self.profile.get("website_domain") or ""])})
            self.docs[did] = doc
            return did

    def fetch_with_links(self, url: str, about: str, origin: str, chars: int, follow: int = 1) -> list[str]:
        """Fetch a page; if it is an index page, also follow its most promising PDF links."""
        with self._lock:
            seen = {d["url"] for d in list(self.docs.values())}
        if url in seen:
            return []
        doc = web.fetch(url, self.keywords(), chars)
        if not doc:
            return []
        added = []
        tag = f"peer: {about}" if origin == "peer" else None
        is_index = doc["kind"] == "html" and len(doc.get("links") or []) >= 5
        if doc["kind"] == "pdf" or (len(doc["text"]) >= 1500 and (not is_index or len(doc["text"]) >= 6000)):
            did = self.add_doc(doc, about, origin)
            added.append(did)
            log.ok(f"{did} [{tag or self.docs[did]['source_type']}] {doc['title'][:80]}")
        if doc.get("links") and follow:
            for link in web.best_pdf_links(doc, self.when["recent_tokens"], n=follow):
                with self._lock:
                    if link in {d["url"] for d in list(self.docs.values())}:
                        continue
                sub = web.fetch(link, self.keywords(), chars)
                if sub:
                    did = self.add_doc(sub, about, origin)
                    added.append(did)
                    log.ok(f"{did} [{tag or self.docs[did]['source_type']}] {sub['title'][:60]} (linked from {web.domain(url)})")
        return added

    def step_gather(self) -> None:
        log.stage("3 · Gather")
        w = self.when
        queries = [q.format(c=self.company, lq=w["latest_quarter"], lf=w["latest_year"], year=w["today"][:4])
                   for q in playbook.BASELINE_QUERIES]
        for a in self.angles:
            queries += a["queries_used"]
        found: dict = {}
        for q in queries:
            log.detail(f"search: {q}")
            for r in web.search(q):
                if not self.relevant(r):
                    continue
                item = found.setdefault(r["url"], {**r, "hits": 0})
                item["hits"] += 1
        weight = {"exchange_filing": 6, "company": 6, "rating_agency": 5, "regulator": 4, "news": 2, "other": 1}
        dom = self.profile.get("website_domain") or ""
        tokens = [re.sub(r"[^a-z0-9]", "", t) for t in self.when["recent_tokens"]]

        def recent(r):
            flat = re.sub(r"[^a-z0-9]", "", f"{r.get('title', '')} {r.get('snippet', '')} {r.get('url', '')}".lower())
            return any(t and t in flat for t in tokens)

        ranked = sorted(
            found.values(),
            key=lambda r: weight[web.source_type(r["url"], [dom])] + (1 if ".pdf" in r["url"].lower() else 0)
            + r["hits"] + (3 if recent(r) else 0),
            reverse=True,
        )
        log.info(f"{len(found)} relevant results from {len(queries)} searches "
                 f"({sum(1 for r in found.values() if recent(r))} mention {self.when['latest_quarter']} or {self.when['latest_year']})")
        per_domain: dict = {}
        target = config.PROFILE["docs"]
        for r in ranked:
            if sum(1 for d in list(self.docs.values()) if d["origin"] == "main") >= target:
                break
            d = web.domain(r["url"])
            if per_domain.get(d, 0) >= 3:
                continue
            added = self.fetch_with_links(r["url"], "company", "main", config.PROFILE["doc_chars"], follow=2)
            per_domain[d] = per_domain.get(d, 0) + len(added)

        # peers: one comparable, recent document each
        for peer in self.peers[: config.PROFILE["peer_docs"]]:
            results = web.search(f"{peer['name']} {self.when['latest_quarter']} investor presentation results")
            results.sort(key=lambda r: (".pdf" not in r["url"].lower(), not recent(r)))
            got = False
            for r in results[:5]:
                if web.skippable(r["url"]):
                    continue
                if self.fetch_with_links(r["url"], peer["name"], "peer", config.PROFILE["doc_chars"] // 2, follow=1):
                    got = True
                    break
            if not got:
                log.detail(f"No usable document found for peer {peer['name']}")

        # optional Yahoo Finance data
        sym = market.symbol_for(self.profile.get("nse_symbol"))
        self.prices = market.price_history(sym)
        if self.prices:
            log.ok(f"Yahoo Finance: {len(self.prices['points'])} months of prices for {sym}")
        figs = market.annual_figures(sym, self.profile.get("company_name") or self.company)
        if figs:
            did = self.add_doc({"url": f"https://finance.yahoo.com/quote/{sym}/financials", "title": f"Yahoo Finance financials ({sym})",
                                "text": "", "kind": "data", "pages": None}, "company", "market")
            self.docs[did]["source_type"] = "market_data"
            for f in figs:
                self._store_claim(f, self.docs[did], verified=True)
            log.ok(f"Yahoo Finance: {len(figs)} annual figures")

    # ---------- 4. extract ----------
    def _store_claim(self, raw: dict, doc: dict, verified: bool | None = None, origin: str = "main") -> dict | None:
        statement = str(raw.get("statement") or "").strip()
        quote = str(raw.get("quote") or "").strip()
        page = raw.get("page") if isinstance(raw.get("page"), int) else None
        m = re.match(r"\s*\[page (\d+)\]\s*", quote)
        if m:  # the model copied the page marker into the quote
            page = page or int(m.group(1))
            quote = quote[m.end():].strip()
        if not statement or not quote:
            return None
        with self._lock:
            self._claim_n += 1
            cid = f"C{self._claim_n}"
        ent = str(raw.get("entity") or "").strip()
        about = doc.get("about")
        ek = self.entity_key(ent, about if about == "company" or any(about == p["name"] for p in self.peers) else None)
        if not ent:
            ent = self.entity_name(ek)
        basis = str(raw.get("basis") or "").strip().lower()
        basis = "" if basis in ("null", "none", "n/a", "na") else basis[:30]
        unit, value = _norm_unit(str(raw.get("unit") or ""), _num(raw.get("value")))
        c = {
            "id": cid,
            "entity": self.entity_name(ek) if ek == "company" or ek.startswith("peer:") else ent,
            "entity_key": ek,
            "metric": str(raw.get("metric") or "other").strip().lower().replace(" ", "_")[:60],
            "value": value,
            "unit": unit,
            "basis": basis,
            "period": str(raw.get("period") or "").strip()[:20] or None,
            "kind": str(raw.get("kind") or "reported_figure").strip(),
            "statement": statement[:400],
            "quote": quote[:400],
            "page": page,
            "doc_id": doc["id"],
            "url": doc["url"],
            "title": doc["title"],
            "source_type": doc["source_type"],
            "origin": origin,
        }
        c["verified"] = verify.quote_found(quote, doc.get("text", "")) if verified is None else verified
        c["group"] = self.group_of(c)
        with self._lock:
            self.claims.append(c)
        return c

    def extract_batch(self, dids: list[str], origin: str = "main", provider: str = "gemini") -> int:
        """Extract claims from several documents in ONE model call (saves free-tier quota)."""
        limit = config.GROQ_DOC_CHARS if provider == "groq" else None
        metrics = ", ".join(playbook.metrics_for(self.subtype))
        peers = ", ".join(p["name"] for p in self.peers) or "none"
        blocks = "\n\n".join(
            f"=== DOCUMENT {d} ===\nTitle: {self.docs[d]['title']}\nURL: {self.docs[d]['url']}\n"
            f"Source type: {self.docs[d]['source_type']}\n--- text ---\n{self.docs[d]['text'][:limit]}"
            for d in dids)
        per_doc = 12 if (len(dids) > 1 or provider == "groq") else 20
        prompt = f"""Company under research: {self.profile.get('company_name', self.company)} (also called: {', '.join(self.aliases)})
Peers: {peers}

From EACH of the {len(dids)} documents below, extract up to {per_doc} factual claims that matter for these questions:
{chr(10).join('- ' + a['question'] for a in self.angles)}

Rules:
- "doc": the document id the claim comes from ({', '.join(dids)}).
- "quote" must be copied EXACTLY, word for word, from that document's text (max 300 characters). No paraphrase.
- "metric": use one of [{metrics}] when it fits, otherwise "other:<short_name>".
- "value": a plain number (no commas or symbols) or null. "unit": e.g. "%", "rs crore", "bps", "x".
- "period": e.g. "Q1 FY26" or "FY25"; null if not stated.
- "entity": the company the fact is about (the company, a peer, or e.g. "RBI" / "sector").
- "basis": "consolidated" or "standalone" when the document says which (or a segment name), else null.
- "kind": reported_figure | management_statement | guidance | event | analysis.
- "page": the [page N] marker number before the quote, or null.
- "statement": one line in your own words.
Prefer numbers with periods, changes over time, and management claims that can be checked against numbers.

Return JSON: {{"claims": [{{"doc": "", "entity": "", "basis": null, "metric": "", "value": null, "unit": "", "period": "", "kind": "", "statement": "", "quote": "", "page": null}}]}}

{blocks}"""
        try:
            out = ask_json(self.analyst, prompt, tag=f"extract {'+'.join(dids)}", retry_invalid=len(dids) == 1,
                           provider=provider)
        except QuotaError:
            raise
        except LLMError as e:
            if len(dids) > 1:  # a cut-off reply: do the documents one at a time instead
                log.detail(f"Reply for {'+'.join(dids)} was cut off; reading them one at a time")
                return sum(self.extract_batch([d], origin, provider) for d in dids)
            log.warn(f"Skipping {', '.join(dids)}: {e}")
            return 0
        raw = out.get("claims", []) if isinstance(out, dict) else (out if isinstance(out, list) else [])
        counts = {d: [0, 0] for d in dids}
        for r in raw:
            if not isinstance(r, dict):
                continue
            m = re.search(r"D\s*(\d+)", str(r.get("doc") or ""), re.IGNORECASE)
            d = f"D{int(m.group(1))}" if m else (dids[0] if len(dids) == 1 else "")
            if d not in counts or sum(counts[d]) >= per_doc:
                continue
            c = self._store_claim(r, self.docs[d], origin=origin)
            if c is None:
                continue
            counts[d][0 if c["verified"] else 1] += 1
        total = 0
        for d, (ok_n, bad_n) in counts.items():
            total += ok_n
            log.info(f"{d}: {ok_n} claims verified" + (f", {bad_n} dropped (quote not found in source)" if bad_n else "")
                     + (" [Groq]" if provider == "groq" else ""))
        return total

    def extract_all(self, dids: list[str], origin: str = "main") -> None:
        """Read documents with Gemini and Groq at the same time, each taking a share sized to its limits."""
        groq_docs, gemini_docs = [], []
        use_groq = available("groq")
        for i, d in enumerate(dids):
            # Groq's free tier is ~8K tokens/minute, so it takes about 2 documents in every 5
            (groq_docs if use_groq and i % 5 in (1, 3) else gemini_docs).append(d)
        if not available("gemini"):
            groq_docs, gemini_docs = dids, []

        def gemini_worker():
            budget, batch, size = config.PROFILE["batch_chars"], [], 0
            for d in gemini_docs:
                n = len(self.docs[d]["text"])
                if batch and (size + n > budget or len(batch) >= 3):
                    self.extract_batch(batch, origin, "gemini")
                    batch, size = [], 0
                batch.append(d)
                size += n
            if batch:
                self.extract_batch(batch, origin, "gemini")

        def groq_worker():
            for d in groq_docs:
                self.extract_batch([d], origin, "groq")

        jobs = [j for j, docs in ((gemini_worker, gemini_docs), (groq_worker, groq_docs)) if docs]
        if groq_docs and gemini_docs:
            log.detail(f"Reading {len(gemini_docs)} documents with Gemini and {len(groq_docs)} with Groq in parallel")
        with ThreadPoolExecutor(max_workers=2) as pool:
            for f in [pool.submit(j) for j in jobs]:
                f.result()  # re-raises QuotaError etc.

    def step_extract(self) -> None:
        log.stage("4 · Extract claims")
        self.extract_all([d for d, x in self.docs.items() if x["origin"] in ("main", "peer")])
        v = sum(1 for c in self.claims if c["verified"])
        log.ok(f"{v} verified claims from {len(self.docs)} sources")

    # ---------- 5. timeline & calculate ----------
    def step_timeline(self) -> None:
        log.stage("5 · Timeline and calculations")
        self.series = numbers.build_series(self.claims)
        self.contradictions = numbers.reconcile(self.claims)
        multi = {k: v for k, v in self.series.items() if len(v) >= 2}
        log.info(f"{len(multi)} metric series with 2+ periods")
        for k, pts in multi.items():
            st = numbers.series_stats(pts)
            if st.get("trend_break_at"):
                log.detail(f"Trend break: {self.entity_name(k[0])} {k[1]} around {st['trend_break_at']}")
        if self.contradictions:
            log.warn(f"{len(self.contradictions)} numeric mismatches between sources")
        for x in self.contradictions[:5]:
            log.detail(f"{x['entity']} {x['metric']} {x['period']}: {x['detail']}")

    def series_text(self, metrics: set | None = None, limit: int = 40) -> str:
        lines = []
        for (ek, metric, unit, freq), pts in self.series.items():
            if len(pts) < 2 or (metrics and metric not in metrics):
                continue
            st = numbers.series_stats(pts)
            vals = ", ".join(f"{p['period']}={p['value']:g} ({'/'.join(p['claim_ids'])})" for p in pts[-12:])
            extra = f"; change {st['change_pct']}%" if "change_pct" in st else ""
            extra += f"; trend break around {st['trend_break_at']}" if st.get("trend_break_at") else ""
            lines.append(f"- {self.entity_name(ek)} | {metric} [{unit}] ({freq}): {vals}{extra}")
        return "\n".join(lines[:limit]) or "(no multi-period series found)"

    # ---------- 6. hypothesize ----------
    def step_hypothesize(self, avoid: list[dict] | None = None, start: int = 1) -> None:
        log.stage("6 · Hypotheses")
        verified = [c for c in self.claims if c["verified"]]
        lines = "\n".join(self.claim_line(c) for c in verified[:180])
        contra = "\n".join(f"- {x['entity']} {x['metric']} {x['period']}: {x['detail']}" for x in self.contradictions) or "(none)"
        avoid_txt = ""
        if avoid:
            avoid_txt = "\nThese hypotheses were already tested and DISPROVED; do not repeat them:\n" + "\n".join(
                f"- {a['hypothesis']} (red team: {a.get('judge', {}).get('reason', '')})" for a in avoid)
        n = config.PROFILE["candidates"]
        prompt = f"""Company: {self.profile.get('company_name', self.company)} ({self.subtype}). Peers: {', '.join(p['name'] for p in self.peers)}
Angles: {'; '.join(a['question'] for a in self.angles)}

VERIFIED CLAIMS (id | entity | metric | period | value | kind | source group | statement):
{lines}

TIME SERIES computed in code:
{self.series_text()}

NUMERIC MISMATCHES between sources:
{contra}
{avoid_txt}
Find up to {n} candidate discoveries. A good discovery is a DIVERGENCE: two things that should move together but don't,
a gap between what management emphasises and what the numbers show, a company-vs-peer gap, or a shift that started
earlier than the narrative suggests. Rank by how non-obvious and how well supported each one is.
Each candidate must cite at least 2 supporting claim ids from the list, ideally from different source groups.
The finding must be CURRENT: built mainly on the latest 4 to 8 quarters, up to {self.when['latest_quarter']}.
NOT acceptable as a discovery: something management already says plainly on calls or in presentations, or what
headlines report. Management statements are claims to TEST against the numbers, not discoveries in themselves.

Return JSON:
{{"candidates": [{{"hypothesis": "one clear sentence",
  "angle": "angle id",
  "mechanism": "what could be causing this",
  "supporting_claims": ["C1", "C2"],
  "non_obvious_because": "why a summary or dashboard would miss it",
  "kill_criteria": ["evidence that would prove this wrong"]}}]}}"""
        out = ask_json(self.analyst, prompt, temperature=0.4, tag="hypothesize")
        ids = {c["id"] for c in verified}
        self.candidates = []
        for i, h in enumerate(out.get("candidates", []) if isinstance(out, dict) else []):
            if not isinstance(h, dict) or not h.get("hypothesis"):
                continue
            sup = [s for s in _ids(h.get("supporting_claims")) if s in ids]
            if len(sup) < 2:
                log.detail(f"Dropped a hypothesis with fewer than 2 verified supporting claims: {h['hypothesis'][:90]}")
                continue
            h["supporting_claims"] = sup
            h["kill_criteria"] = [str(k) for k in _list(h.get("kill_criteria"))]
            h["id"] = f"H{start + len(self.candidates)}"
            self.candidates.append(h)
        self.candidates = self.candidates[:n]
        for h in self.candidates:
            log.info(f"{h['id']}: {h['hypothesis']}")
        if not self.candidates:
            log.warn("No hypothesis had enough verified support")

    # ---------- 7. challenge ----------
    def challenge(self, h: dict) -> None:
        log.info(f"Red team attacking {h['id']}")
        byid = self.claims_by_id()
        sup = "\n".join(self.claim_line(byid[s], full=True) for s in h["supporting_claims"])
        plan = ask_json(self.red, provider="groq", prompt=f"""Hypothesis about {self.profile.get('company_name', self.company)}:
"{h['hypothesis']}"

Evidence offered for it:
{sup}

List the most plausible ordinary explanations and how to test them. Always consider: a sector-wide effect
(do peers show the same pattern?), an accounting or definition change, a one-off item, a base effect, and a
regulatory change. Then write up to 4 web search queries that would find DISCONFIRMING evidence.
Return JSON: {{"alternatives": [{{"name": "", "test": ""}}], "queries": ["", ""]}}""", tag=f"redteam plan {h['id']}")
        h["red_team_plan"] = plan if isinstance(plan, dict) else {}

        new_docs = []
        seen = {d["url"] for d in self.docs.values()}
        for q in [q for q in h["red_team_plan"].get("queries", []) if isinstance(q, str)][:4]:
            if len(new_docs) >= config.PROFILE["redteam_docs"]:
                break
            log.detail(f"red-team search: {q}")
            for r in web.search(q, max_results=6):
                if r["url"] in seen or web.skippable(r["url"]):
                    continue
                seen.add(r["url"])
                added = self.fetch_with_links(r["url"], "red team", "red_team", config.PROFILE["doc_chars"] // 2, follow=1)
                if added:
                    new_docs += added
                    seen |= {self.docs[d]["url"] for d in added}
                    break
        if new_docs:
            self.extract_all(new_docs, origin="red_team")
        mine = set(new_docs)
        with self._lock:
            all_claims = list(self.claims)
        counter = [c for c in all_claims if c["verified"] and c["doc_id"] in mine]
        # claims already in the store that bear on the same metrics (peers especially) are fair game too
        metrics = {byid[s]["metric"] for s in h["supporting_claims"]}
        related = [c for c in all_claims if c["verified"] and c["origin"] != "red_team" and c["metric"] in metrics
                   and c["id"] not in h["supporting_claims"]]
        pool = counter[:12] + related[:8]  # kept small enough for Groq's per-minute token limit
        h["counter_pool"] = [c["id"] for c in pool]

        judge = ask_json(self.red, provider="groq", prompt=f"""Hypothesis: "{h['hypothesis']}"
Kill criteria set in advance: {h.get('kill_criteria', [])}

Supporting evidence:
{sup}

Other evidence found (including your own searches for disconfirming evidence):
{chr(10).join(self.claim_line(c, short=True) for c in pool) or '(none found)'}

Peer and sector series for the same metrics:
{self.series_text(metrics, limit=8)}

Alternative explanations to assess: {[a.get('name') for a in h['red_team_plan'].get('alternatives', []) if isinstance(a, dict)]}

Judge the hypothesis. "survives" = no alternative explains it away; "downgraded" = partly explained or thinly
supported; "killed" = an alternative explains it, or evidence contradicts it. Cite claim ids for every finding.
Return JSON: {{"verdict": "survives|downgraded|killed",
  "alternatives_assessed": [{{"name": "", "finding": "", "claim_ids": [], "explains_away": "fully|partly|no|untested"}}],
  "strongest_counterargument": "the best case against the hypothesis, even if it survives",
  "counter_claim_ids": [],
  "already_public": "yes|partly|no — is the core of this hypothesis already plainly stated by management or by news headlines in the evidence?",
  "already_public_note": "one line",
  "reason": "one or two sentences"}}""", tag=f"redteam judge {h['id']}")
        judge = judge if isinstance(judge, dict) else {}
        v = str(judge.get("verdict", "")).lower().strip()
        judge["verdict"] = v if v in ("survives", "downgraded", "killed") else "downgraded"
        with self._lock:
            ok_ids = {c["id"] for c in self.claims if c["verified"]}
        judge["counter_claim_ids"] = [i for i in _ids(judge.get("counter_claim_ids"))
                                      if i in ok_ids and i not in h["supporting_claims"]]
        alts = []
        for a in _list(judge.get("alternatives_assessed")):
            if isinstance(a, dict):
                a["claim_ids"] = [i for i in _ids(a.get("claim_ids")) if i in ok_ids]
                alts.append(a)
        judge["alternatives_assessed"] = alts
        m = re.match(r"\s*(yes|partly|no)\b", str(judge.get("already_public") or "").lower())
        judge["already_public"] = m.group(1) if m and "|" not in str(judge.get("already_public")) else "partly"
        h["judge"] = judge
        sym = {"survives": log.ok, "downgraded": log.warn, "killed": log.bad}[judge["verdict"]]
        sym(f"{h['id']} {judge['verdict'].upper()}: {judge.get('reason', '')}")

    # ---------- 8. grade ----------
    def grade(self, h: dict) -> None:
        byid = self.claims_by_id()
        sup = [byid[s] for s in h["supporting_claims"] if byid[s]["verified"]]
        seen_quotes, groups = set(), set()
        for c in sup:  # news copies of one press release count once
            q = verify._norm(c["quote"])[:120]
            if q in seen_quotes:
                continue
            seen_quotes.add(q)
            groups.add(c["group"])
        groups.discard("other") if len(groups) > 1 else None
        primary = bool(groups & PRIMARY_GROUPS)
        open_conflicts = [x for x in self.contradictions if set(x["claim_ids"]) & set(h["supporting_claims"])]
        verdict = h.get("judge", {}).get("verdict", "downgraded")
        reasons = [f"{len(groups)} independent source type(s): {', '.join(sorted(groups))}",
                   "includes a primary document" if primary else "no primary document",
                   f"red team verdict: {verdict}",
                   f"already public: {h.get('judge', {}).get('already_public', 'partly')}"]
        if open_conflicts:
            reasons.append(f"{len(open_conflicts)} unresolved numeric mismatch(es)")
        if verdict == "killed":
            strength = "Killed"
        elif len(groups) >= 3 and primary and not open_conflicts and verdict == "survives":
            strength = "Strong"
        elif len(groups) >= 2 and primary:
            strength = "Moderate"
        else:
            strength = "Weak"
        h["strength"] = strength
        h["strength_reasons"] = reasons
        h["groups"] = sorted(groups)
        log.info(f"{h['id']}: {strength} ({'; '.join(reasons)})")

    # ---------- 9. write ----------
    def step_write(self) -> None:
        log.stage("9 · Write up")
        h = self.chosen
        byid = self.claims_by_id()
        sup_ids = h["supporting_claims"]
        counter_ids = h.get("judge", {}).get("counter_claim_ids", [])
        evidence = "\n".join(self.claim_line(byid[i], full=True) for i in sup_ids + counter_ids)
        metrics = {byid[i]["metric"] for i in sup_ids}
        extra = [c for c in self.claims if c["verified"] and c["metric"] in metrics
                 and c["id"] not in sup_ids and c["id"] not in counter_ids and isinstance(c.get("value"), (int, float))]
        extra_txt = "\n".join(self.claim_line(c) for c in extra[:40]) or "(none)"
        out = ask_json(self.analyst, f"""Write up this finding about {self.profile.get('company_name', self.company)}.

Hypothesis: {h['hypothesis']}
Mechanism: {h.get('mechanism', '')}
Why non-obvious: {h.get('non_obvious_because', '')}
Red team: {h.get('judge', {})}
Evidence strength (computed by rule): {h['strength']} — {'; '.join(h['strength_reasons'])}

EVIDENCE (supporting, then counter):
{evidence}

OTHER VERIFIED FIGURES you may use in calculations (cite their ids):
{extra_txt}

TIME SERIES:
{self.series_text(metrics)}

Rules:
- Use only the evidence above. Cite claim ids. No investment recommendation.
- "numbers": only calculations that substantiate the finding. "formula" must use CLAIM IDS for every figure
  (never type a figure in), with + - * / and brackets ONLY, e.g. "(C12 - C7) / C7 * 100". Allowed constants: 1, 100,
  or a number that appears in the quote of a claim the formula uses (e.g. a stated "24%" growth rate).
  Percent values are stored as percents (0.96 means 0.96%): a change in bps is "(C12 - C7) * 100", in percentage points
  "(C12 - C7)". Never use 10000. Label each calculation exactly as what it computes.
- Include the calculation that proves the core claim. For a denominator effect, compute the ABSOLUTE amount from a
  ratio and its base, e.g. bad loans = C9 * C10 / 100, and compare it across periods.
  Never compare a consolidated figure with a standalone one. The code computes the result.
- If the red team downgraded or corrected the hypothesis, state the CORRECTED, narrower finding firmly; do not
  hedge it into "explained by ordinary factors". Say precisely what the evidence shows and what remains unexplained.
- "timeline": dated steps showing when the change emerged and how it developed.
- "discovery": one sentence, at most 35 words, saying what is non-obvious (not a list of facts).
- "falsification_condition": a specific, measurable data point with a threshold, for periods AFTER
  {self.when['latest_quarter']} (i.e. still in the future), with a time frame.

Return JSON:
{{"discovery": "one clear sentence",
  "why_it_matters": ["3 to 5 bullets"],
  "timeline": [{{"period": "Q1 FY26", "event": "", "claim_ids": []}}],
  "numbers": [{{"label": "", "formula": "", "unit": "", "note": ""}}],
  "chart_series": [{{"entity": "company or peer name", "metric": "metric name"}}],
  "counterargument": "the strongest alternative explanation or contradictory evidence",
  "falsification_condition": "",
  "strength_reasoning": "why the evidence is {h['strength']}"}}""", tag="write")
        out = out if isinstance(out, dict) else {}

        shown = list(sup_ids) + list(counter_ids) + [c["id"] for c in extra[:40]]  # every claim the model was shown
        # numbers are computed here, not by the model
        values = {c["id"]: c["value"] for c in self.claims if c["verified"] and isinstance(c.get("value"), (int, float))}
        nums = []
        for n in _list(out.get("numbers")):
            if not isinstance(n, dict) or not n.get("formula"):
                continue
            f_txt = str(n["formula"]).replace("×", "*").replace("÷", "/").replace("−", "-")
            used = sorted(set(re.findall(r"C\d+", f_txt)), key=lambda x: int(x[1:]))
            consts = [float(x) for x in re.findall(r"(?<![C\d.])\d+(?:\.\d+)?", f_txt)]
            no_op = not re.search(r"[-+*/]", f_txt)
            # a typed-in number is only allowed if it appears in the quote of a cited claim; that claim joins the inputs
            sources, untraced = [], False
            for x in consts:
                if x in (1.0, 100.0):
                    continue
                src = next((i for i in used + shown if i in byid and x in _quoted_numbers(byid[i]["quote"])), None)
                if src is None:
                    untraced = True
                    break
                if src not in used and src not in sources:
                    sources.append(src)
            if not used or no_op or untraced:
                log.detail(f"Dropped a calculation that is not fully traceable to cited figures ({n.get('label')}: {f_txt})")
                continue
            used = sorted(set(used) | set(sources), key=lambda x: int(x[1:]))
            bases = {byid[i].get("basis") for i in used if i in byid} - {""}
            if {"consolidated", "standalone"} <= bases:
                log.detail(f"Dropped a calculation that mixes consolidated and standalone figures ({n.get('label')})")
                continue
            try:
                val = numbers.eval_formula(f_txt, values)
                nums.append({**n, "formula": f_txt, "value": round(val, 2), "inputs": used})
            except Exception as e:
                log.detail(f"Dropped a calculation that could not be computed ({n.get('label')}: {e})")
        out["numbers"] = nums

        tl = []
        for ev in _list(out.get("timeline")):
            if isinstance(ev, dict) and ev.get("event"):
                ev["claim_ids"] = [i for i in _ids(ev.get("claim_ids")) if i in byid]
                tl.append(ev)
        tl.sort(key=lambda e: numbers.parse_period(e.get("period")) or (9999, 9))
        out["timeline"] = tl
        out["why_it_matters"] = [str(x) for x in _list(out.get("why_it_matters"))]

        charts = []
        for cs in _list(out.get("chart_series"))[:4]:
            if not isinstance(cs, dict):
                continue
            ek, metric = self.entity_key(str(cs.get("entity", ""))), str(cs.get("metric", "")).lower()
            for (k, m, unit, freq), pts in self.series.items():
                if k.split("|")[0] == ek and m == metric and len(pts) >= 3:
                    charts.append({"label": f"{self.entity_name(k)} · {m.replace('_', ' ')} ({unit or 'value'}, {freq})",
                                   "points": [{"period": p["period"], "value": p["value"]} for p in pts]})
                    break
        out["charts"] = charts
        self.final = out
        log.ok("Discovery: " + str(out.get("discovery", ""))[:200])

    # ---------- orchestration ----------
    def run(self) -> dict:
        self.step_profile()
        self.step_plan()
        self.step_gather()
        self.step_extract()
        self.step_timeline()

        tested: list[dict] = []
        for round_no in (1, 2):
            self.step_hypothesize(avoid=[h for h in tested if h.get("strength") == "Killed"] if round_no == 2 else None,
                                  start=len(tested) + 1)
            if not self.candidates:
                break
            log.stage("7 · Challenge (red team)" + (" — second round" if round_no == 2 else ""))
            with ThreadPoolExecutor(max_workers=2) as pool:
                for f in [pool.submit(self.challenge, h) for h in self.candidates]:
                    f.result()
            log.stage("8 · Triangulate and grade")
            for h in self.candidates:
                self.grade(h)
            tested += self.candidates
            if any(h["strength"] != "Killed" for h in self.candidates):
                break
            log.warn("Every hypothesis was killed. Trying once more with what the red team learned.")

        order = {"Strong": 0, "Moderate": 1, "Weak": 2, "Killed": 3}
        alive = [h for h in tested if h.get("strength") != "Killed"]
        self.candidates = tested
        if alive:
            novelty = {"no": 0, "partly": 1, "yes": 2}
            # strength first; a finding that is already public counts one level weaker
            self.chosen = sorted(alive, key=lambda h: (
                order[h["strength"]] + (1 if h["judge"].get("already_public") == "yes" else 0),
                novelty[h["judge"].get("already_public", "partly")], tested.index(h)))[0]
            log.ok(f"Reporting {self.chosen['id']} ({self.chosen['strength']})")
            self.step_write()
        else:
            log.bad("No hypothesis survived the red team. The report shows what was tested and why it failed.")
        return self.result()

    def result(self) -> dict:
        byid = self.claims_by_id()
        used = set()
        if self.chosen:
            used |= set(self.chosen["supporting_claims"]) | set(self.chosen.get("judge", {}).get("counter_claim_ids", []))
            for ev in self.final.get("timeline", []):
                used |= set(ev.get("claim_ids", []))
            for n in self.final.get("numbers", []):
                used |= set(n.get("inputs", []))
        return {
            "company": self.company,
            "profile": self.profile,
            "subtype": self.subtype,
            "peers": self.peers,
            "angles": [{k: a.get(k) for k in ("id", "name", "question", "why", "queries_used")} for a in self.angles],
            "candidates": self.candidates,
            "chosen": self.chosen,
            "final": self.final,
            "claims": self.claims,
            "used_claims": [byid[i] for i in sorted(used, key=lambda x: int(x[1:])) if i in byid],
            "docs": [{k: d.get(k) for k in ("id", "url", "title", "source_type", "about", "origin", "kind")} for d in self.docs.values()],
            "contradictions": self.contradictions,
            "prices": self.prices,
            "stats": {
                "sources": len(self.docs),
                "claims": len(self.claims),
                "verified": sum(1 for c in self.claims if c["verified"]),
                "llm_calls": CALLS["n"],
                "gemini_calls": CALLS["gemini"],
                "groq_calls": CALLS["groq"],
                "minutes": round((time.time() - self.started) / 60, 1),
            },
            "trace": log.TRACE,
        }
