# Falsify

**An autonomous financial research agent that tries to disprove its own discoveries before reporting them.**

Built for *Finding the Next Signal* (ATMOS 2026, BITS Pilani Hyderabad), **Track A: Banking, NBFCs & Financial Services**.

Give it any listed bank, NBFC, housing-finance company or insurer. It researches public sources on its own, looks for a
**divergence** (numbers that should move together but don't, or a gap between what management says and what the
numbers show), then hands that idea to a separate red-team step whose only job is to break it. Only findings that
survive are reported, with a clickable evidence trail and the condition that would prove them wrong.

## Run it

Requires Python 3.10 or newer.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your free API keys in .env
python falsify.py --check   # tests both AI providers, web search and Yahoo Finance

python falsify.py "Bajaj Finance" --quick   # faster run (recommended for live demos)
python falsify.py "HDFC Bank"               # fuller run
```

The report opens in your browser and is saved in `reports/` (an HTML research note plus a JSON file with every claim,
source and calculation). Downloads and model answers are cached in `.cache/`, so if a run stops (for example when a
free quota runs out), running the same command again continues from where it stopped. `--fresh` ignores the cache.

## What the report contains

1. **The discovery**: one sentence
2. **Why it matters**: 3 to 5 points
3. **Evidence**: every claim with its exact quote, a link to the source and the page, and a "quote verified" mark
4. **Timeline**: when the change emerged and how it developed
5. **Numbers**: calculations computed in code from cited figures, never typed by the AI
6. **Counterargument**: the strongest case against the finding, plus each alternative the red team tested
7. **Falsification condition**: a measurable future data point that would overturn the finding
8. **Evidence strength**: Strong, Moderate or Weak, computed by a fixed rule and shown with its reasons

Plus a **reasoning log**: every angle investigated, every hypothesis tested (including rejected ones), all sources read
and the full run trace.

## How it works

| Step | What happens | Where |
|---|---|---|
| 1 Profile | Identifies the company, its sub-type (bank, NBFC, housing finance, insurer) and 3 listed peers | `pipeline.step_profile` |
| 2 Plan | Picks 3 research angles from the sub-type's playbook and writes date-aware search queries | `playbook.py`, `step_plan` |
| 3 Gather | Searches the web, ranks primary sources first, downloads pages and PDFs, follows links on investor-relations pages to the actual results PDFs, fetches peer documents and Yahoo Finance data | `web.py`, `market.py` |
| 4 Extract | Pulls dated, sourced claims from every document. **Every quote is checked word for word against the downloaded text; claims whose quote isn't found are dropped** | `extract_all`, `verify.py` |
| 5 Timeline | Builds metric time series, detects trend breaks, flags sources that disagree on the same figure (keeping consolidated and standalone figures apart) | `numbers.py` |
| 6 Hypotheses | Proposes divergences, each backed by at least 2 verified claims, with kill criteria written before testing. Restating what management already said doesn't count | `step_hypothesize` |
| 7 Red team | A different AI model searches for disconfirming evidence and tests the ordinary explanations: sector-wide effect, accounting change, one-off item, base effect, regulatory change. It also judges whether the idea is already public | `challenge` |
| 8 Grade | Strength by rule: number of independent source types, a primary document, unresolved mismatches, red-team verdict | `grade` |
| 9 Write up | Produces the eight required elements; calculations are evaluated by code, and any that can't be traced to cited figures are rejected | `step_write`, `report.py` |

**No company is hard-coded.** The playbook is defined per sub-type (metrics and research angles for banks, NBFCs,
housing-finance companies and insurers), so the same system covers every company in the track and other listed
companies in the sector.

## Two free AI providers

| Steps | Provider | Why |
|---|---|---|
| Profile, plan, hypotheses, write-up | Google Gemini | A few calls that need the best judgement |
| Reading documents | Gemini **and** Groq at the same time | The biggest job, split to fit each provider's free limits |
| Red team | Groq | A *different* model checks Gemini's hypothesis: an independent challenge |

If one provider is out of quota or busy, the other takes over automatically. The design is model-agnostic; free
providers were used for this prototype so it runs at zero cost.

## Safeguards against wrong answers

- Quotes are verified against the source text; unverifiable claims never reach the report.
- Every calculation must reference cited figures; constants are allowed only if they appear in a cited quote.
- Built-in finance rules: provisions don't lower *gross* NPA, ratio changes are in basis points, consolidated and
  standalone figures are never mixed.
- The agent knows today's date and targets the latest reported quarter and financial year.
- No investment recommendations.

## Limitations

- Free-tier rate limits: a quick run takes roughly 8 to 15 minutes; a few runs a day fit in the free quotas.
- Some websites block automated downloads; the agent skips them and moves on.
- Scanned PDFs without a text layer can't be read.
- Automated research from public sources: always check the linked sources before relying on a finding.
