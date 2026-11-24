"""Request/response models. The same models validate the LLM output, so a malformed LLM
answer is caught (and retried once) instead of leaking to the client."""
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

PLATFORMS = ("Google", "Facebook", "Instagram", "LinkedIn", "X")
_PLATFORM_ALIASES = {
    "google": "Google", "google search": "Google",
    "facebook": "Facebook", "fb": "Facebook",
    "instagram": "Instagram", "ig": "Instagram",
    "linkedin": "LinkedIn",
    "x": "X", "twitter": "X", "x (twitter)": "X", "x/twitter": "X",
}


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value).strip()


def _to_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [v if not isinstance(v, str) else v.strip() for v in value if v not in (None, "")]
    return [value]


Str = Annotated[str, BeforeValidator(_to_str)]
StrList = Annotated[list[str], BeforeValidator(_to_list)]


class Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


# request
class LinkAnalysisRequest(BaseModel):
    url: str = Field(..., description="Website URL to analyze", examples=["https://example.com"])
    refresh: bool = Field(False, description="Skip the cache and re-analyze")


# section 1
class BrandProfile(Loose):
    main_service: Str = ""
    target_market: Str = ""
    key_strength: Str = ""
    opportunity: Str = ""


class Competitor(Loose):
    name: Str
    website: Str = ""
    main_service: Str = ""
    target_market: Str = ""
    key_strength: Str = ""
    strengths: StrList = []
    weaknesses: StrList = []
    confidence: Literal["low", "medium", "high"] = "low"
    # Set by the server, never trusted from the LLM:
    source: str = "ai_generated_suggestion"


class ComparisonRow(BaseModel):
    attribute: str
    your_brand: str
    competitors: dict[str, str]


class CompetitorAnalysis(Loose):
    brand_overview: Str = ""
    main_category: Str = ""
    main_products_services: StrList = []
    brand_profile: BrandProfile = BrandProfile()
    competitors: list[Competitor] = []
    competitor_strengths: StrList = []  # derived server side
    competitor_gaps: StrList = []  # derived server side
    strengths: StrList = []  # the brand's competitive advantages
    opportunities: StrList = []  # market positioning improvements
    comparison_table: list[ComparisonRow] = []  # built server side


# section 2
class SearchQuery(Loose):
    query: Str
    platforms: StrList
    intent: Str = ""
    business_relevance: Str = ""
    target_audience: Str = ""
    # Never invented. Always null unless a real data source is wired in.
    search_volume: Optional[int] = None

    @field_validator("platforms")
    @classmethod
    def _normalize_platforms(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value:
            canon = _PLATFORM_ALIASES.get(item.strip().lower())
            if canon and canon not in out:
                out.append(canon)
        if not out:
            raise ValueError("platforms must include at least one of " + ", ".join(PLATFORMS))
        return out

    @field_validator("search_volume", mode="before")
    @classmethod
    def _never_trust_llm_volume(cls, _value: Any) -> None:
        return None


# section 3
class SupportingContent(Loose):
    type: Str = ""
    title: Str
    reason: Str = ""


class SEOImprovements(Loose):
    primary_keyword: Str
    secondary_keywords: StrList = []
    long_tail_keywords: StrList = []
    business_keywords: StrList = []
    recommended_title: Str
    title_rationale: Str = ""
    meta_description: Str
    supporting_content: list[SupportingContent] = []


# section 4
class Service(Loose):
    name: Str
    description: Str = ""
    target_industry: Str = ""
    target_customer: Str = ""
    main_benefit: Str = ""
    source: Literal["website", "inferred"] = "inferred"

    @field_validator("source", mode="before")
    @classmethod
    def _norm_source(cls, v: Any) -> str:
        return "website" if str(v).strip().lower().startswith("web") else "inferred"


# section 5
class TargetAudience(Loose):
    primary_audience: StrList = []
    secondary_audience: StrList = []
    industries: StrList = []
    business_types: StrList = []
    customer_profiles: StrList = []
    geographic_focus: StrList = []
    customer_needs: StrList = []
    interests: StrList = []
    recommended_segments: StrList = []


# LLM output envelope
class Brand(Loose):
    name: Str
    category: Str = ""
    summary: Str = ""


class Analysis(Loose):
    competitor_analysis: CompetitorAnalysis
    target_search_queries: list[SearchQuery]
    seo_improvements: SEOImprovements
    top_services: list[Service]
    target_audience: TargetAudience

    @model_validator(mode="after")
    def _check_counts(self) -> "Analysis":
        if len(self.target_search_queries) < 5:
            raise ValueError("target_search_queries must contain exactly 5 items")
        if not self.top_services:
            raise ValueError("top_services must not be empty")
        self.target_search_queries = self.target_search_queries[:5]
        self.top_services = self.top_services[:5]
        return self


class LLMOutput(Loose):
    brand: Brand
    analysis: Analysis


# response
class Meta(BaseModel):
    provider: str
    model: str
    cached: bool = False
    pages_fetched: list[str] = []
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: Optional[float] = None
    fetch_ms: int = 0
    llm_ms: int = 0
    total_ms: int = 0
    warnings: list[str] = []
    notes: list[str] = []


class LinkAnalysisResponse(BaseModel):
    success: bool = True
    website: str
    brand: Brand
    analysis: Analysis
    meta: Meta
