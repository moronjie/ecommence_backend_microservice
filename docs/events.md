# Event catalogue

Envelope for every Kafka message:

```json
{"event_id": "uuid", "event_type": "order.created", "version": 1,
 "occurred_at": "ISO-8601", "correlation_id": "request id", "payload": {}}
```

| Topic | Producer | Consumers |
|---|---|---|
| user.registered | auth | user, notification |
| product.created / product.updated | product | inventory (created), cache invalidation |
| order.created | order | inventory |
| inventory.reserved / inventory.failed | inventory | order, payment |
| payment.completed / payment.failed | payment | order, inventory (release), notification |
| order.confirmed / order.cancelled | order | notification |

`user.registered` payload: `{user_id, email, full_name, role}`.

Consumers are idempotent: they record `event_id` in a `processed_events` table in the same transaction as
their own changes and skip duplicates. Handlers run up to 3 times with backoff.

Failed messages go to `<topic>.dlq` after retries. Topics are created by `infra/kafka/create-topics.sh`.
