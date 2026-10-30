# Load test results

How to run it: `make loadtest` (the stack must be up, started with `RATE_LIMIT_REQUESTS=1000000`; otherwise you
measure the rate limiter). It runs 20 simulated shoppers for 30 seconds: 60% browsing, 30% cart, 10% checkout.

## What was measured (and where)

**Machine:** a Windows laptop running everything in Docker Desktop, with a separate PostgreSQL and other programs busy
at the same time. Docker Desktop on Windows is considerably slower than Linux, and this laptop is a noisy place to
measure, so read these as "what this setup does", **not** what production hardware would do.

| Run | Requests/s | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|
| First run (INFO logging, 3 log lines per request) | 71 | 169 ms | 414 ms | 763 ms | 1 of 2160 (a client timeout, see below) |
| Final build (duplicate logging removed) | 51 | 306 ms | 556 ms | 680 ms | 0 of 1571 |

The two runs differ by more than the change between them could explain, and repeated benchmarks of identical settings
swung by a similar amount. **The honest summary is a range: roughly 50 to 70 mixed requests per second, p95 under
0.7 seconds, no server errors.** Do not read the second run as "the final build is slower".

## What the numbers say

* **Server-side the services are fast.** Prometheus recorded the median time inside each service during the test:
  product 8 ms, user 14 ms, inventory 18 ms, order 28 ms, cart 45 ms, gateway 182 ms. (Auth is slower, 280 ms, by
  design: it hashes passwords with argon2, which is deliberately expensive, and the test registers and logs in 20 customers.)
* **The gateway's 182 ms is queueing, not work.** One request at a time costs the gateway only about 6 ms more than going
  straight to a service (12 ms against 6 ms). With 20 shoppers at once each single Python process is saturated at roughly
  85 requests per second (about 100 to 130 for a service doing less work), so requests wait their turn. All the signing,
  token checking and Redis calls together cost under 1 ms per request; they are not the bottleneck.
* **No server-side failures.** Prometheus showed zero 5xx responses and no request over 5 seconds across the runs.
* **One unexplained client timeout** (30 seconds, in the first run). The servers recorded nothing slow, so the stall was
  between the test program and Docker's networking. It did not happen in the two later runs.

## What to do with this

* Capacity is per **process**. To serve more traffic, run more copies of the busy services (the gateway first) behind a load
  balancer, and run them on Linux. Everything shared lives in Redis or Postgres, so more copies need no code changes. Two
  things to plan for: Prometheus needs to scrape each copy (static single-name targets assume one), and the gateway's
  circuit breaker is per process.
* Quieter logging helps under load. `LOG_LEVEL=WARNING` removes the per-request access line. Duplicate request logging
  (uvicorn's own and the `httpx` client's) was removed outright.
* Re-measure on the hardware you will deploy to before promising anyone a number.
