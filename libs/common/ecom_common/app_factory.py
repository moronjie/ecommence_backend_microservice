from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastapi import FastAPI

from ecom_common.config import BaseServiceSettings
from ecom_common.exceptions import register_exception_handlers
from ecom_common.health import ReadinessCheck, build_health_router
from ecom_common.logging import setup_logging
from ecom_common.middleware import RequestContextMiddleware


def create_service_app(
    settings: BaseServiceSettings,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[Any]] | None = None,
    readiness_checks: dict[str, ReadinessCheck] | None = None,
) -> FastAPI:
    setup_logging(settings.service_name, settings.log_level)
    app = FastAPI(
        title=settings.service_name,
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(build_health_router(settings.service_name, readiness_checks))
    return app
