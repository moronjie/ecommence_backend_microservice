# 0001. Each service owns its own database

## Context
Eight services need to store data. The easy option is one shared database that everyone reads and writes.
That couples them: a change to one table can break three services, services cannot be deployed or scaled
alone, and nobody can say which service is responsible for which data.

## Decision
Every service has its **own database** (`auth_db`, `order_db`, ...) and is the only thing that touches it. In
development they are separate databases inside one Postgres server; in production each could move to its own server
without any code change. A service that needs another's data either asks it over HTTP or keeps a **copy fed by
events** (the cart's copy of product prices).

## Consequences
* **Good:** services change, migrate and scale independently; ownership is unambiguous; a bug in one service cannot
  corrupt another's data.
* **Cost:** no joins across services and no transaction spanning two of them. "Place an order and reserve stock" becomes
  a sequence of steps coordinated by events (ADR 0002 and 0003) instead of one database transaction.
* **Cost:** copies of data can be briefly out of date (a new user's profile appears a moment after sign-up). The code
  handles this explicitly, for example with a clear "your profile is still being set up" answer.
* **Cost:** a new service needs a new database. A job in `docker-compose.yml` (`db-init`) creates any that are
  missing on every start, because Postgres only runs its own init scripts on a brand-new volume.
