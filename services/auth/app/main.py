import logging
from contextlib import asynccontextmanager

from ecom_common.app_factory import create_service_app
from ecom_common.db import create_engine, create_session_factory
from ecom_common.kafka import EventPublisher, KafkaPublisher
from ecom_common.outbox import OutboxRelay
from ecom_common.redis import create_redis
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import auth
from app.core.config import Settings
from app.core.config import settings as default_settings
from app.services.auth_service import AuthService

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings = default_settings,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    redis_client: Redis | None = None,
    publisher: EventPublisher | None = None,
) -> FastAPI:
    """Dependencies can be injected (tests); otherwise the lifespan builds real ones."""

    async def db_ready() -> None:
        async with app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))

    async def redis_ready() -> None:
        await app.state.redis.ping()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cleanup = []
        if app.state.session_factory is None:
            engine = create_engine(settings.database_url)
            app.state.session_factory = create_session_factory(engine)
            cleanup.append(engine.dispose)
        if app.state.redis is None:
            app.state.redis = create_redis(settings.redis_url)
            cleanup.append(app.state.redis.aclose)

        if settings.admin_email and settings.admin_password:
            async with app.state.session_factory() as session:
                await AuthService(session, app.state.redis, settings).ensure_admin(
                    settings.admin_email, settings.admin_password
                )

        if settings.outbox_relay_enabled:
            kafka = app.state.publisher
            try:
                if kafka is None:
                    kafka = KafkaPublisher(settings.kafka_bootstrap_servers)
                    await kafka.start()
                    cleanup.append(kafka.stop)
                relay = OutboxRelay(app.state.session_factory, kafka)
                relay.start()
                cleanup.append(relay.stop)
            except Exception:  # noqa: BLE001 - events stay safely in the outbox until Kafka is back
                logger.exception("Kafka unavailable; events will queue in the outbox")

        yield
        for close in reversed(cleanup):
            await close()

    app = create_service_app(
        settings, lifespan=lifespan, readiness_checks={"db": db_ready, "redis": redis_ready}
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.redis = redis_client
    app.state.publisher = publisher
    app.include_router(auth.router)
    return app


app = create_app()
