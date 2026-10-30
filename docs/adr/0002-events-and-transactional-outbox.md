# 0002. Services cooperate through events, sent via a transactional outbox

## Context
Placing an order touches the cart, order, inventory, payment and notification services. Calling them one after another
over HTTP makes the shopper wait for all of them, and one slow or crashed service breaks the whole request. A
service also has to do two things at once when something happens: save its own change **and** tell the others. If it
saves and then crashes before telling, the others never find out. If it tells and then fails to save, they were told
something that did not happen.

## Decision
1. Services announce facts as **events on Kafka** (`order.created`, `payment.completed`) and react to each other's.
2. A service never publishes directly. It writes the event into an **outbox table in the same database transaction**
   as its change. A background relay sends unsent rows to Kafka afterwards. Either both the change and its event
   exist, or neither does.
3. Delivery is **at least once**, so every consumer is **idempotent**: it records each event id in the same transaction
   as its own work and skips ids it has seen.
4. A handler that keeps failing is retried three times, then the message goes to `<topic>.dlq` so it cannot block
   the topic behind it.

## Consequences
* **Good:** no event is lost or invented; a service that is down simply catches up later; the shopper's request
  finishes quickly; crashes and redeliveries are harmless.
* **Cost:** results appear a moment later (eventual consistency). Tests and clients have to wait for them.
* **Cost:** events can arrive late, twice or out of order across topics. Every handler decides what a stale message
  means (the order service ignores moves that no longer apply; inventory handles `order.cancelled` arriving before
  `order.created`).
* **Cost:** more moving parts (a relay per service, a dead-letter topic someone must watch; the Grafana dashboard
  shows dead letters for that reason).
* **Exception:** the cart has no database, so it cannot use an outbox. It publishes `cart.checked_out` directly and fails
  the request (keeping the cart) if Kafka is down; a deterministic checkout id makes the retry safe.
