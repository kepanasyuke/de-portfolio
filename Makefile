SHELL := /bin/bash
COMPOSE := docker compose
PY := python

PACKAGES := org.apache.hadoop:hadoop-aws:3.3.4,\
com.amazonaws:aws-java-sdk-bundle:1.12.262,\
org.postgresql:postgresql:42.7.3,\
org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1

.PHONY: help up down ps logs jars landing stream dq test lint fmt psql ch showcase game clean

help:
	@echo "up        - поднять весь стек (postgres, kafka, minio, spark, airflow, clickhouse, prometheus, grafana)"
	@echo "down      - остановить стек"
	@echo "jars      - один раз скачать jar-зависимости Spark в общий ivy-кэш"
	@echo "landing   - сгенерировать синтетический landing-слой (CSV) локально"
	@echo "stream    - запустить Kafka -> Spark streaming джобу в контейнере"
	@echo "dq        - прогнать data-quality проверки против локального DWH"
	@echo "test      - pytest по всем проектам"
	@echo "lint      - контракт стиля (один выход, без continue, вложенность ≤ 2) + ruff"
	@echo "psql      - psql внутрь контейнера postgres"
	@echo "showcase  - открыть docs/index.html в браузере"
	@echo "game      - открыть игру 06-pipeline-game/index.html"

up:
	$(COMPOSE) --env-file .env up -d --build

down:
	$(COMPOSE) --env-file .env down -v

ps:
	$(COMPOSE) --env-file .env ps

logs:
	$(COMPOSE) --env-file .env logs -f airflow

jars:
	$(COMPOSE) --env-file .env exec airflow spark-submit --packages $(PACKAGES) /opt/project/scripts/warm_jars.py

landing:
	$(PY) -m ingestion.generate_landing --out data/landing --date $(shell date +%Y-%m-%d) --orders 800
	$(PY) -m ingestion.upload_landing --dir data/landing --date $(shell date +%Y-%m-%d)

stream:
	$(COMPOSE) --env-file .env exec airflow bash /opt/project/scripts/run_stream.sh

dq:
	PYTHONPATH=03-dq-framework $(PY) -m dq.run --rules 03-dq-framework/dq/rules.yml \
		--dsn "$${DWH_DSN_LOCAL:-postgresql://dwh:dwh@localhost:5432/dwh}"

parity:
	PYTHONPATH=05-migration-parity $(PY) -m parity.cli --spec 05-migration-parity/specs/orders.json \
		--source-dialect postgres --source-dsn "$${DWH_DSN_LOCAL:-postgresql://dwh:dwh@localhost:5432/dwh}" \
		--target-dialect clickhouse --target-dsn "http://localhost:8123/marts" --buckets 8

bench:
	$(PY) 04-clickhouse-marts/bench/benchmark.py --runs 5 --rows 200000

test:
	$(PY) -m pytest

lint:
	$(PY) scripts/check_style.py .
	$(PY) -m ruff check .

fmt:
	$(PY) -m ruff format .

psql:
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-dwh} -d $${POSTGRES_DB:-dwh}

showcase:
	$(PY) -c "import webbrowser, pathlib; webbrowser.open(pathlib.Path('docs/index.html').resolve().as_uri())"

game:
	$(PY) -c "import webbrowser, pathlib; webbrowser.open(pathlib.Path('06-pipeline-game/index.html').resolve().as_uri())"

clean:
	rm -rf data .pytest_cache .ruff_cache
