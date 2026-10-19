"""Track A playbook: metric schema and research angles per company sub-type.

Nothing here names a company. The agent detects the sub-type from public sources
and loads the matching schema, which is how one system covers every company.
"""
from __future__ import annotations

SUBTYPES = ("bank", "nbfc", "housing_finance", "insurer", "other_financial")

METRICS = {
    "bank": [
        "advances", "deposits", "casa_ratio", "cd_ratio", "borrowings", "cost_of_funds", "nim",
        "yield_on_advances", "gross_npa_ratio", "net_npa_ratio", "gross_npa_amount", "gross_slippages",
        "slippage_ratio", "write_offs", "recoveries_upgrades", "provision_coverage_ratio", "credit_cost",
        "restructured_book", "sma_book", "capital_adequacy_ratio", "cet1_ratio", "retail_advances",
        "unsecured_retail_share", "msme_advances", "corporate_advances", "agri_advances", "other_income",
        "treasury_income", "operating_profit", "provisions", "pat", "roa", "roe", "branches",
    ],
    "nbfc": [
        "aum", "disbursements", "loan_book", "gross_stage3_ratio", "net_stage3_ratio", "stage2_ratio",
        "gross_npa_ratio", "net_npa_ratio", "credit_cost", "write_offs", "provision_coverage_ratio", "nim",
        "yield", "cost_of_borrowing", "borrowings", "bank_borrowings_share", "ncd_share",
        "commercial_paper_share", "deposits", "securitisation_da_volume", "co_lending_aum",
        "capital_adequacy_ratio", "unsecured_share", "segment_aum", "customer_count", "pat", "roa", "roe",
    ],
    "housing_finance": [
        "aum", "disbursements", "home_loan_share", "lap_share", "construction_finance_share",
        "gross_stage3_ratio", "net_stage3_ratio", "gross_npa_ratio", "credit_cost", "write_offs", "nim",
        "spread", "cost_of_borrowing", "borrowings", "bank_borrowings_share", "nhb_refinance",
        "ncd_share", "deposits", "securitisation_da_volume", "capital_adequacy_ratio", "pat", "roa", "roe",
    ],
    "insurer": [
        "gross_written_premium", "new_business_premium", "ape", "vnb", "vnb_margin", "persistency_13m",
        "persistency_61m", "solvency_ratio", "ulip_share", "par_share", "non_par_share", "protection_share",
        "annuity_share", "bancassurance_share", "agency_share", "embedded_value", "claims_ratio",
        "combined_ratio", "expense_ratio", "commission_ratio", "aum", "pat", "roe",
    ],
    "other_financial": [
        "revenue", "segment_revenue", "aum", "loan_book", "customer_count", "gross_npa_ratio",
        "credit_cost", "borrowings", "capital_adequacy_ratio", "pat", "roe",
    ],
}

_GQ = {
    "id": "growth_vs_quality",
    "name": "Growth vs asset quality",
    "question": "Are loan-book growth and the asset-quality story moving in different directions?",
    "keywords": ["slippage", "write-off", "written off", "gross npa", "gnpa", "stage 3", "stage 2", "sma",
                 "restructur", "movement of npa", "credit cost", "provision", "asset quality"],
    "queries": ["{c} gross slippages write-offs asset quality", "{c} GNPA slippage ratio credit cost trend",
                "{c} annual report movement of NPAs written off"],
}
_SAID = {
    "id": "said_vs_did",
    "name": "Said vs did",
    "question": "Did management's guidance and emphasis match what was later reported?",
    "keywords": ["guidance", "we expect", "target", "outlook", "going forward", "aim to", "earnings call"],
    "queries": ["{c} management guidance outlook earnings call", "{c} missed guidance target",
                "{c} earnings call transcript"],
}
_REG = {
    "id": "regulatory_impact",
    "name": "Regulatory impact",
    "question": "Did a regulatory or compliance development hit the financials before mainstream coverage?",
    "keywords": ["risk weight", "rbi", "regulat", "directive", "embargo", "penalty", "supervisory",
                 "circular", "irdai"],
    "queries": ["{c} RBI action penalty restriction", "{c} risk weight impact capital",
                "{c} regulatory change impact"],
}

ANGLES = {
    "bank": [
        _GQ,
        {
            "id": "funding_mix",
            "name": "Funding mix shift",
            "question": "How has the funding mix or customer profile changed over five years, and when and why?",
            "keywords": ["casa", "deposit", "term deposit", "borrowing", "cost of funds", "credit-deposit",
                         "cd ratio", "certificate of deposit", "infrastructure bond"],
            "queries": ["{c} CASA ratio deposits trend", "{c} credit deposit ratio borrowings",
                        "{c} cost of funds deposit growth"],
        },
        {
            "id": "hidden_dependency",
            "name": "Hidden dependency",
            "question": "Is there a non-obvious dependency on partners, funders or a concentrated customer group?",
            "keywords": ["co-lending", "concentration", "top 20", "securitis", "priority sector", "pslc",
                         "partnership", "fintech", "bulk deposit"],
            "queries": ["{c} co-lending partnership NBFC", "{c} deposit concentration top 20 depositors",
                        "{c} PSLC priority sector shortfall"],
        },
        {
            "id": "mix_shift",
            "name": "Loan mix shift",
            "question": "Is the loan mix shifting (unsecured retail, MSME, corporate) faster than the narrative says?",
            "keywords": ["unsecured", "personal loan", "credit card", "msme", "segment", "retail", "corporate",
                         "business banking", "rural"],
            "queries": ["{c} unsecured loans personal loans credit cards growth", "{c} loan mix retail corporate MSME",
                        "{c} segment-wise advances"],
        },
        _SAID,
        _REG,
    ],
    "nbfc": [
        {**_GQ, "keywords": _GQ["keywords"] + ["aum", "disbursement"],
         "queries": ["{c} stage 3 credit cost AUM growth", "{c} write-offs collection efficiency",
                     "{c} asset quality stress segment"]},
        {
            "id": "funding_dependency",
            "name": "Funding dependency",
            "question": "Does it depend on banks, co-lending partners or specific funders more than its disclosures show?",
            "keywords": ["borrowing mix", "bank loans", "term loans from banks", "ncd", "commercial paper",
                         "co-lending", "direct assignment", "securitis", "top lenders", "liquidity", "ecb"],
            "queries": ["{c} borrowing mix banks NCD commercial paper", "{c} credit rating rationale liquidity funding",
                        "{c} co-lending direct assignment securitisation"],
        },
        {
            "id": "customer_mix_shift",
            "name": "Customer and product mix shift",
            "question": "How has its customer profile or product mix changed, and when?",
            "keywords": ["unsecured", "new to credit", "customer", "product mix", "segment", "personal loan",
                         "two-wheeler", "gold loan", "msme", "consumer durable"],
            "queries": ["{c} product mix AUM segment growth", "{c} new customers unsecured lending",
                        "{c} segment-wise AUM"],
        },
        _REG,
        _SAID,
    ],
    "housing_finance": [
        _GQ,
        {
            "id": "funding_dependency",
            "name": "Funding dependency",
            "question": "How dependent is it on banks, NHB refinance or specific funders, and is that changing?",
            "keywords": ["borrowing mix", "bank loans", "nhb", "refinance", "ncd", "commercial paper",
                         "deposits", "securitis", "direct assignment"],
            "queries": ["{c} borrowing mix NHB refinance banks", "{c} credit rating rationale funding profile",
                        "{c} cost of borrowing trend"],
        },
        {
            "id": "mix_shift",
            "name": "Loan mix shift",
            "question": "Is growth coming from home loans or from riskier LAP and construction finance?",
            "keywords": ["home loan", "loan against property", "lap", "construction finance", "developer",
                         "affordable", "segment", "self-employed"],
            "queries": ["{c} home loans LAP construction finance mix", "{c} affordable housing segment growth",
                        "{c} segment-wise AUM"],
        },
        _SAID,
    ],
    "insurer": [
        {
            "id": "growth_vs_profitability",
            "name": "Growth vs profitability",
            "question": "Is premium growth coming at the cost of new-business margins or persistency?",
            "keywords": ["vnb", "margin", "ape", "persistency", "new business", "embedded value", "surrender"],
            "queries": ["{c} VNB margin APE growth", "{c} persistency ratio trend", "{c} embedded value analyst presentation"],
        },
        {
            "id": "channel_dependency",
            "name": "Channel dependency",
            "question": "How dependent is it on one distribution channel or parent bank?",
            "keywords": ["bancassurance", "banca", "agency", "parent", "channel mix", "partner", "direct"],
            "queries": ["{c} bancassurance share channel mix", "{c} parent bank distribution dependence",
                        "{c} agency channel growth"],
        },
        {
            "id": "product_mix_shift",
            "name": "Product mix shift",
            "question": "Has the product mix quietly shifted (ULIP, non-par, protection, annuity), and why?",
            "keywords": ["ulip", "non-par", "par", "protection", "annuity", "product mix", "savings"],
            "queries": ["{c} product mix ULIP non-par protection", "{c} protection share trend", "{c} product mix change"],
        },
        _REG,
        _SAID,
    ],
    "other_financial": [_GQ, _SAID, _REG],
}


def angles_for(subtype: str) -> list[dict]:
    return ANGLES.get(subtype, ANGLES["other_financial"])


def metrics_for(subtype: str) -> list[str]:
    return METRICS.get(subtype, METRICS["other_financial"])


BASELINE_QUERIES = [
    "{c} {lq} results investor presentation",
    "{c} {lq} earnings call transcript",
    "{c} annual report {lf}",
    "{c} credit rating rationale {year}",
]
