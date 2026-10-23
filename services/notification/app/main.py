import logging
from contextlib import asynccontextmanager

from ecom_common.app_factory import create_service_app
from ecom_common.consumer import KafkaConsumerRunner
from ecom_common.db import create_engine, create_session_factory
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import notifications
from app.core.config import Settings
from app.core.config import settings as default_settings
from app.events.handlers import TOPICS, build_handlers
from app.senders.base import EmailSender
from app.senders.console import ConsoleSender
from app.senders.smtp import SmtpSender
from app.tasks.worker import EmailWorker

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings = default_settings,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    sender: EmailSender | None = None,
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

        email_sender = app.state.sender
        if email_sender is None:
            if settings.email_provider == "smtp":
                email_sender = SmtpSender(
                    settings.smtp_host,
                    settings.smtp_port,
                    username=settings.smtp_username,
                    password=settings.smtp_password,
                    starttls=settings.smtp_starttls,
                    timeout=settings.smtp_timeout_seconds,
                )
            else:
                logger.warning("EMAIL_PROVIDER=console: emails are only written to the log")
                email_sender = ConsoleSender()
            app.state.sender = email_sender

        worker = EmailWorker(factory, email_sender, settings)
        worker.start()
        cleanup.append(worker.stop)

        if settings.consumer_enabled:
            runner = KafkaConsumerRunner(
                settings.kafka_bootstrap_servers,
                settings.consumer_group,
                topics=TOPICS,
                handlers=build_handlers(factory, settings),
            )
            runner.start()  # reconnects by itself if Kafka is not up yet
            cleanup.append(runner.stop)

        yield
        for close in reversed(cleanup):
            await close()

    app = create_service_app(settings, lifespan=lifespan, readiness_checks={"db": db_ready})
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.sender = sender
    app.include_router(notifications.router)
    return app


app = create_app()
