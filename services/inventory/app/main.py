import logging
from contextlib import asynccontextmanager

from ecom_common.app_factory import create_service_app
from ecom_common.consumer import KafkaConsumerRunner
from ecom_common.db import create_engine, create_session_factory
from ecom_common.kafka import EventPublisher, KafkaPublisher
from ecom_common.outbox import OutboxRelay
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import inventory
from app.core.config import Settings
from app.core.config import settings as default_settings
from app.events.handlers import TOPICS, build_handlers
from app.tasks.expiry import ExpiryLoop

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings = default_settings,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    publisher: EventPublisher | None = None,
) -> FastAPI:
    """Dependencies can be injected (tests); otherwise the lifespan builds real ones."""

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
        factory = app.state.session_factory

        expiry = ExpiryLoop(factory, settings)
        expiry.start()
        cleanup.append(expiry.stop)

        if settings.consumer_enabled:
            runner = KafkaConsumerRunner(
                settings.kafka_bootstrap_servers,
                settings.consumer_group,
                topics=TOPICS,
                handlers=build_handlers(factory, settings),
            )
            runner.start()  # reconnects by itself if Kafka is not up yet
            cleanup.append(runner.stop)

        if settings.outbox_relay_enabled:
            kafka = app.state.publisher
            try:
                if kafka is None:
                    kafka = KafkaPublisher(settings.kafka_bootstrap_servers)
                    await kafka.start()
                    cleanup.append(kafka.stop)
                relay = OutboxRelay(factory, kafka)
                relay.start()
                cleanup.append(relay.stop)
            except Exception:  # noqa: BLE001 - events stay safely in the outbox until Kafka is back
                logger.exception("Kafka unavailable; events will queue in the outbox")

        yield
        for close in reversed(cleanup):
            await close()

    app = create_service_app(settings, lifespan=lifespan, readiness_checks={"db": db_ready})
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.publisher = publisher
    app.include_router(inventory.router)
    return app


app = create_app()
