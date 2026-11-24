"""Post-processes the LLM's competitor section deterministically (no extra LLM tokens).

- Removes the submitted site from its own competitor list and de-duplicates.
- Stamps every competitor as an AI-generated suggestion (the model has no live market data).
- Builds the side-by-side comparison table and the aggregated strengths/gaps lists.
"""
from urllib.parse import urlsplit

from app.schemas import Brand, ComparisonRow, CompetitorAnalysis

MIN_COMPETITORS = 3
MAX_COMPETITORS = 5


def _host(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    return (urlsplit(value).hostname or "").removeprefix("www.")


def finalize_competitors(section: CompetitorAnalysis, brand: Brand, site_url: str) -> tuple[CompetitorAnalysis, list[str]]:
    warnings: list[str] = []
    own_host = _host(site_url)
    own_name = brand.name.strip().lower()

    kept, seen = [], set()
    for c in section.competitors:
        key = c.name.strip().lower()
        if not key or key in seen or key == own_name or (c.website and _host(c.website) == own_host):
            continue
        seen.add(key)
        c.source = "ai_generated_suggestion"
        kept.append(c)
    section.competitors = kept[:MAX_COMPETITORS]

    if len(section.competitors) < MIN_COMPETITORS:
        warnings.append(
            f"Only {len(section.competitors)} reliable competitor(s) could be suggested; the model was not confident enough to list {MIN_COMPETITORS}."
        )

    section.competitor_strengths = [f"{c.name}: {s}" for c in section.competitors for s in (c.strengths or ([c.key_strength] if c.key_strength else []))][:12]
    section.competitor_gaps = [f"{c.name}: {w}" for c in section.competitors for w in c.weaknesses][:12]

    p = section.brand_profile
    rows = [
        ("Brand Name", brand.name, lambda c: c.name),
        ("Main Service", p.main_service, lambda c: c.main_service),
        ("Target Market", p.target_market, lambda c: c.target_market),
        ("Key Strength", p.key_strength, lambda c: c.key_strength),
    ]
    section.comparison_table = [
        ComparisonRow(attribute=a, your_brand=mine, competitors={c.name: get(c) for c in section.competitors})
        for a, mine, get in rows
    ]
    if p.opportunity:
        section.comparison_table.append(
            ComparisonRow(attribute="Opportunity", your_brand=p.opportunity, competitors={c.name: "" for c in section.competitors})
        )
    return section, warnings
