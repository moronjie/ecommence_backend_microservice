#!/bin/bash
# Creates the topic catalogue (see docs/events.md). Safe to re-run.
set -e
BOOTSTRAP="${BOOTSTRAP:-localhost:9092}"
TOPICS=(
  user.registered
  product.created product.updated
  inventory.reserved inventory.failed inventory.released
  order.created order.confirmed order.cancelled
  order.payment_requested order.refund_requested order.shipped order.delivered
  inventory.reserved.dlq inventory.failed.dlq inventory.released.dlq
  payment.completed.dlq payment.failed.dlq
  payment.completed payment.failed
  cart.checked_out cart.checked_out.dlq
  user.registered.dlq
  product.created.dlq product.updated.dlq
  order.created.dlq order.confirmed.dlq order.cancelled.dlq
  notification.dlq
)
for t in "${TOPICS[@]}"; do
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
    --create --if-not-exists --topic "$t" --partitions 3 --replication-factor 1
done
