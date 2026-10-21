# Event catalogue

Envelope for every Kafka message:

```json
{"event_id": "uuid", "event_type": "order.created", "version": 1,
 "occurred_at": "ISO-8601", "correlation_id": "request id", "payload": {}}
```

| Topic | Producer | Consumers |
|---|---|---|
| user.registered | auth | user, notification |
| product.created / product.updated | product | inventory, cart |
| cart.checked_out | cart | order |
| order.created | order | inventory |
| inventory.reserved / inventory.failed | inventory | order |
| order.payment_requested | order | payment |
| order.refund_requested | order | payment |
| order.shipped / order.delivered | order | notification |
| payment.completed / payment.failed | payment | order, notification |
| order.confirmed / order.cancelled | order | inventory, notification |
| inventory.released | inventory | order (for expired holds) |

`product.created` / `product.updated` payload (a full snapshot): `{product_id, sku, name, price_cents, currency, category_id, is_active}`.
Deleting a product publishes `product.updated` with `is_active: false`.

### Checkout request from the cart to the order service
`cart.checked_out` (key = user_id) payload:
`{checkout_id, user_id, address_id | null, shipping_address | null, currency, total_cents, items: [{product_id, sku, name, quantity, unit_price_cents}]}`.
`shipping_address` is a copy of the chosen address (`recipient, line1, line2, city, state, postal_code, country`) taken at checkout.
The cart has no database, so this is published directly (no outbox): if Kafka is down the checkout request fails
and the cart is kept. `checkout_id` is derived from the cart's contents, so a retry of an unchanged cart sends the
SAME id. **The order service must treat `checkout_id` as an idempotency key** (create at most one order per id).
Prices are the current ones at checkout time; stock was confirmed with the inventory service just before.

### Payment service
Consumes `order.payment_requested` (`{order_id, user_id, total_cents, currency, email}`) and `order.refund_requested`.
Publishes (key = order_id): `payment.completed` `{order_id, payment_id, amount_cents, currency, channel, reference}`,
`payment.failed` `{order_id, payment_id, amount_cents, currency, reason}` and `payment.refunded`
`{order_id, payment_id, amount_cents, currency, refund_id, refund_status}`. Only GHS is accepted by default.
A payment is only reported complete after the provider itself has confirmed the amount and currency.

### Order service workflow
`pending -> awaiting_payment -> confirmed -> shipped -> delivered`, and `cancelled` (only before payment).
Every move is recorded in `order_status_history`. Events the order service **publishes** (key = order_id), all with
`{order_id, user_id, status, currency, total_cents}` plus the extras shown:

| Event | When | Extras |
|---|---|---|
| `order.created` | checkout accepted | `items: [{product_id, quantity, unit_price_cents}]` |
| `order.payment_requested` | stock reserved | (charge `total_cents` in `currency`) |
| `order.confirmed` | payment received | |
| `order.cancelled` | stock failed, payment failed, timeout or customer/admin cancel | `reason` |
| `order.refund_requested` | payment arrived for an order that was already cancelled | `reason` |
| `order.shipped` / `order.delivered` | admin actions | |

Events it **consumes**: `cart.checked_out`, `inventory.reserved|failed|released`, `payment.completed|failed`
(payload needs `order_id`; `reason` is optional). Late, duplicate and out-of-order messages are normal and are ignored
safely: only the moves in the status table are allowed. Orders that get no answer are cancelled by a sweeper
(10 min waiting for stock, 30 min waiting for payment). `cart.checked_out` is deduplicated on `checkout_id`.

### Checkout contract between the order and inventory services
The order service is the single authority on what happens to an order; inventory only reacts to it.

| Event | Payload | Inventory does |
|---|---|---|
| `order.created` | `{order_id, items: [{product_id, quantity}]}` (key = order_id) | Reserves all lines or none |
| `inventory.reserved` | `{order_id, items, expires_at}` | (sent) stock is held until `expires_at` |
| `inventory.failed` | `{order_id, reason, shortages: [{product_id, requested, available}]}` | (sent) nothing was held |
| `order.confirmed` | `{order_id}` | Order is paid: the held units leave stock permanently |
| `order.cancelled` | `{order_id}` | Gives the held units back (also used for failed payments) |
| `inventory.released` | `{order_id, reason: "cancelled" \| "expired"}` | (sent) the hold was released |

Notes: events about different topics are not ordered, so inventory tolerates `order.cancelled`
arriving before `order.created`, and `order.confirmed` arriving after the hold expired (it re-takes
the stock if still available, otherwise sends `inventory.failed`). Holds expire after 15 minutes.

`user.registered` payload: `{user_id, email, full_name, role}`.

Consumers are idempotent: they record `event_id` in a `processed_events` table in the same transaction as
their own changes and skip duplicates. Handlers run up to 3 times with backoff.

Failed messages go to `<topic>.dlq` after retries. Topics are created by `infra/kafka/create-topics.sh`.
