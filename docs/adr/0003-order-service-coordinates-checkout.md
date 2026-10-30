# 0003. The order service is the single authority on an order

## Context
A purchase involves stock, payment and notifications, and each can succeed or fail independently. If every service
reacted to every other service's events directly (inventory cancelling on payment failure, payment cancelling on
stock failure...) nobody could say what state an order was in, and a missing or reordered event would leave things
half done: money taken with no order, or stock held for an order that no longer exists.

## Decision
The **order service owns the order's status** and is the only one that decides it. Other services report facts and
react to the order's decisions:

* inventory says `inventory.reserved` or `inventory.failed`; it acts on `order.confirmed` and `order.cancelled`
  (never on `payment.failed` directly);
* payment says `payment.completed` or `payment.failed`; it acts on `order.payment_requested` and
  `order.refund_requested`;
* the order service turns those facts into one status, one history, and the next event.

Only the moves in a fixed table are allowed (`pending -> awaiting_payment -> confirmed -> shipped -> delivered`,
and `cancelled` before payment). A message that asks for any other move is ignored as stale.

## Consequences
* **Good:** one place answers "what happened to my order and why" (the status history). Out-of-order and duplicate
  messages are handled in one table instead of in every service. A payment arriving after a cancellation is noticed
  and refunded.
* **Cost:** the order service is on the critical path of every purchase and knows the whole flow. It is a coordinator,
  not a pure observer of events.
* **Cost:** some timeouts need their own owner. The order service cancels orders stuck waiting for stock (10 minutes)
  or payment (30), and the inventory service releases unpaid reservations (15), so nothing waits forever.
