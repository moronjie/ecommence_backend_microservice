# Architecture decision records

A short note for each significant choice: what we faced, what we decided, and what it costs us. They explain
*why* the code looks the way it does, so a future change does not undo a decision without knowing the reason.

| # | Decision |
|---|---|
| [0001](0001-database-per-service.md) | Each service owns its own database |
| [0002](0002-events-and-transactional-outbox.md) | Services cooperate through events, sent via a transactional outbox |
| [0003](0003-order-service-coordinates-checkout.md) | The order service is the single authority on an order |
| [0004](0004-money-as-integer-minor-units.md) | Money is stored as whole minor units (pesewas), never decimals |
| [0005](0005-signed-identity-between-services.md) | The gateway signs the caller's identity; services verify it |
| [0006](0006-asymmetric-jwt-signing.md) | Login tokens use RS256 so only auth can sign |
| [0007](0007-hosted-checkout-with-paystack.md) | Paystack's hosted page takes the payment; we never see card details |
| [0008](0008-fail-open-when-redis-is-down.md) | Redis outages degrade protections instead of taking the shop down |

Format: **Context** (the problem), **Decision**, **Consequences** (including what we gave up).
