#!/bin/bash
# Creates the topic catalogue (see docs/events.md). Safe to re-run.
set -e
BOOTSTRAP="${BOOTSTRAP:-localhost:9092}"
TOPICS=(
  user.registered
  product.created product.updated
  inventory.reserved inventory.failed inventory.released
  order.created order.confirmed order.cancelled
  payment.completed payment.failed
  notification.dlq
)
for t in "${TOPICS[@]}"; do
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
    --create --if-not-exists --topic "$t" --partitions 3 --replication-factor 1
done
