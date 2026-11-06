#!/bin/bash
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -f /opt/project/01-batch-dwh/sql/01_schema.sql \
    -f /opt/project/01-batch-dwh/sql/03_dm_schema.sql \
    -f /opt/project/02-streaming-kafka/sql/06_events_schema.sql

echo "dwh schemas: stg, dds, dm, meta are ready"
