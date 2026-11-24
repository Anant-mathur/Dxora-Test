# AI Website Link Analysis API

FastAPI service with one endpoint, `POST /link-analysis`. Send a website URL. It fetches the site safely, extracts the real content, makes **one** LLM call, validates the answer against a strict schema and returns five structured sections: competitors, five search queries, SEO improvements, top services and target audience.

- **Live demo:** `TODO https://your-domain.com`
- **API docs:** `TODO https://your-domain.com/docs`
- **LLM research and final recommendation:** see [LLM platform and model research](#llm-platform-and-model-research)

## How it works

```
URL -> validate + SSRF check -> fetch home page (+ up to 2 service/about pages)
    -> strip noise, build a token-budgeted text -> ONE LLM call (JSON mode)
    -> parse + Pydantic validation (one corrective retry) -> post-process -> JSON
```

| Concern | What the code does |
|---|---|
| Real content only | Title, meta tags, headings, nav, JSON-LD and visible text from the home page plus the 2 best-scoring `services/about/products` pages. Scripts, styles, forms and cookie banners are removed. |
| SSRF protection | Scheme and port allowlist, no embedded credentials, hostname is **resolved and every address must be globally routable** (blocks loopback, RFC1918, link-local/cloud metadata, CGNAT, multicast, IPv4-mapped IPv6). Redirects are followed manually and **re-validated on every hop**. Response size is capped and only HTML is accepted. |
| Prompt injection | Scraped text is wrapped in `<website_content>` tags and the system prompt tells the model to treat it as data, not instructions. |
| Honest competitors | Competitors come from the model's general knowledge, not live research. Every competitor is stamped `source: "ai_generated_suggestion"` with a confidence level by the server, and the response `meta.notes` says so. The site itself and duplicates are removed. |
| No invented volumes | `search_volume` is always `null`. The server overwrites anything the model returns. |
| Services labelled | Each service has `source: "website"` or `"inferred"`. |
| Reliable JSON | Provider JSON mode, tolerant parsing (code fences), Pydantic validation, one retry that feeds the validation error back to the model. Exactly 5 queries enforced. |
| Failures | Typed errors with stable codes (table below). Unreachable sites, bot blocking, JavaScript-only pages, LLM outages, bad keys and rate limits all return a clean JSON error, never a stack trace. |
| Secrets | Keys only from environment variables. Gemini key is sent in a header, not the URL. `httpx` request logging is silenced. `.env` is git-ignored. |
| Cost control | One LLM call per site, cleaned text, content budget, in-memory cache, per-IP rate limit (protects the public deployment's budget). See [Cost optimization](#c-cost-optimization). |

Deterministic post-processing (no extra tokens): the competitor comparison table, the aggregated competitor strengths and gaps, de-duplication, platform name normalisation (`Twitter` becomes `X`), and SEO length warnings.

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # Windows: copy .env.example .env
# edit .env: set LLM_PROVIDER and the matching API key
```

| Provider | `LLM_PROVIDER` | Key variable | Default model |
|---|---|---|---|
| Google Gemini | `gemini` | `GEMINI_API_KEY` | `gemini-3.1-flash-lite` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-haiku-5-5` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-6.1-terra` (**not tested**, see research) |

Override the model with `LLM_MODEL`. Set `LLM_FALLBACK_PROVIDER` (and its key) to fail over automatically when the primary provider errors. With a fallback configured, the primary gets `LLM_PRIMARY_BUDGET_SECONDS` (default 30) for the whole attempt, retries included, before the fallback takes over, so a slow or overloaded provider cannot stall a request.

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 4000
```

Docs: <http://localhost:4000/docs>. Tests: `pytest` (50 tests, no network, no real LLM).

Behind a TLS-inspecting proxy or antivirus (common on corporate or some home networks) outbound HTTPS fails with `CERTIFICATE_VERIFY_FAILED`. For local runs only, set `USE_SYSTEM_CERTS=true` in `.env` to trust the OS certificate store. Leave it off in production.

## API

### Request

```bash
curl -X POST http://localhost:4000/link-analysis \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

Optional body field `"refresh": true` bypasses the cache.

### Response

A **real response** (abbreviated: lists cut to 2 items) from `POST /link-analysis` with `{"url": "https://plausible.io"}`, produced by `gemini-3.1-flash-lite`. The full, unabridged response is in [docs/sample_response_plausible.json](docs/sample_response_plausible.json).

```json
{
  "success": true,
  "website": "https://plausible.io/",
  "brand": {"name": "Plausible Analytics", "category": "Web Analytics SaaS",
            "summary": "Plausible is a lightweight, open-source, and privacy-friendly alternative to Google Analytics. ..."},
  "analysis": {
    "competitor_analysis": {
      "brand_overview": "An independent, EU-hosted analytics platform focused on simplicity, speed, and GDPR compliance.",
      "main_category": "Privacy-focused Web Analytics",
      "main_products_services": ["Web traffic analytics", "Goal and conversion tracking", "Real-time dashboard", "Funnel analysis", "SEO integration"],
      "competitors": [
        {"name": "Matomo", "website": "matomo.org", "main_service": "Self-hosted and cloud analytics",
         "strengths": ["Highly customizable", "Self-hosting option"], "weaknesses": ["Complex setup", "Heavier script"],
         "confidence": "high", "source": "ai_generated_suggestion"},
        {"name": "Fathom Analytics", "website": "usefathom.com", "...": "..."}
      ],
      "competitor_gaps": ["Matomo: Complex setup", "Matomo: Heavier script"]
    },
    "target_search_queries": [
      {"query": "privacy friendly google analytics alternative", "platforms": ["Google"], "intent": "Commercial",
       "business_relevance": "High", "target_audience": "Website owners", "search_volume": null},
      {"query": "how to track website traffic without cookies", "platforms": ["Google"], "...": "..."}
    ],
    "seo_improvements": {
      "primary_keyword": "privacy-friendly analytics",
      "secondary_keywords": ["open-source analytics", "GDPR compliant analytics"],
      "long_tail_keywords": ["how to replace google analytics", "lightweight website traffic tracking"],
      "business_keywords": ["website insights", "conversion tracking"],
      "recommended_title": "Plausible Analytics | Simple, Privacy-Friendly GA Alternative",
      "meta_description": "Plausible is a lightweight, open-source Google Analytics alternative. Get clear insights without cookies or GDPR consent banners.",
      "supporting_content": [{"type": "article", "title": "Migrating from GA4 to Plausible", "reason": "..."}]
    },
    "top_services": [
      {"name": "Web Traffic Analytics", "description": "Real-time, simple dashboard for tracking site visits and sources.",
       "target_industry": "All", "target_customer": "Website owners", "main_benefit": "Clear insights without complexity", "source": "website"}
    ],
    "target_audience": {"primary_audience": ["Website owners", "Developers"], "secondary_audience": ["Agency owners", "Privacy advocates"],
                        "industries": ["SaaS", "Ecommerce"], "geographic_focus": ["European Union", "Global"], "...": "..."}
  },
  "meta": {"provider": "gemini", "model": "gemini-3.1-flash-lite", "cached": false,
           "pages_fetched": ["https://plausible.io/", "https://plausible.io/features", "https://plausible.io/about"],
           "input_tokens": 3862, "output_tokens": 1800, "estimated_cost_usd": 0.003666,
           "fetch_ms": 1079, "llm_ms": 6778, "total_ms": 7859, "warnings": [],
           "notes": ["Competitors are AI-generated suggestions from the model's general knowledge, not live research.",
                     "No search volume data is included: none was available from a real data source."]}
}
```

### Errors

All errors use `{"success": false, "error": {"code": "...", "message": "..."}}`.

| HTTP | Code | When |
|---|---|---|
| 400 | `INVALID_URL` | Empty, malformed, non-http(s), credentials in URL, bare hostname |
| 403 | `URL_NOT_ALLOWED` | Private/internal address (SSRF) or non-standard port |
| 422 | `INVALID_REQUEST` | Body missing `url` |
| 422 | `WEBSITE_UNREACHABLE` | Domain does not resolve |
| 422 | `WEBSITE_INACCESSIBLE`, `WEBSITE_HTTP_ERROR`, `WEBSITE_TIMEOUT`, `NO_READABLE_CONTENT`, `UNSUPPORTED_CONTENT`, `TOO_MANY_REDIRECTS` | Site blocks bots, errors, times out, is JS-only, is not HTML |
| 429 | `RATE_LIMITED` | Per-IP limit hit (default 10/min) |
| 502 | `LLM_ERROR`, `LLM_INVALID_OUTPUT` | Provider failure, bad key, or invalid JSON after one retry |
| 503 | `LLM_NOT_CONFIGURED` | No API key for the selected provider |

## Project structure

```
app/
  main.py                 routes, error envelope, rate limit
  config.py               env-based settings
  schemas.py              request/response + LLM output validation
  errors.py               typed errors -> HTTP status + code
  services/
    website_extractor.py  safe fetch, redirects, HTML -> compact text
    llm_service.py        Gemini / Anthropic / OpenAI over HTTPS, retries, fallback, cost
    analysis_service.py   orchestration, prompt, validation + retry, cache
    competitor_service.py dedupe, labelling, comparison table
  utils/validators.py     URL normalisation + SSRF protection
tests/test_link_analysis.py
scripts/benchmark.py      measures latency / tokens / cost on real sites
```

## LLM platform and model research

Pricing for Gemini and Claude was read from provider pricing pages and secondary sources on **2026-11-24**. Prices change often, so re-check before relying on them. Each figure below is labelled **measured** (from my own runs), **verified** (read from a provider page or several consistent sources) or **unverified**.

### A. Comparison

| Provider | Model | Input / 1M tok | Output / 1M tok | Context window | Native JSON mode | Status |
|---|---|---|---|---|---|---|
| Google | `gemini-3.1-flash-lite` | $0.25 | $1.50 | not stated on the pricing page | Yes (`responseMimeType: application/json`) | Pricing verified. **Benchmarked.** Free tier exists. |
| Google | `gemini-3.5-flash-lite` | $0.30 | $2.50 | not stated | Yes | Pricing **unverified** (read once, not re-checked). **Benchmarked.** |
| Google | `gemini-3.8-flash` | $0.75 until 2026-12-31, then $1.50 | $3.75 until 2026-12-31, then $7.50 | not stated | Yes | **Could not be measured**: 0 of 10 runs succeeded (5x HTTP 503, 3x HTTP 429, 2x timeout) on my free-tier key. This says nothing about its quality. |
| Anthropic | `claude-haiku-5-5` | $0.10 (prompts up to 100K), $0.50 above | $0.50 (up to 100K), $2.50 above | 1M (third-party coverage, not the pricing page) | No dedicated flag; JSON enforced by prompt, validation and retry | Pricing verified against several consistent sources (I could not open Anthropic's page directly). **Benchmarked.** |
| Anthropic | `claude-sonnet-5-5` | $2.00 | $10.00 | 1M | No dedicated flag | Not benchmarked. Quality-ceiling reference only. |
| OpenAI | `gpt-6.1-terra` | $0.50 or $1.00 | $2.50 or $5.00 | not stated | Yes (`response_format: json_object`) | **Not tested** (no key) and **pricing ambiguous** (two different figures across sources). Not recommended without testing. |

Retired: `gemini-2.5-flash` and `gemini-2.5-flash-lite` return HTTP 404 ("no longer available to new users") and were removed from the code.

Sources: [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing), [OpenAI pricing](https://developers.openai.com/api/docs/pricing).

### Measured results

All numbers in this section are **measured** with [`scripts/benchmark.py`](scripts/benchmark.py): 5 real sites (plausible.io, dreamfolks.in, basecamp.com, buffer.com, zerodha.com) x 2 runs each = 10 runs per model, one at a time, cache disabled. "LLM s" is the LLM call only; the page fetch is separate (typically 1 to 4 s). Cost uses the token counts the provider returned and the prices in section A.

**Final configuration** (prompt includes a brevity rule; Anthropic thinking disabled):

| Model | Valid runs | Median LLM s | Max LLM s | Avg input tok | Avg output tok | Avg cost / analysis | Avg competitors |
|---|---|---|---|---|---|---|---|
| `gemini-3.1-flash-lite` | 10/10 | 7.0 | 8.3 | 3,443 | 1,823 | $0.00360 | 3.0 |
| `gemini-3.5-flash-lite` | 10/10 | 6.8 | 11.1 | 3,716 | 2,447 | $0.00723 (unverified price) | 3.3 |
| `claude-haiku-5-5` | 10/10 | 15.6 | 16.7 | 5,280 | 3,762 | $0.00241 | 3.6 |
| `gemini-3.8-flash` | 0/10 | n/a | n/a | n/a | n/a | n/a | n/a |

**Before the brevity rule** (same sites, same method), for transparency:

| Model | Valid runs | Median LLM s | Max LLM s | Avg output tok | Avg cost |
|---|---|---|---|---|---|
| `gemini-3.1-flash-lite` | 10/10 | 7.7 | 8.9 | 1,906 | $0.00370 |
| `claude-haiku-5-5` | 10/10 | 21.2 | 24.8 | 5,412 | $0.00328 |

What the testing found and fixed:

- **Haiku 5.5 thinks by default.** Thinking tokens are billed as output and count against `max_tokens`, which truncated the JSON (1 of 3 runs valid before the fix). The code now sends `thinking: {"type": "disabled"}` and no `temperature` (this model rejects it with HTTP 400). Valid runs went to 10/10.
- **Brevity rule:** capping descriptions at about 25 words cut Haiku's median latency from 21.2 s to 15.6 s and its cost by 26%. Gemini was unaffected. The cost: Haiku's outputs contained about 26% fewer numerals (27 to 20 per output on average), so some specifics were lost. On dreamfolks.in it dropped "over 1000 lounges" and, in one run, the bank and card-issuer audience.
- **Page selection bug:** the original subpage picker matched hints against long anchor text, so it fetched `/status` and `/changelog` instead of product pages. It now matches on URL path and short labels and skips operational pages.
- **Latency is not constant.** In an earlier 3-call probe, the same Gemini prompt took 7.6 s, 23.5 s and 43.5 s, plus one HTTP 503 ("high demand"). It did not recur across the later 20 runs, but spikes are possible, which is why the primary call has a time budget and a fallback.
- **First-attempt invalid JSON** occurred occasionally on every provider (Gemini "Extra data" and Haiku "Expecting ',' delimiter" were observed). The corrective retry recovered every case in the final benchmark. I did not count these systematically, so no rate is claimed.

**Accuracy: not measured against ground truth, so no accuracy ranking is claimed.** What I did: read full outputs for plausible.io and dreamfolks.in from both main models. The competitor lists for dreamfolks.in (Priority Pass, LoungeKey and similar global lounge-access programs) were confirmed as sensible by the project owner. Haiku's outputs were more specific (it cited site details such as plan prices); Gemini's were shorter and, in earlier runs, once listed a product-analytics tool (Mixpanel) as a competitor and returned only 3 services before the prompt required 5. Specific numbers quoted by Haiku (for example a golf plan price and "1000+ lounges") were **not** independently verified. Treat this as a qualitative impression from 2 sites, not a score.

### B. Selection criteria

1. Grounded content understanding (does it stay inside the extracted text).
2. Latency (one call, about 5K tokens in, about 2.5K out).
3. Cost per analysis.
4. Valid JSON on the first try (measured as "valid runs" and retry count).
5. Quality of competitor and SEO suggestions (human-scored).
6. Context headroom (not a constraint here: content is capped at about 4K tokens).

### C. Cost optimization

Implemented in this code:

- **One LLM call per website** for all five sections, instead of five calls that would each repeat the page content.
- **Clean before sending:** scripts, styles, forms, SVG, cookie banners and duplicate lines are removed; text is capped (`MAX_CONTENT_CHARS`, default 14,000 chars, about 3.5K tokens) with most of the budget on the home page.
- **Only 2 extra pages**, chosen by relevance score, not a crawl.
- **Deterministic work stays out of the LLM:** the comparison table, competitor aggregation, de-duplication and platform normalisation are computed server side.
- **Cache** (in memory, TTL 1 hour) so repeat requests cost nothing. `refresh` bypasses it.
- **Small model** for a structured-extraction task, with a **fallback** model for reliability.
- **Per-IP rate limit** so a public demo cannot drain the API budget.
- **Retry only on invalid output**, once, and only the failing call.

Not implemented, worth doing at scale: provider prompt caching for the static system prompt and schema, batch APIs (about 50% cheaper, but asynchronous), a persistent cache (Redis), and a cheap model for extraction with a stronger model only for the competitor section.

### D. Final recommendation

**Measured** per-analysis cost (token counts from real runs, current prices):

| Model | Cost / analysis | Cost / 1,000 analyses |
|---|---|---|
| `claude-haiku-5-5` | $0.0024 | about $2.41 |
| `gemini-3.1-flash-lite` | $0.0036 | about $3.60 |
| `gemini-3.5-flash-lite` | $0.0072 (price unverified) | about $7.23 |

- **Recommended primary: Google `gemini-3.1-flash-lite`.** It had the lowest and most stable latency in the final benchmark (7.0 s median, 8.3 s max, 10/10 valid), has native JSON mode and a free tier that suits a public demo. It is not the cheapest per analysis: Haiku costs about a third less but is about 2x slower.
- **Fallback: Anthropic `claude-haiku-5-5`** via `LLM_FALLBACK_PROVIDER=anthropic`, used when Gemini errors or exceeds the 30 s budget. A different provider also protects against a Google-wide outage.
- **Expected response time** (measured): about 7 s LLM plus 1 to 4 s page fetch, so roughly **8 to 12 s end to end** with Gemini (one real call: 7.9 s total; a live call on dreamfolks.in took about 17 s while a benchmark was running). After a Gemini timeout the fallback adds up to about 45 s worst case. Haiku alone is about 16 to 20 s.
- **Cost optimization ideas not yet used:** provider prompt caching for the static system prompt and schema (about 1.2K of about 3.5K input tokens), batch APIs for non-interactive bulk runs (about 50% cheaper), a Redis cache shared across instances, and a two-tier design that escalates to a larger model only when validation fails twice.
- **Balancing quality and cost:** small model by default, retry only on invalid output, escalate only on repeated failure.
- **Claim limits:** "maximum accuracy" is **not** claimed. It would need a labelled ground-truth set, which this project does not have. OpenAI was not tested, so no claim is made about it.

## Limitations (honest list)

- **JavaScript-rendered sites** return little HTML. They are detected and rejected with `NO_READABLE_CONTENT` or analysed with a warning. A headless browser would fix this but costs more and widens the attack surface.
- **Competitors are model suggestions**, not research. No live search or SEO data source is connected, which is why search volumes are `null`.
- **DNS rebinding:** the SSRF check resolves the host before connecting, so a hostile DNS server could change the answer in between. For hard guarantees, block private ranges at the network layer (egress rules) in addition.
- **Rate limit and cache are in memory** and per process. Use Redis if you run several workers.

## Deployment (Render example)

1. Push this repo (public) to GitHub. Confirm `.env` is not in it.
2. Render: New, Web Service, connect the repo. Runtime: Docker (uses the included `Dockerfile`).
3. Environment variables: `LLM_PROVIDER`, the matching `*_API_KEY`, `TRUST_PROXY=true`. Optionally `RATE_LIMIT_PER_MINUTE`.
4. Deploy, then check `https://<service>/docs` and call `POST https://<service>/link-analysis`.

## Screenshots and logs

- [docs/evidence_logs.txt](docs/evidence_logs.txt): real `curl` requests and responses against the running server for SSRF rejections (loopback, cloud-metadata address, private range), a nonexistent domain, a malformed URL, a missing field, a successful analysis, and the `pytest` result (50 passed).
- [docs/sample_response_plausible.json](docs/sample_response_plausible.json): complete real response.
- Swagger UI and live-deployment screenshots: `TODO add after deploy` (place images in `docs/`).
