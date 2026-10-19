"""Citation check: every quote the model gives must actually appear in the downloaded document."""
from __future__ import annotations

import re

_TRANS = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", " ": " ", "₹": "rs "})


def _norm(s: str) -> str:
    s = (s or "").translate(_TRANS).lower()
    s = re.sub(r"rs\.?\s*", "rs ", s)
    s = re.sub(r"[^a-z0-9%.\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def quote_found(quote: str, text: str, threshold: float = 0.8) -> bool:
    q, t = _norm(quote), _norm(text)
    if len(q) < 15:
        return False
    if q in t:
        return True
    words = q.split()
    if len(words) < 5:
        return False
    shingles = [" ".join(words[i : i + 4]) for i in range(len(words) - 3)]
    hits = sum(1 for s in shingles if s in t)
    return hits / len(shingles) >= threshold
