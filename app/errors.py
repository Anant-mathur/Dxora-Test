"""Application errors. Every error maps to one HTTP status and one machine-readable code."""


class AppError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class InvalidURLError(AppError):
    status_code = 400
    code = "INVALID_URL"


class BlockedURLError(AppError):
    """URL points at a private/internal address (SSRF protection)."""

    status_code = 403
    code = "URL_NOT_ALLOWED"


class HostResolutionError(AppError):
    status_code = 422
    code = "WEBSITE_UNREACHABLE"


class WebsiteFetchError(AppError):
    status_code = 422
    code = "WEBSITE_INACCESSIBLE"


class LLMNotConfiguredError(AppError):
    status_code = 503
    code = "LLM_NOT_CONFIGURED"


class LLMError(AppError):
    status_code = 502
    code = "LLM_ERROR"


class LLMOutputError(AppError):
    status_code = 502
    code = "LLM_INVALID_OUTPUT"


class RateLimitError(AppError):
    status_code = 429
    code = "RATE_LIMITED"
