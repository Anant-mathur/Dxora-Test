"""Fetches a website safely and turns its HTML into compact, LLM-ready text.

Safety: every request (and every redirect hop) is validated against private/internal
addresses, redirects are followed manually, body size is capped, and only HTML is accepted.
"""
import asyncio
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from app.config import Settings
from app.errors import AppError, WebsiteFetchError
from app.utils.validators import assert_public_host, normalize_url

USER_AGENT = "Mozilla/5.0 (compatible; LinkAnalysisBot/1.0)"
MAX_REDIRECTS = 5
MAX_SUBPAGES = 2
NOISE_TAGS = ["script", "style", "noscript", "svg", "iframe", "template", "canvas", "form", "picture"]
NOISE_ATTR_RE = re.compile(r"cookie|consent|gdpr", re.I)
SUBPAGE_HINTS = ["service", "solution", "product", "what-we-do", "offering", "platform",
                 "feature", "about", "company", "pricing", "who-we-are"]
SKIP_PATH_RE = re.compile(r"\.(pdf|jpg|jpeg|png|gif|svg|zip|mp4|webp|css|js)$|/(login|signin|signup|register|sites|cart|privacy|terms|status|changelog|blog|docs|careers|contact|legal|cookie)", re.I)


@dataclass
class PageContent:
    url: str
    title: str = ""
    meta_description: str = ""
    site_name: str = ""
    headings: list[str] = field(default_factory=list)
    nav: list[str] = field(default_factory=list)
    structured_data: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute href, anchor text)


@dataclass
class ExtractedSite:
    final_url: str
    pages: list[PageContent]
    content: str  # assembled, token-budgeted text for the LLM
    warnings: list[str] = field(default_factory=list)


# parsing
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_html(html: str, url: str) -> PageContent:
    soup = BeautifulSoup(html, "html.parser")
    page = PageContent(url=url)
    page.title = _clean(soup.title.string) if soup.title and soup.title.string else ""

    def meta(*names: str) -> str:
        for n in names:
            tag = soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"property": n})
            if tag and tag.get("content"):
                return _clean(tag["content"])
        return ""

    page.meta_description = meta("description", "og:description", "twitter:description")
    page.site_name = meta("og:site_name", "application-name")

    structured: list[str] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        for item in data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []:
            if isinstance(item, dict):
                keep = {k: item[k] for k in ("@type", "name", "description", "areaServed", "address", "serviceType", "knowsAbout") if k in item}
                if keep:
                    structured.append(json.dumps(keep, ensure_ascii=False)[:500])
    page.structured_data = " | ".join(structured[:3])

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        page.links.append((urljoin(url, href), _clean(a.get_text(" "))[:60]))

    nav = soup.find("nav")
    if nav:
        seen: set[str] = set()
        for a in nav.find_all("a"):
            label = _clean(a.get_text(" "))[:40]
            if label and label.lower() not in seen:
                seen.add(label.lower())
                page.nav.append(label)
        page.nav = page.nav[:25]

    for tag in soup(NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        ident = " ".join([tag.get("id") or "", " ".join(tag.get("class") or [])])
        if ident.strip() and NOISE_ATTR_RE.search(ident):
            tag.decompose()

    page.headings = []
    for h in soup.find_all(["h1", "h2", "h3"]):
        text = _clean(h.get_text(" "))
        if 2 < len(text) < 160 and text not in page.headings:
            page.headings.append(text)
    page.headings = page.headings[:25]

    root = soup.find("main") or soup.body or soup
    lines, seen_lines = [], set()
    for raw in root.get_text("\n").splitlines():
        line = _clean(raw)
        if len(line) < 3 or line.lower() in seen_lines:
            continue
        seen_lines.add(line.lower())
        lines.append(line)
    page.text = "\n".join(lines)
    return page


def pick_subpages(home: PageContent, final_url: str) -> list[str]:
    """Choose up to MAX_SUBPAGES same-site pages most likely to describe the business."""
    base_host = (urlsplit(final_url).hostname or "").removeprefix("www.")
    scored: dict[str, int] = {}
    fallback: dict[str, None] = {}
    for href, anchor in home.links:
        parts = urlsplit(href)
        if parts.scheme not in ("http", "https") or (parts.hostname or "").removeprefix("www.") != base_host:
            continue
        path = parts.path.rstrip("/").lower()
        if not path or SKIP_PATH_RE.search(path):
            continue
        key = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if key.rstrip("/") == final_url.rstrip("/"):
            continue
        # long anchors are descriptions, not labels, so ignore them
        label = anchor.lower() if len(anchor) <= 30 else ""
        haystack = f"{path} {label}"
        score = max((len(SUBPAGE_HINTS) - i for i, hint in enumerate(SUBPAGE_HINTS) if hint in haystack), default=0)
        if score:
            scored[key] = max(scored.get(key, 0), score)
        fallback.setdefault(key, None)  # insertion order = order in the page's nav
    picks = [u for u, _ in sorted(scored.items(), key=lambda kv: -kv[1])[:MAX_SUBPAGES]]
    for key in fallback:  # top up with the first nav links
        if len(picks) >= MAX_SUBPAGES:
            break
        if key not in picks:
            picks.append(key)
    return picks


# fetching
async def fetch_html(client: httpx.AsyncClient, url: str, settings: Settings) -> tuple[str, str]:
    """GET with manual, validated redirects and a response size cap. Returns (final_url, html)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        await asyncio.to_thread(assert_public_host, urlsplit(current).hostname or "")
        try:
            async with client.stream("GET", current, follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise WebsiteFetchError("Website sent a redirect without a location.")
                    current = normalize_url(urljoin(current, location))
                    continue
                if resp.status_code >= 400:
                    raise WebsiteFetchError(
                        f"Website returned HTTP {resp.status_code} (it may block automated access).",
                        "WEBSITE_HTTP_ERROR",
                    )
                ctype = resp.headers.get("content-type", "").lower()
                if ctype and "html" not in ctype and "xml" not in ctype and "text/plain" not in ctype:
                    raise WebsiteFetchError(f"Unsupported content type: {ctype.split(';')[0]}.", "UNSUPPORTED_CONTENT")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > settings.max_response_bytes:
                        break
                return current, bytes(body[: settings.max_response_bytes]).decode(resp.encoding or "utf-8", errors="replace")
        except httpx.TimeoutException:
            raise WebsiteFetchError("Website took too long to respond.", "WEBSITE_TIMEOUT")
        except httpx.HTTPError as exc:
            raise WebsiteFetchError(f"Could not connect to the website ({type(exc).__name__}).")
    raise WebsiteFetchError("Too many redirects.", "TOO_MANY_REDIRECTS")


def assemble(pages: list[PageContent], max_chars: int) -> str:
    """Build the LLM input. Home page gets the largest share; subpages get the rest."""
    parts: list[str] = []
    for i, p in enumerate(pages):
        budget = int(max_chars * (0.55 if i == 0 else 0.45 / max(1, len(pages) - 1)))
        head = [f"## PAGE: {p.url}"]
        if p.title: head.append(f"Title: {p.title}")
        if p.site_name: head.append(f"Site name: {p.site_name}")
        if p.meta_description: head.append(f"Meta description: {p.meta_description}")
        if p.nav and i == 0: head.append("Navigation: " + " | ".join(p.nav))
        if p.headings: head.append("Headings: " + " | ".join(p.headings))
        if p.structured_data: head.append(f"Structured data: {p.structured_data}")
        header = "\n".join(head)
        body = p.text[: max(0, budget - len(header))]
        parts.append(f"{header}\nText:\n{body}")
    return "\n\n".join(parts)[: max_chars + 2000]


async def extract_site(url: str, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> ExtractedSite:
    warnings: list[str] = []
    async with httpx.AsyncClient(
        timeout=settings.fetch_timeout, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
        transport=transport,
    ) as client:
        final_url, html = await fetch_html(client, url, settings)
        home = parse_html(html, final_url)
        pages = [home]

        async def get_sub(sub_url: str) -> PageContent | None:
            try:
                u, h = await fetch_html(client, sub_url, settings)
                return parse_html(h, u)
            except AppError:
                return None  # subpages are best-effort

        subs = await asyncio.gather(*(get_sub(u) for u in pick_subpages(home, final_url)))
        pages += [s for s in subs if s]

    total_text = sum(len(p.text) for p in pages)
    if total_text < 150 and not home.meta_description:
        raise WebsiteFetchError(
            "The page has almost no readable text (it is probably rendered with JavaScript or blocks bots).",
            "NO_READABLE_CONTENT",
        )
    if total_text < 600:
        warnings.append("Very little text was found on the site; the analysis relies mostly on metadata and may be generic.")
    return ExtractedSite(final_url=final_url, pages=pages, content=assemble(pages, settings.max_content_chars), warnings=warnings)
