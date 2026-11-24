"""No real network, no real LLM: MockTransport, a fake LLM and dependency overrides."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.errors import BlockedURLError, InvalidURLError, WebsiteFetchError
from app.services import website_extractor as we
from app.services.analysis_service import AnalysisService, parse_llm_json
from app.services.llm_service import LLMResult, LLMService, estimate_cost
from app.utils import validators

HOME_HTML = """<html><head><title>Acme Plumbing | Emergency Plumber in Leeds</title>
<meta name="description" content="Fast, fair-priced plumbing and boiler repair across Leeds.">
<script type="application/ld+json">{"@type":"Plumber","name":"Acme Plumbing","areaServed":"Leeds"}</script></head>
<body><nav><a href="/services">Services</a><a href="/about">About</a></nav>
<div id="cookie-banner">We use cookies, accept all</div>
<main><h1>Emergency plumber in Leeds</h1>
<p>Acme Plumbing repairs boilers, fixes leaks and installs bathrooms for homeowners and landlords across Leeds. Call us 24/7 for emergency callouts.</p>
<script>var secret = 1;</script></main></body></html>"""
SERVICES_HTML = "<html><head><title>Services</title></head><body><main><h2>Boiler repair</h2><p>Same-day boiler repair and annual servicing for landlords and homeowners in Leeds.</p></main></body></html>"

GOOD_LLM_JSON = {
    "brand": {"name": "Acme Plumbing", "category": "Plumbing services", "summary": "Leeds plumber."},
    "analysis": {
        "competitor_analysis": {
            "brand_overview": "Local plumber.", "main_category": "Plumbing", "main_products_services": ["Boiler repair"],
            "brand_profile": {"main_service": "Emergency plumbing", "target_market": "Leeds homeowners", "key_strength": "24/7 callouts", "opportunity": "Add online booking"},
            "competitors": [
                {"name": "Acme Plumbing", "website": "acme.test", "confidence": "high"},
                {"name": "Pimlico Plumbers", "website": "pimlico.test", "key_strength": "Brand", "weaknesses": ["London only"], "confidence": "medium"},
                {"name": "Dyno", "website": "dyno.test", "key_strength": "National", "weaknesses": ["Pricey"]},
                {"name": "pimlico plumbers", "website": "dup.test"},
            ],
            "strengths": ["24/7 service"], "opportunities": ["Google Business Profile"],
        },
        "target_search_queries": [
            {"query": f"q{i}", "platforms": ["Google", "Twitter"], "intent": "Service Discovery", "search_volume": 9999} for i in range(5)
        ],
        "seo_improvements": {"primary_keyword": "emergency plumber leeds", "recommended_title": "Emergency Plumber Leeds | Acme Plumbing",
                             "meta_description": "24/7 emergency plumber in Leeds. Boiler repair, leak fixes and bathroom installs from a local, fair-priced team. Call now."},
        "top_services": [{"name": "Boiler repair", "source": "website"}],
        "target_audience": {"primary_audience": ["Homeowners"], "geographic_focus": "Leeds"},
    },
}


class FakeLLM(LLMService):
    def __init__(self, replies):
        super().__init__(Settings(env={"GEMINI_API_KEY": "x"}))
        self.replies, self.calls = list(replies), 0

    async def generate_json(self, system, user, max_output_tokens=6000):
        self.calls += 1
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return LLMResult(text, 3000, 1500, "gemini", "gemini-3.1-flash-lite", 800)


def site_transport(pages=None, status=200):
    pages = pages or {"/": HOME_HTML, "/services": SERVICES_HTML}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in pages:
            return httpx.Response(status, text=pages[request.url.path], headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """Fake DNS only (every domain resolves to a public IP); the real SSRF logic still runs."""
    monkeypatch.setattr(validators, "resolve_host", lambda host: ["93.184.216.34"])


@pytest.fixture
def client():
    yield TestClient(main.app, raise_server_exceptions=False)
    main.app.dependency_overrides.clear()
    main._hits.clear()


def use_service(reply_list, transport=None, **env):
    settings = Settings(env={"GEMINI_API_KEY": "x", **env})
    svc = AnalysisService(settings, FakeLLM(reply_list), transport or site_transport())
    main.app.dependency_overrides[main.get_analysis_service] = lambda: svc
    main.app.dependency_overrides[main.get_settings] = lambda: settings
    return svc


# URL validation / SSRF
@pytest.mark.parametrize("raw, expected", [
    ("example.com", "https://example.com/"),
    ("  https://Example.com/a?b=1#frag ", "https://example.com/a?b=1"),
    ("http://example.com:80/x", "http://example.com:80/x"),
])
def test_normalize_ok(raw, expected):
    assert validators.normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ftp://example.com", "javascript:alert(1)", "http://localhost", "https://user:pw@example.com", "http://exa mple.com", "https://" + "a" * 3000 + ".com"])
def test_normalize_rejects_invalid(raw):
    with pytest.raises((InvalidURLError, BlockedURLError)):
        validators.normalize_url(raw)


def test_unusual_port_blocked():
    with pytest.raises(BlockedURLError):
        validators.normalize_url("http://example.com:6379/")


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "::ffff:127.0.0.1", "fe80::1", "224.0.0.1"])
def test_private_ips_blocked(ip):
    with pytest.raises(BlockedURLError):
        validators.assert_public_host(ip)


def test_public_ip_allowed():
    validators.assert_public_host("8.8.8.8")


def test_hostname_resolving_to_private_is_blocked(monkeypatch):
    monkeypatch.setattr(validators, "resolve_host", lambda h: ["93.184.216.34", "10.0.0.7"])
    with pytest.raises(BlockedURLError):
        validators.assert_public_host("evil.example.com")


def test_decimal_ip_trick_is_not_accepted():
    with pytest.raises(InvalidURLError):
        validators.normalize_url("http://2130706433/")  # 127.0.0.1 as one integer


# extraction
def test_parse_html_strips_noise_and_keeps_signal():
    page = we.parse_html(HOME_HTML, "https://acme.test/")
    assert page.title.startswith("Acme Plumbing")
    assert "Fast, fair-priced" in page.meta_description
    assert "secret" not in page.text and "cookies" not in page.text
    assert "Emergency plumber in Leeds" in page.headings
    assert "Plumber" in page.structured_data
    assert page.nav == ["Services", "About"]


def test_subpage_selection_prefers_service_pages():
    page = we.parse_html(HOME_HTML, "https://acme.test/")
    assert we.pick_subpages(page, "https://acme.test/")[0] == "https://acme.test/services"


def test_extract_site_fetches_home_and_subpages():
    site = asyncio.run(we.extract_site("https://acme.test/", Settings(), site_transport()))
    assert len(site.pages) >= 2 and "Boiler repair" in site.content


def test_http_error_is_graceful():
    with pytest.raises(WebsiteFetchError) as e:
        asyncio.run(we.extract_site("https://acme.test/", Settings(), site_transport(status=403)))
    assert "403" in e.value.message


def test_js_only_page_is_rejected_clearly():
    t = site_transport({"/": "<html><body><div id='root'></div><script>app()</script></body></html>"})
    with pytest.raises(WebsiteFetchError) as e:
        asyncio.run(we.extract_site("https://spa.test/", Settings(), t))
    assert e.value.code == "NO_READABLE_CONTENT"


def test_redirect_to_internal_host_is_blocked(monkeypatch):
    def guard(host):
        if host == "internal.test":
            raise BlockedURLError("private")
    monkeypatch.setattr(we, "assert_public_host", guard)

    def handler(request):
        return httpx.Response(302, headers={"location": "http://internal.test/admin"}) if request.url.host == "acme.test" else httpx.Response(200, text="SECRET")

    with pytest.raises(BlockedURLError):
        asyncio.run(we.extract_site("https://acme.test/", Settings(), httpx.MockTransport(handler)))


# LLM output handling
def test_parse_llm_json_handles_code_fences():
    assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_cost_estimate():
    assert estimate_cost("gemini-3.1-flash-lite", 3000, 1500, Settings()) == round(3000 * 0.25 / 1e6 + 1500 * 1.5 / 1e6, 6)
    assert estimate_cost("unknown-model", 1, 1, Settings()) is None


# endpoint
def test_success_shape_and_postprocessing(client):
    use_service([json.dumps(GOOD_LLM_JSON)])
    r = client.post("/link-analysis", json={"url": "https://acme.test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True and body["brand"]["name"] == "Acme Plumbing"
    a = body["analysis"]
    assert len(a["target_search_queries"]) == 5
    assert all(q["search_volume"] is None for q in a["target_search_queries"])  # never invented
    assert a["target_search_queries"][0]["platforms"] == ["Google", "X"]  # Twitter normalized
    names = [c["name"] for c in a["competitor_analysis"]["competitors"]]
    assert names == ["Pimlico Plumbers", "Dyno"]  # self + duplicate removed
    assert all(c["source"] == "ai_generated_suggestion" for c in a["competitor_analysis"]["competitors"])
    assert a["competitor_analysis"]["comparison_table"][0]["attribute"] == "Brand Name"
    assert a["target_audience"]["geographic_focus"] == ["Leeds"]  # str coerced to list
    assert any("competitor" in w for w in body["meta"]["warnings"])  # only 2 competitors, flagged
    assert body["meta"]["input_tokens"] == 3000 and body["meta"]["estimated_cost_usd"] > 0


def test_second_call_is_cached(client):
    svc = use_service([json.dumps(GOOD_LLM_JSON)])
    client.post("/link-analysis", json={"url": "https://acme.test"})
    r = client.post("/link-analysis", json={"url": "acme.test"})
    assert r.json()["meta"]["cached"] is True and svc.llm.calls == 1
    client.post("/link-analysis", json={"url": "https://acme.test", "refresh": True})
    assert svc.llm.calls == 2


def test_invalid_llm_json_is_retried_once_then_succeeds(client):
    svc = use_service(["not json at all", json.dumps(GOOD_LLM_JSON)])
    assert client.post("/link-analysis", json={"url": "https://acme.test"}).status_code == 200
    assert svc.llm.calls == 2


def test_invalid_llm_json_twice_returns_502(client):
    use_service(["garbage"])
    r = client.post("/link-analysis", json={"url": "https://acme.test"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "LLM_INVALID_OUTPUT"


def test_too_few_queries_fails_validation(client):
    bad = json.loads(json.dumps(GOOD_LLM_JSON))
    bad["analysis"]["target_search_queries"] = bad["analysis"]["target_search_queries"][:2]
    use_service([json.dumps(bad)])
    assert client.post("/link-analysis", json={"url": "https://acme.test"}).status_code == 502


@pytest.mark.parametrize("url, status, code", [
    ("ftp://acme.test", 400, "INVALID_URL"),
    ("not a url", 400, "INVALID_URL"),
    ("http://127.0.0.1/admin", 403, "URL_NOT_ALLOWED"),
    ("http://169.254.169.254/latest/meta-data", 403, "URL_NOT_ALLOWED"),
])
def test_bad_urls(client, url, status, code):
    use_service([json.dumps(GOOD_LLM_JSON)])
    r = client.post("/link-analysis", json={"url": url})
    assert r.status_code == status and r.json() == {"success": False, "error": {"code": code, "message": r.json()["error"]["message"]}}


def test_missing_url_field(client):
    use_service([json.dumps(GOOD_LLM_JSON)])
    r = client.post("/link-analysis", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_REQUEST"


def test_unreachable_website_returns_422(client):
    use_service([json.dumps(GOOD_LLM_JSON)], transport=site_transport(status=500))
    r = client.post("/link-analysis", json={"url": "https://acme.test"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "WEBSITE_HTTP_ERROR"


def test_rate_limit(client):
    use_service([json.dumps(GOOD_LLM_JSON)], RATE_LIMIT_PER_MINUTE="2")
    codes = [client.post("/link-analysis", json={"url": "ftp://x.test"}).status_code for _ in range(3)]
    assert codes == [400, 400, 429]


def test_missing_api_key_returns_503(client):
    settings = Settings(env={})
    svc = AnalysisService(settings, LLMService(settings), site_transport())
    main.app.dependency_overrides[main.get_analysis_service] = lambda: svc
    main.app.dependency_overrides[main.get_settings] = lambda: settings
    r = client.post("/link-analysis", json={"url": "https://acme.test"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "LLM_NOT_CONFIGURED"


# LLM provider payloads (mock HTTP)
def test_gemini_request_and_parse():
    seen = {}

    def handler(request):
        seen["key_header"] = request.headers.get("x-goog-api-key")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{\"ok\":true}"}]}}],
                                         "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4, "thoughtsTokenCount": 2}})

    llm = LLMService(Settings(env={"GEMINI_API_KEY": "SECRETKEY"}), httpx.MockTransport(handler))
    res = asyncio.run(llm.generate_json("sys", "user"))
    assert res.text == '{"ok":true}' and (res.input_tokens, res.output_tokens) == (10, 6)
    assert seen["key_header"] == "SECRETKEY" and "SECRETKEY" not in seen["url"]
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"


def test_fallback_provider_used_when_primary_fails():
    def handler(request):
        if "googleapis" in request.url.host:
            return httpx.Response(400, text="bad")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}], "usage": {"input_tokens": 5, "output_tokens": 1}})

    env = {"GEMINI_API_KEY": "a", "ANTHROPIC_API_KEY": "b", "LLM_FALLBACK_PROVIDER": "anthropic"}
    res = asyncio.run(LLMService(Settings(env=env), httpx.MockTransport(handler)).generate_json("s", "u"))
    assert res.provider == "anthropic"


def test_slow_primary_hands_over_to_fallback_within_budget():
    async def slow_then_ok(request):
        if "googleapis" in request.url.host:
            await asyncio.sleep(5)  # far longer than the 0.2s budget below
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}], "usage": {"input_tokens": 5, "output_tokens": 1}})

    env = {"GEMINI_API_KEY": "a", "ANTHROPIC_API_KEY": "b", "LLM_FALLBACK_PROVIDER": "anthropic", "LLM_PRIMARY_BUDGET_SECONDS": "0.2"}
    import time
    t = time.perf_counter()
    res = asyncio.run(LLMService(Settings(env=env), httpx.MockTransport(slow_then_ok)).generate_json("s", "u"))
    assert res.provider == "anthropic"
    assert time.perf_counter() - t < 3
