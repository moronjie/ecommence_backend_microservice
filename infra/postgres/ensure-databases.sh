#!/bin/sh
# Creates any database that does not exist yet (one per service). Safe to run on every start.
# Unlike a Postgres init script, which only runs when the data volume is first created, this also
# picks up databases for services added later.
set -e
for db in auth_db user_db product_db inventory_db order_db payment_db cart_db notification_db; do
  exists=$(psql -h "$PGHOST" -U "$PGUSER" -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='$db'")
  if [ "$exists" = "1" ]; then
    echo "database $db: already exists"
  else
    psql -h "$PGHOST" -U "$PGUSER" -d postgres -c "CREATE DATABASE $db"
    echo "database $db: created"
  fi
done
