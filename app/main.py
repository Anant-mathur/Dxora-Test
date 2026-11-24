"""FastAPI entrypoint: uvicorn app.main:app --host 0.0.0.0 --port 4000"""
import logging
import os
import time
from collections import defaultdict, deque
from functools import lru_cache

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import Settings
from app.errors import AppError, RateLimitError
from app.schemas import LinkAnalysisRequest, LinkAnalysisResponse
from app.services.analysis_service import AnalysisService
from app.services.llm_service import LLMService

from dotenv import load_dotenv

load_dotenv()
# local-only: use the OS cert store behind TLS-inspecting proxies
if os.getenv("USE_SYSTEM_CERTS", "").lower() in ("1", "true", "yes"):
    import truststore

    truststore.inject_into_ssl()

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # keep URLs/keys out of logs
logging.getLogger("httpcore").setLevel(logging.WARNING)

app = FastAPI(
    title="AI Website Link Analysis API",
    description="Analyzes a website with an LLM: competitors, search queries, SEO, services, audience.",
    version="1.0.0",
)


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache
def get_analysis_service() -> AnalysisService:
    settings = get_settings()
    return AnalysisService(settings, LLMService(settings))


_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Per-IP sliding window. Protects the public deployment's LLM budget."""
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return
    ip = request.client.host if request.client else "unknown"
    if settings.trust_proxy and (fwd := request.headers.get("x-forwarded-for")):
        ip = fwd.split(",")[0].strip()
    now, window = time.time(), _hits[ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= limit:
        raise RateLimitError(f"Rate limit exceeded ({limit} requests per minute). Try again shortly.")
    window.append(now)


def _error(status: int, code: str, message: str, details=None) -> JSONResponse:
    body = {"success": False, "error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return _error(exc.status_code, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    details = [{"field": ".".join(str(p) for p in e["loc"] if p != "body") or "body", "message": e["msg"]} for e in exc.errors()]
    return _error(422, "INVALID_REQUEST", "Request body is invalid. Send JSON like {\"url\": \"https://example.com\"}.", details)


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    logging.getLogger("app").exception("Unhandled error")
    return _error(500, "INTERNAL_ERROR", "Unexpected server error.")


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@app.post("/link-analysis", response_model=LinkAnalysisResponse, dependencies=[Depends(rate_limit)])
async def link_analysis(
    request: LinkAnalysisRequest, service: AnalysisService = Depends(get_analysis_service)
) -> LinkAnalysisResponse:
    return await service.analyze(request.url, refresh=request.refresh)
