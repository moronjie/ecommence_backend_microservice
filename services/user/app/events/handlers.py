"""Reactions to events from other services."""

import logging
import uuid
from typing import Any

from ecom_common.consumer import Handler
from ecom_common.inbox import first_time
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.profile import Profile

logger = logging.getLogger(__name__)


def build_handlers(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Handler]:
    async def on_user_registered(event: dict[str, Any]) -> None:
        payload = event["payload"]
        async with session_factory() as session:
            # Skip if this exact event was already handled (Kafka may deliver twice)
            if not await first_time(session, event["event_id"]):
                return
            user_id = payload["user_id"]
            # ...and skip if the profile exists anyway (e.g. a replayed, re-issued event)
            if await session.get(Profile, uuid.UUID(user_id)) is None:
                session.add(
                    Profile(
                        user_id=uuid.UUID(user_id),
                        email=payload["email"],
                        full_name=payload.get("full_name", ""),
                    )
                )
            await session.commit()  # profile + "processed" marker succeed or fail together
            logger.info("Profile ready for user %s", user_id)

    return {"user.registered": on_user_registered}
