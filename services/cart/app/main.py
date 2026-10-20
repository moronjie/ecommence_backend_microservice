import logging
from contextlib import asynccontextmanager

import httpx
from ecom_common.app_factory import create_service_app
from ecom_common.consumer import KafkaConsumerRunner
from ecom_common.kafka import EventPublisher
from ecom_common.redis import create_redis
from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.v1 import cart
from app.core.config import Settings
from app.core.config import settings as default_settings
from app.events.handlers import TOPICS, build_handlers
from app.events.publisher import LazyKafkaPublisher
from app.stores import ProductStore

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings = default_settings,
    *,
    redis_client: Redis | None = None,
    http_client: httpx.AsyncClient | None = None,
    publisher: EventPublisher | None = None,
) -> FastAPI:
    """Dependencies can be injected (tests); otherwise the lifespan builds real ones."""

    async def redis_ready() -> None:
        await app.state.redis.ping()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cleanup = []
        if app.state.redis is None:
            app.state.redis = create_redis(settings.redis_url)
            cleanup.append(app.state.redis.aclose)
        if app.state.http is None:
            app.state.http = httpx.AsyncClient(timeout=settings.http_timeout_seconds)
            cleanup.append(app.state.http.aclose)
        if app.state.publisher is None:
            app.state.publisher = LazyKafkaPublisher(settings.kafka_bootstrap_servers)
            cleanup.append(app.state.publisher.stop)

        if settings.consumer_enabled:
            runner = KafkaConsumerRunner(
                settings.kafka_bootstrap_servers,
                settings.consumer_group,
                topics=TOPICS,
                handlers=build_handlers(ProductStore(app.state.redis)),
            )
            runner.start()  # reconnects by itself if Kafka is not up yet
            cleanup.append(runner.stop)

        yield
        for close in reversed(cleanup):
            await close()

    app = create_service_app(settings, lifespan=lifespan, readiness_checks={"redis": redis_ready})
    app.state.settings = settings
    app.state.redis = redis_client
    app.state.http = http_client
    app.state.publisher = publisher
    app.include_router(cart.router)
    return app


app = create_app()
