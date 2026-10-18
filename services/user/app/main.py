import logging
from contextlib import asynccontextmanager

from ecom_common.app_factory import create_service_app
from ecom_common.consumer import KafkaConsumerRunner
from ecom_common.db import create_engine, create_session_factory
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import users
from app.core.config import Settings
from app.core.config import settings as default_settings
from app.events.handlers import build_handlers

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings = default_settings,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    """The session factory can be injected (tests); otherwise the lifespan builds a real one."""

    async def db_ready() -> None:
        async with app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cleanup = []
        if app.state.session_factory is None:
            engine = create_engine(settings.database_url)
            app.state.session_factory = create_session_factory(engine)
            cleanup.append(engine.dispose)

        if settings.consumer_enabled:
            runner = KafkaConsumerRunner(
                settings.kafka_bootstrap_servers,
                settings.consumer_group,
                topics=["user.registered"],
                handlers=build_handlers(app.state.session_factory),
            )
            runner.start()  # reconnects by itself if Kafka is not up yet
            cleanup.append(runner.stop)

        yield
        for close in reversed(cleanup):
            await close()

    app = create_service_app(settings, lifespan=lifespan, readiness_checks={"db": db_ready})
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.include_router(users.router)
    return app


app = create_app()
