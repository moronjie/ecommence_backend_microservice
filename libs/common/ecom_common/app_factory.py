from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastapi import FastAPI

from ecom_common.config import BaseServiceSettings
from ecom_common.exceptions import register_exception_handlers
from ecom_common.health import ReadinessCheck, build_health_router
from ecom_common.identity import VerifyIdentityMiddleware
from ecom_common.logging import setup_logging
from ecom_common.metrics import add_metrics
from ecom_common.middleware import RequestContextMiddleware


def create_service_app(
    settings: BaseServiceSettings,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[Any]] | None = None,
    readiness_checks: dict[str, ReadinessCheck] | None = None,
    verify_identity: bool = True,
    docs: bool = True,
) -> FastAPI:
    settings.assert_safe_for_environment()
    setup_logging(settings.service_name, settings.log_level)
    app = FastAPI(
        title=settings.service_name,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.add_middleware(RequestContextMiddleware)
    if verify_identity and settings.internal_auth_secret:
        app.add_middleware(VerifyIdentityMiddleware, secret=settings.internal_auth_secret)
    register_exception_handlers(app)
    app.include_router(build_health_router(settings.service_name, readiness_checks))
    add_metrics(app, settings.service_name, settings.metrics_token)  # outermost: measures it all
    return app
