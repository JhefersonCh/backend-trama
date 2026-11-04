#!/bin/sh
# One-shot migration runner (compose service `migrate`, postgres image).
# Applies database/migrations/*.sql then database/seeds/*.sql not yet recorded in schema_migrations.
# Each file runs in ONE transaction together with its tracking INSERT (psql -1, ON_ERROR_STOP).
set -eu

DIR="${TRAMA_DB_DIR:-/trama-db}"
export PGHOST="${PGHOST:-db}" PGUSER="${POSTGRES_USER:-trama}" PGDATABASE="${POSTGRES_DB:-trama}" PGPASSWORD="${POSTGRES_PASSWORD:-trama}"
export PGOPTIONS="-c client_min_messages=warning"
PSQL="psql -X -q -v ON_ERROR_STOP=1"

# Files that were applied by the old initdb wrapper, before tracking existed.
LEGACY="001_extensions.sql 002_config.sql 003_operational.sql 004_knowledge.sql 005_conversation_audit.sql 900_seed_config.sql 910_seed_lab.sql"

$PSQL -c "CREATE TABLE IF NOT EXISTS schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"

count=$($PSQL -At -c "SELECT count(*) FROM schema_migrations")
has_schema=$($PSQL -At -c "SELECT to_regclass('public.model_providers') IS NOT NULL")
if [ "$count" = "0" ] && [ "$has_schema" = "t" ]; then
  echo ">> existing DB without tracking: baselining legacy files"
  for f in $LEGACY; do
    $PSQL -c "INSERT INTO schema_migrations (filename) VALUES ('$f') ON CONFLICT DO NOTHING"
  done
fi

for sub in migrations seeds; do
  for f in "$DIR/$sub"/*.sql; do
    [ -e "$f" ] || continue
    name=$(basename "$f")
    done_already=$($PSQL -At -c "SELECT 1 FROM schema_migrations WHERE filename = '$name'")
    if [ "$done_already" = "1" ]; then
      echo "-- skip  $sub/$name"
      continue
    fi
    echo ">> apply $sub/$name"
    $PSQL -1 -f "$f" -c "INSERT INTO schema_migrations (filename) VALUES ('$name')"
  done
done
echo "migrate: done"
