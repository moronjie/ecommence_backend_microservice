# 0008. Redis outages degrade protections instead of taking the shop down

## Context
Redis holds several things: the product cache, shopping carts, the gateway's rate-limit counters, revoked login tokens and
the login lockout counters. When Redis is unreachable each user of it must choose: refuse requests ("fail closed") or carry on
without Redis's help ("fail open").

## Decision
Decide **per use**, by what failing costs:

| Use | When Redis is down | Why |
|---|---|---|
| Product cache | **Fail open**: read from the database | The cache is only a speed-up |
| Rate limiting | **Fail open**: allow, log a warning | A rate limiter outage should not become a shop outage |
| Revoked-token check | **Fail open**: accept the token, log | Same; tokens still expire on their own within 15 minutes |
| Login lockout | **Fail open**: logins keep working | Customers can still sign in; the gateway's per-IP limit is also down, so brute force is briefly easier |
| Cart | **Cannot work** (it *is* the cart) | Cart endpoints return errors; the rest of the shop is unaffected |
| Refresh-token rotation | **Cannot work** | A refresh cannot be checked without it, so it fails rather than accept a possibly revoked token |

## Consequences
* **Good:** a Redis blip does not stop people browsing, buying or logging in; the failures that do happen are limited to
  the features that truly need it.
* **Cost:** during an outage the protections are weaker: limits are not enforced, logged-out tokens work until they expire, and
  password guessing is easier. Every fail-open path logs a warning, so the outage and the exposure are visible.
* **Cost:** if availability is worth less than strictness for your business (for example a bank), flip the sensitive paths to
  fail closed. Each is a small `except` block, and the table above says where.
