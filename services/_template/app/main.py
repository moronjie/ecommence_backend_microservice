from ecom_common.app_factory import create_service_app

from app.api.v1 import demo
from app.core.config import settings


def create_app():
    app = create_service_app(settings)
    app.include_router(demo.router)
    return app


app = create_app()
