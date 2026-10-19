"""Free web access: DuckDuckGo search, page/PDF download and text extraction, disk cache."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time
from urllib.parse import urljoin, urlparse

import logging

import requests

from . import config, log

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

SKIP_DOMAINS = (
    "youtube.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "scribd.com", "slideshare.net", "quora.com", "reddit.com", "pinterest.", "tiktok.com",
)
EXCHANGE = ("bseindia.com", "nseindia.com")
RATING = (
    "crisil.com", "crisilratings.com", "icra.in", "icraratings.com", "careratings.com", "careedge.in",
    "indiaratings.co.in", "acuite.in", "brickworkratings.com", "infomerics.com",
)
REGULATOR = ("rbi.org.in", "sebi.gov.in", "mospi.gov.in", "mca.gov.in", "pib.gov.in", "irdai.gov.in", "nhb.org.in")
NEWS = (
    "economictimes.indiatimes.com", "livemint.com", "business-standard.com", "moneycontrol.com",
    "thehindubusinessline.com", "financialexpress.com", "reuters.com", "bloomberg.com", "ndtvprofit.com",
    "cnbctv18.com", "businesstoday.in", "thehindu.com", "hindustantimes.com", "timesofindia.indiatimes.com",
    "outlookbusiness.com", "fortuneindia.com", "zeebiz.com", "bqprime.com", "theprint.in", "forbesindia.com",
)

# Tests can replace these with fake functions.
SEARCH_BACKEND = None
FETCH_BACKEND = None

_state = {"last_search": 0.0}
_search_lock = threading.Lock()

logging.getLogger("pypdf").setLevel(logging.ERROR)  # hide harmless "wrong pointing object" warnings


def domain(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return d[4:] if d.startswith("www.") else d


def _matches(d: str, names) -> bool:
    return any(d == n or d.endswith("." + n) for n in names)


def source_type(url: str, company_domains=()) -> str:
    d = domain(url)
    if _matches(d, RATING):
        return "rating_agency"
    if _matches(d, REGULATOR):
        return "regulator"
    if _matches(d, EXCHANGE):
        return "exchange_filing"
    own = [domain("https://" + c) if "://" not in c else domain(c) for c in company_domains if c]
    if own and _matches(d, own):
        return "company"
    if _matches(d, NEWS):
        return "news"
    return "other"


def skippable(url: str) -> bool:
    d = domain(url)
    return (not url.startswith(("http://", "https://"))) or _matches(d, SKIP_DOMAINS)


def _cache(kind: str, key: str) -> str:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return os.path.join(config.CACHE_DIR, kind, h + ".json")


def _load(path: str):
    if config.USE_CACHE and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _save(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def search(query: str, max_results: int = 8) -> list[dict]:
    """Web search. Returns [{title, url, snippet}]."""
    path = _cache("search", query)
    cached = _load(path)
    if cached is not None:
        return cached

    if SEARCH_BACKEND is not None:
        out = SEARCH_BACKEND(query, max_results)
        _save(path, out)
        return out

    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # older package name
        except ImportError:
            raise RuntimeError("The ddgs package is not installed. Run: pip install -r requirements.txt")

    for attempt in range(4):
        with _search_lock:  # several threads may search at once; keep the overall pace polite
            wait = config.SEARCH_MIN_INTERVAL - (time.time() - _state["last_search"])
            _state["last_search"] = time.time() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)
        try:
            results = list(DDGS().text(query, region="in-en", max_results=max_results) or [])
            out = []
            for r in results:
                url = r.get("href") or r.get("url") or ""
                if url and not skippable(url):
                    out.append({"title": r.get("title", ""), "url": url, "snippet": r.get("body", "")})
            _save(path, out)
            return out
        except Exception as e:
            if "no results" in str(e).lower():
                _save(path, [])  # a genuine empty result: retrying won't change it
                return []
            delay = 6 * (attempt + 1)
            log.warn(f"Search hiccup ({str(e)[:80]}). Waiting {delay}s and retrying…")
            time.sleep(delay)
    log.warn(f"Search failed for: {query}")
    return []


def fetch(url: str, keywords=(), max_chars: int = 24000) -> dict | None:
    """Download a page or PDF and return {url, title, text, kind, pages}. None if unusable."""
    kw_key = ",".join(sorted(k.lower() for k in keywords))
    path = _cache("page", f"v2|{url}|{kw_key}|{max_chars}")
    cached = _load(path)
    if cached is not None:
        return cached or None

    if FETCH_BACKEND is not None:
        doc = FETCH_BACKEND(url, keywords, max_chars)
        _save(path, doc or {})
        return doc

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9", "Accept": "*/*"},
            timeout=30,
        )
    except Exception as e:
        log.detail(f"Could not open {domain(url)} ({type(e).__name__})")
        return None  # network trouble may be temporary: don't remember it as a dead page
    if resp.status_code != 200 or not resp.content:
        log.detail(f"{domain(url)} returned HTTP {resp.status_code}")
        if resp.status_code in (403, 404, 410, 451):
            _save(path, {})  # permanent refusals are remembered so we don't ask again
        return None

    ctype = resp.headers.get("Content-Type", "").lower()
    is_pdf = "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf") or resp.content[:4] == b"%PDF"
    try:
        if is_pdf:
            title, text, pages = _pdf_text(resp.content, keywords, max_chars)
            kind, links = "pdf", []
        else:
            title, text, links = _html_text(resp.content, resp.url or url)
            text = _focus(text, keywords, max_chars)
            pages, kind = None, "html"
    except Exception as e:
        log.detail(f"Could not read {domain(url)} ({type(e).__name__})")
        _save(path, {})
        return None

    if len(text.strip()) < 300 and not links:
        _save(path, {})
        return None
    doc = {"url": url, "title": title or domain(url), "text": text, "kind": kind, "pages": pages, "links": links}
    _save(path, doc)
    return doc


def _html_text(content: bytes, base_url: str = ""):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "html.parser")
    links, seen = [], set()
    for a in soup.find_all("a", href=True):  # document links on investor-relations pages
        href = urljoin(base_url, a["href"].strip())
        label = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:160]
        if href in seen or not href.startswith("http"):
            continue
        if ".pdf" in href.lower() or "pdf" in href.lower().split("/")[-1]:
            seen.add(href)
            links.append({"url": href, "text": label})
        if len(links) >= 120:
            break
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg"]):
        tag.decompose()
    title = (soup.title.get_text(strip=True) if soup.title else "")[:200]
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return title, text, links


def _focus(text: str, keywords, max_chars: int) -> str:
    """Keep the paragraphs that mention the keywords when a page is too long."""
    if len(text) <= max_chars or not keywords:
        return text[:max_chars]
    kws = [k.lower() for k in keywords]
    paras = text.split("\n")
    scored = []
    for i, p in enumerate(paras):
        low = p.lower()
        s = sum(low.count(k) for k in kws)
        scored.append((s, i))
    keep = set(range(min(15, len(paras))))  # the top of the page usually has the headline and date
    total = sum(len(paras[i]) for i in keep)
    for s, i in sorted(scored, reverse=True):
        if s == 0 or total > max_chars:
            break
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(paras) and j not in keep:
                keep.add(j)
                total += len(paras[j])
    return "\n".join(paras[i] for i in sorted(keep))[:max_chars]


def _pdf_text(content: bytes, keywords, max_chars: int):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    title = ""
    try:
        title = (reader.metadata.title or "") if reader.metadata else ""
    except Exception:
        pass
    n = len(reader.pages)
    limit = min(n, 450)
    texts = []
    for i in range(limit):
        try:
            t = reader.pages[i].extract_text() or ""
        except Exception:
            t = ""
        texts.append(re.sub(r"[ \t]+", " ", t))

    kws = [k.lower() for k in keywords]
    if sum(len(t) for t in texts) <= max_chars or not kws:
        chosen = list(range(limit))
    else:
        scores = [(sum(t.lower().count(k) for k in kws), i) for i, t in enumerate(texts)]
        chosen, total = [0], len(texts[0]) if texts else 0  # cover page carries name and period
        for s, i in sorted(scores, reverse=True):
            if s == 0 or total > max_chars:
                break
            if i not in chosen:
                chosen.append(i)
                total += len(texts[i])
        chosen.sort()

    parts = [f"[page {i + 1}]\n{texts[i]}" for i in chosen if texts[i].strip()]
    return title, "\n".join(parts)[:max_chars], [i + 1 for i in chosen]


DOC_WORDS = ("investor presentation", "presentation", "results", "financial results", "transcript", "earnings call",
             "conference call", "annual report", "press release", "analyst", "factsheet", "fact sheet")


def best_pdf_links(doc: dict, recent_tokens=(), n: int = 2) -> list[str]:
    """Pick the most promising PDF links on an index page: recent results, presentations, transcripts."""
    scored = []
    for i, l in enumerate(doc.get("links") or []):
        blob = (l["text"] + " " + l["url"]).lower()
        flat = re.sub(r"[^a-z0-9]", "", blob)
        s = sum(2 for w in DOC_WORDS if w in blob)
        s += sum(3 for t in recent_tokens if re.sub(r"[^a-z0-9]", "", t) and re.sub(r"[^a-z0-9]", "", t) in flat)
        if s > 0:
            scored.append((s, -i, l["url"]))
    return [u for _, _, u in sorted(scored, reverse=True)[:n]]
