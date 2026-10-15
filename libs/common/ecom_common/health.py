from collections.abc import Awaitable, Callable

from fastapi import APIRouter

from ecom_common.responses import error_response, success_response

ReadinessCheck = Callable[[], Awaitable[None]]


def build_health_router(
    service_name: str, checks: dict[str, ReadinessCheck] | None = None
) -> APIRouter:
    router = APIRouter(tags=["health"])
    checks = checks or {}

    @router.get("/health")
    async def health():
        return success_response({"service": service_name, "status": "up"}, "Service is alive")

    @router.get("/ready")
    async def ready():
        failures: dict[str, str] = {}
        for name, check in checks.items():
            try:
                await check()
            except Exception as exc:  # noqa: BLE001 - report any dependency failure
                failures[name] = str(exc)
        if failures:
            return error_response(
                "Service not ready", ", ".join(failures), 503, {"failed": failures}
            )
        return success_response(
            {"service": service_name, "checks": list(checks)}, "Service is ready"
        )

    return router
