"""FastAPI application: REST API, Telegram webhook, tracking, admin dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import settings
from app.core.errors import AdNetError
from app.core.logging import configure_logging, get_logger
from app.db.session import healthcheck

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("starting", env=settings.app_env, name=settings.app_name)
    yield
    log.info("shutting down")


app = FastAPI(
    title=f"{settings.app_name} API",
    description=(
        "Independent third-party Telegram advertising network. "
        "Monetary amounts are strings in JSON, never numbers: a float cannot "
        "represent money exactly."
    ),
    version="1.0.0",
    lifespan=lifespan,
    # The interactive docs are useful in development and an information leak in
    # production, where the schema is served only to authenticated staff.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)

if settings.allowed_host_list != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key",
                       "X-Telegram-Init-Data"],
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline hardening for the admin dashboard and any HTML we serve (spec §28)."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "frame-ancestors 'none'",
    )
    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


# --- error handling (spec §25) -------------------------------------------


@app.exception_handler(AdNetError)
async def domain_error_handler(request: Request, exc: AdNetError) -> JSONResponse:
    if exc.http_status >= 500:
        log.error("domain_error", code=exc.code, message=exc.message)
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_failed",
                "message": "the request body or query is invalid",
                "context": {"details": _safe_errors(exc.errors())},
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak a stack trace or SQL to a caller.
    log.exception("unhandled_error", path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error",
                           "message": "an internal error occurred", "context": {}}},
    )


def _safe_errors(errors: list) -> list[dict]:
    out = []
    for error in errors:
        out.append(
            {
                "field": ".".join(str(p) for p in error.get("loc", ())[1:]),
                "message": error.get("msg", "invalid"),
                "type": error.get("type", "value_error"),
            }
        )
    return out[:20]


# --- routes ---------------------------------------------------------------

from app.admin.routes import router as admin_ui_router  # noqa: E402
from app.api.v1 import admin, advertisers, auth, publishers, tracking  # noqa: E402
from app.bot.webhook import router as webhook_router  # noqa: E402

API_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(advertisers.router, prefix=API_PREFIX)
app.include_router(publishers.router, prefix=API_PREFIX)
app.include_router(admin.router, prefix=API_PREFIX)
app.include_router(tracking.router)
app.include_router(webhook_router)
app.include_router(admin_ui_router)

try:
    app.mount("/admin/static", StaticFiles(directory="app/admin/static"), name="admin-static")
except Exception:  # pragma: no cover - directory may be absent in a slim image
    pass


@app.get("/health", tags=["ops"])
def health() -> dict:
    ok = healthcheck()
    return {"status": "ok" if ok else "degraded", "database": ok}


@app.get("/", tags=["ops"])
def root() -> dict:
    return {"name": settings.app_name, "version": "1.0.0", "api": API_PREFIX}
