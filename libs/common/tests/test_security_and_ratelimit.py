from datetime import timedelta

import pytest
from ecom_common import Unauthorized
from ecom_common.redis import SlidingWindowRateLimiter
from ecom_common.security import REFRESH, create_token, decode_token
from fakeredis import FakeAsyncRedis

SECRET = "test-secret"


def test_token_round_trip():
    token = create_token("user-1", secret=SECRET, role="admin")
    claims = decode_token(token, secret=SECRET)
    assert claims["sub"] == "user-1" and claims["role"] == "admin"


def test_expired_token_rejected():
    token = create_token("u", secret=SECRET, expires_in=timedelta(seconds=-5))
    with pytest.raises(Unauthorized):
        decode_token(token, secret=SECRET)


def test_wrong_secret_and_garbage_rejected():
    token = create_token("u", secret=SECRET)
    with pytest.raises(Unauthorized):
        decode_token(token, secret="other")
    with pytest.raises(Unauthorized):
        decode_token("not-a-jwt", secret=SECRET)


def test_refresh_token_not_accepted_as_access():
    token = create_token("u", secret=SECRET, token_type=REFRESH)
    with pytest.raises(Unauthorized):
        decode_token(token, secret=SECRET)


async def test_rate_limiter_blocks_after_limit():
    limiter = SlidingWindowRateLimiter(FakeAsyncRedis(decode_responses=True))
    results = [await limiter.hit("ip:1", limit=3, window=60) for _ in range(5)]
    assert [r.allowed for r in results] == [True, True, True, False, False]
    assert results[0].remaining == 2
    assert results[3].retry_after >= 1


async def test_rate_limiter_keys_are_independent():
    limiter = SlidingWindowRateLimiter(FakeAsyncRedis(decode_responses=True))
    await limiter.hit("a", limit=1, window=60)
    assert (await limiter.hit("a", limit=1, window=60)).allowed is False
    assert (await limiter.hit("b", limit=1, window=60)).allowed is True
