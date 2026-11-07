#!/usr/bin/env bash
# Start Airflow with a predictable admin user instead of the random password
# `airflow standalone` prints into the logs, and keep scheduler + webserver in
# one container (fine for a single-machine demo, not for production).
set -euo pipefail

airflow db migrate

airflow users create \
    --username "${AIRFLOW_ADMIN_USER:-admin}" \
    --password "${AIRFLOW_ADMIN_PASSWORD:-admin}" \
    --firstname Admin \
    --lastname User \
    --role Admin \
    --email admin@example.com || true

airflow scheduler &
exec airflow webserver
