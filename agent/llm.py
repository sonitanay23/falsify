"""Model layer: Gemini + Groq, JSON-only answers, free-tier pacing, retries, fallback, caching, thread-safe."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import deque

from . import config, log


class LLMError(Exception):
    pass


class QuotaError(LLMError):
    """Every free model on every provider is out or busy: stop the run instead of skipping steps."""


class _ProviderDown(Exception):
    """This provider can't serve the request now (quota, busy, key, too large); try the other one."""


CALLS = {"n": 0, "gemini": 0, "groq": 0}
_lock = threading.Lock()

# Tests can replace this with a function(system, prompt, provider) -> str.
BACKEND = None

_state = {
    "gemini": {"client": None, "model": None, "last": 0.0, "dead": set(), "down": False, "lock": threading.Lock()},
    "groq": {"model": None, "last": 0.0, "dead": set(), "down": False, "lock": threading.Lock(), "window": deque()},
}


def available(provider: str) -> bool:
    if provider == "groq" and not config.GROQ_API_KEY and BACKEND is None:
        return False
    if provider == "gemini" and not config.GEMINI_API_KEY and BACKEND is None:
        return False
    return not _state[provider]["down"]


def ask_json(system: str, prompt: str, *, temperature: float = 0.2, tag: str = "llm",
             retry_invalid: bool = True, provider: str = "gemini"):
    """Ask a model and return parsed JSON. `provider` is a preference; the other one takes over if it can't."""
    key = hashlib.sha256(f"{system}\n\n{prompt}\n{temperature}".encode("utf-8")).hexdigest()[:32]
    path = os.path.join(config.CACHE_DIR, "llm", key + ".json")
    if config.USE_CACHE and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    text = _generate(system, prompt, temperature, provider)
    data = parse_json(text)
    if data is None:
        data = salvage_claims(text)
    if data is None and not retry_invalid:
        raise LLMError(f"The reply for step '{tag}' was not valid JSON.")
    if data is None:
        log.detail(f"{tag}: reply was not valid JSON, asking again")
        text = _generate(system, prompt + "\n\nYour previous reply was not valid JSON. Reply with ONLY valid JSON, "
                         "no prose, no code fences.", temperature, provider)
        data = parse_json(text) or salvage_claims(text)
        if data is None:
            raise LLMError(f"The model did not return valid JSON for step '{tag}'.")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def salvage_claims(text: str):
    """Recover the complete objects from a cut-off {"claims": [...]} reply."""
    if not text or '"claims"' not in text:
        return None
    start = text.find("[", text.find('"claims"'))
    if start == -1:
        return None
    items, depth, obj_start, in_str, esc = [], 0, None, False, False
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    items.append(json.loads(text[obj_start : i + 1]))
                except Exception:
                    pass
                obj_start = None
        elif ch == "]" and depth == 0:
            break
    return {"claims": items} if items else None


def parse_json(text: str):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    for o, c in (("{", "}"), ("[", "]")):
        i, j = t.find(o), t.rfind(c)
        if i != -1 and j > i:
            try:
                return json.loads(t[i : j + 1])
            except Exception:
                continue
    return None


def _retry_delay(msg: str, attempt: int, header: str | None = None) -> float:
    if header:
        try:
            return min(float(header) + 1, 60)
        except ValueError:
            pass
    m = re.search(r"(?:retry|try again)[^0-9]{0,30}(\d+(?:\.\d+)?)\s*s", msg, re.IGNORECASE)
    if m:
        return min(float(m.group(1)) + 2, 60)
    return min(10 * (2 ** attempt), 60)


def _generate(system: str, prompt: str, temperature: float, provider: str) -> str:
    order = [provider, "groq" if provider == "gemini" else "gemini"]
    if BACKEND is not None:
        p = next((x for x in order if available(x)), None)
        if p is None:
            raise QuotaError("Every free model is out of its daily limit or too busy right now.")
        with _lock:
            CALLS["n"] += 1
            CALLS[p] += 1
        return BACKEND(system, prompt, p)

    tried = []
    for p in order:
        if not available(p):
            continue
        tried.append(p)
        try:
            text = (_gemini if p == "gemini" else _groq)(system, prompt, temperature)
            with _lock:
                CALLS["n"] += 1
                CALLS[p] += 1
            return text
        except _ProviderDown as e:
            log.warn(f"{p.capitalize()} can't take this request ({e}); "
                     + ("trying the other provider" if p == order[0] else "no other provider left"))
            continue
    raise QuotaError(
        "Every free model is out of its daily limit or too busy right now"
        + ("" if config.GROQ_API_KEY else " (tip: add GROQ_API_KEY to .env for a second free provider)")
        + ". Wait a while (Gemini limits reset around 12:30 PM IST) and run the same command again: "
        "finished steps are saved and won't repeat."
    )


# ---------------- Gemini ----------------
def _gemini_client():
    st = _state["gemini"]
    if st["client"] is None:
        if not config.GEMINI_API_KEY:
            raise _ProviderDown("no GEMINI_API_KEY in .env")
        try:
            from google import genai
        except ImportError:
            raise _ProviderDown("google-genai not installed")
        st["client"] = genai.Client(api_key=config.GEMINI_API_KEY)
    return st["client"]


def _gemini(system: str, prompt: str, temperature: float) -> str:
    from google.genai import types

    st = _state["gemini"]
    client = _gemini_client()
    while True:
        with st["lock"]:
            order = ([st["model"]] if st["model"] else []) + config.MODEL_CANDIDATES
            models = [m for m in dict.fromkeys(order) if m and m not in st["dead"]]
        if not models:
            st["down"] = True
            raise _ProviderDown("all Gemini models are out or busy")
        model = models[0]
        for attempt in range(4):
            with st["lock"]:
                wait = config.LLM_MIN_INTERVAL - (time.time() - st["last"])
                st["last"] = time.time() + max(wait, 0)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=temperature,
                        response_mime_type="application/json",
                        max_output_tokens=32768,  # room for the model's own thinking plus a long JSON reply
                    ),
                )
                with st["lock"]:
                    if st["model"] != model:
                        st["model"] = model
                        log.detail(f"Gemini: using {model}")
                return resp.text or ""
            except Exception as e:
                msg, code = str(e), getattr(e, "code", None)
                low = msg.lower()
                if code == 404 or "NOT_FOUND" in msg or ("not found" in low and "model" in low):
                    log.detail(f"Gemini model {model} isn't available on this key, trying the next one")
                    st["dead"].add(model)
                    break
                if code in (429, 500, 503) or "RESOURCE_EXHAUSTED" in msg or "UNAVAILABLE" in msg:
                    if "per day" in low or "perday" in low or "daily" in low:
                        log.warn(f"{model} has used up its free daily limit, switching to the next free model")
                        st["dead"].add(model)
                        if st["model"] == model:
                            st["model"] = None
                        break
                    delay = _retry_delay(msg, attempt)
                    log.warn(f"Gemini is busy or rate-limiting (try {attempt + 1}/4). Waiting {delay:.0f}s…")
                    time.sleep(delay)
                    continue
                if code in (400, 401, 403) and ("key" in low or "permission" in low):
                    st["down"] = True
                    raise _ProviderDown("Gemini rejected the API key")
                raise LLMError(f"Gemini error: {msg[:300]}") from e
        else:
            log.warn(f"{model} is still busy, switching to the next free model")
            st["dead"].add(model)
            if st["model"] == model:
                st["model"] = None


# ---------------- Groq (OpenAI-compatible HTTP API) ----------------
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _groq_tokens(system: str, prompt: str) -> int:
    # ~3.5 characters per token for numbers-heavy financial text, plus the reply budget
    return int((len(system) + len(prompt)) / 3.5) + config.GROQ_MAX_OUTPUT


def _groq_wait_for_budget(need: int) -> None:
    """Stay under Groq's tokens-per-minute limit using a 60-second sliding window."""
    st = _state["groq"]
    while True:
        with st["lock"]:
            now = time.time()
            win = st["window"]
            while win and now - win[0][0] > 60:
                win.popleft()
            used = sum(t for _, t in win)
            gap = config.GROQ_MIN_INTERVAL - (now - st["last"])
            if used + need <= config.GROQ_TPM and gap <= 0:
                win.append((now, need))
                st["last"] = now
                return
            sleep_for = max(gap, (61 - (now - win[0][0])) if win and used + need > config.GROQ_TPM else 0, 0.5)
        time.sleep(min(sleep_for, 30))


def _groq(system: str, prompt: str, temperature: float) -> str:
    import requests

    st = _state["groq"]
    need = _groq_tokens(system, prompt)
    if need > config.GROQ_TPM:
        raise _ProviderDown("request too large for Groq's per-minute limit")
    while True:
        with st["lock"]:
            order = ([st["model"]] if st["model"] else []) + config.GROQ_MODELS
            models = [m for m in dict.fromkeys(order) if m and m not in st["dead"]]
        if not models:
            st["down"] = True
            raise _ProviderDown("all Groq models are out or busy")
        model = models[0]
        for attempt in range(4):
            _groq_wait_for_budget(need)
            try:
                r = requests.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {config.GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                        "temperature": temperature,
                        "max_tokens": config.GROQ_MAX_OUTPUT,
                        **({} if st.get("no_json_mode") else {"response_format": {"type": "json_object"}}),
                        # reasoning models spend reply tokens on thinking: keep it short so the answer fits
                        **({"reasoning_effort": "low"} if model.startswith("openai/gpt-oss")
                           and not st.get("no_reasoning_param") else {}),
                    },
                    timeout=120,
                )
            except Exception as e:
                delay = _retry_delay(str(e), attempt)
                log.warn(f"Groq connection problem (try {attempt + 1}/4). Waiting {delay:.0f}s…")
                time.sleep(delay)
                continue
            if r.status_code == 200:
                with st["lock"]:
                    if st["model"] != model:
                        st["model"] = model
                        log.detail(f"Groq: using {model}")
                try:
                    return r.json()["choices"][0]["message"]["content"] or ""
                except Exception:
                    return ""
            body = r.text[:400]
            low = body.lower()
            if r.status_code in (401, 403):
                st["down"] = True
                raise _ProviderDown("Groq rejected the API key, check GROQ_API_KEY in .env")
            if r.status_code == 404 or "model_not_found" in low or "does not exist" in low or "decommissioned" in low:
                log.detail(f"Groq model {model} isn't available, trying the next one")
                st["dead"].add(model)
                break
            if r.status_code == 413 or "request too large" in low:
                raise _ProviderDown("request too large for Groq's free limits")
            if r.status_code == 400 and "reasoning" in low and not st.get("no_reasoning_param"):
                st["no_reasoning_param"] = True
                continue
            if r.status_code == 400 and "response_format" in low and not st.get("no_json_mode"):
                st["no_json_mode"] = True  # this model doesn't support JSON mode; ask in plain text instead
                continue
            if r.status_code == 400 and "json" in low:
                # json_validate_failed: Groq returns what the model wrote; salvage what we can from it
                try:
                    return str(r.json().get("error", {}).get("failed_generation") or "")
                except Exception:
                    return ""
            if r.status_code == 429:
                if "per day" in low or "(tpd)" in low or "(rpd)" in low:
                    log.warn(f"Groq {model} has used up its free daily limit, switching to the next model")
                    st["dead"].add(model)
                    if st["model"] == model:
                        st["model"] = None
                    break
                delay = _retry_delay(body, attempt, r.headers.get("retry-after"))
                log.warn(f"Groq is rate-limiting (try {attempt + 1}/4). Waiting {delay:.0f}s…")
                time.sleep(delay)
                continue
            if r.status_code >= 500:
                delay = _retry_delay(body, attempt)
                log.warn(f"Groq is busy (try {attempt + 1}/4). Waiting {delay:.0f}s…")
                time.sleep(delay)
                continue
            raise LLMError(f"Groq error {r.status_code}: {body[:200]}")
        else:
            log.warn(f"Groq {model} is still busy, switching to the next model")
            st["dead"].add(model)
            if st["model"] == model:
                st["model"] = None
