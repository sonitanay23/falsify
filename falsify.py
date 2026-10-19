"""Falsify — autonomous financial research agent (Track A).

Usage:
  python falsify.py "Bajaj Finance"            full run, opens the report in your browser
  python falsify.py "Bajaj Finance" --quick    faster, smaller run (rehearsals, tight demos)
  python falsify.py "Bajaj Finance" --fresh    ignore saved results and research again from scratch
  python falsify.py --check                    test your Gemini key, web search and Yahoo Finance
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import webbrowser

from agent import __version__, config, log, market, web
from agent.llm import LLMError, ask_json


def check() -> int:
    good = True
    print(f"Falsify {__version__}: checking setup…\n")
    try:
        from agent import llm as _l
        out = ask_json("Reply with valid JSON only.", 'Return {"status": "ok"}', tag="check")
        if _l.CALLS["gemini"]:
            print("  ✓ Gemini key works" if out.get("status") == "ok" else f"  ? Gemini replied: {out}")
        else:
            print("  ! Gemini is out of quota or busy right now (Groq answered instead)")
    except LLMError as e:
        print(f"  ✗ Gemini: {e}")
        good = False
    from agent import llm as _llm
    if config.GROQ_API_KEY:
        try:
            before = _llm.CALLS["groq"]
            out = ask_json("Reply with valid JSON only.", 'Return {"status": "ok", "via": "groq"}', tag="check",
                           provider="groq")
            used = "groq" if _llm.CALLS["groq"] else "gemini (Groq failed, see the warning above)"
            print("  ✓ Groq key works" if _llm.CALLS["groq"] > before else f"  ✗ Groq: request went to {used}")
        except LLMError as e:
            print(f"  ✗ Groq: {e}")
    else:
        print("  · No GROQ_API_KEY in .env (optional second provider; strongly recommended)")
    try:
        res = web.search("HDFC Bank quarterly results", max_results=3)
        print(f"  ✓ Web search works ({len(res)} results)" if res else "  ✗ Web search returned nothing (try again in a minute)")
        good = good and bool(res)
    except Exception as e:
        print(f"  ✗ Web search: {e}")
        good = False
    p = market.price_history("HDFCBANK.NS")
    print("  ✓ Yahoo Finance works" if p else "  · Yahoo Finance unavailable (optional, the agent works without it)")
    print("\nAll set." if good else "\nFix the ✗ items above, then run --check again.")
    return 0 if good else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Falsify: research a Track A company and try to disprove the finding.")
    ap.add_argument("company", nargs="?", help='Company name, e.g. "Bajaj Finance"')
    ap.add_argument("--quick", action="store_true", help="smaller, faster run")
    ap.add_argument("--fresh", action="store_true", help="ignore the cache and research from scratch")
    ap.add_argument("--check", action="store_true", help="test the setup")
    ap.add_argument("--no-open", action="store_true", help="don't open the report in a browser")
    ap.add_argument("--version", action="version", version=f"Falsify {__version__}")
    args = ap.parse_args()

    config.USE_CACHE = not args.fresh
    if args.check:
        config.USE_CACHE = False
        return check()
    if not args.company:
        ap.print_help()
        return 1

    from agent.pipeline import Run
    from agent.report import render

    config.PROFILE = config.PROFILES["quick" if args.quick else "full"]
    print(f"\n\033[1mFalsify {__version__}\033[0m — researching {args.company} ({'quick' if args.quick else 'full'} run)")
    run = Run(args.company)
    try:
        result = run.run()
    except LLMError as e:
        log.bad(str(e))
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Everything fetched so far is cached, so the next run picks up quickly.")
        sys.stdout.flush()
        os._exit(130)  # don't wait for background requests to finish

    os.makedirs(config.REPORT_DIR, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", args.company.lower()).strip("-")
    stem = os.path.join(config.REPORT_DIR, f"{slug}-{time.strftime('%Y%m%d-%H%M')}")
    with open(stem + ".html", "w", encoding="utf-8") as f:
        f.write(render(result))
    with open(stem + ".json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1, default=str)

    path = os.path.abspath(stem + ".html")
    st = result["stats"]
    print(f"\n\033[1mDone\033[0m in {st['minutes']} min · {st['sources']} sources · {st['verified']} verified claims · "
          f"{st['llm_calls']} model calls (Gemini {st['gemini_calls']}, Groq {st['groq_calls']})")
    print(f"Report: {path}")
    if not args.no_open:
        webbrowser.open("file://" + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
