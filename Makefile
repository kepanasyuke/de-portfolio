# Portable Makefile: every recipe is a single `python script.py` or `docker compose` call,
# so the same targets work in PowerShell, Git Bash and WSL.
PY ?= python
COMPOSE ?= docker compose
PORT ?= 8000
CONSOLE_PORT ?= 8088
SOURCE_DSN ?= postgresql://dwh:dwh@localhost:5432/dwh
TARGET_DSN ?= http://localhost:8123/marts
DWH_DSN_LOCAL ?= postgresql://dwh:dwh@localhost:5432/dwh
BENCH_ROWS ?= 200000

# UTF-8 for every child process: otherwise Cyrillic output becomes cp866 mush on Windows.
export PYTHONUTF8 := 1

.DEFAULT_GOAL := help
.PHONY: help env compose-check doctor up up-core down ps logs landing landing-upload stream dq parity bench \
        test lint fmt site console serve tunnel psql showcase game clean

help:
	@$(PY) scripts/help.py --port $(PORT)

env:
	@$(PY) scripts/ensure_env.py

compose-check:
	@$(PY) scripts/check_compose.py

doctor:
	@$(PY) scripts/doctor.py

up: env compose-check
	$(COMPOSE) --env-file .env up -d --build
	@$(PY) scripts/doctor.py

# Light subset for machines where the full stack does not fit on disk (~6 GB of images).
up-core: env compose-check
	$(COMPOSE) --env-file .env up -d postgres minio minio-init kafka
	@$(PY) scripts/doctor.py

down:
	$(COMPOSE) --env-file .env down -v

ps:
	$(COMPOSE) --env-file .env ps

logs:
	$(COMPOSE) --env-file .env logs -f airflow

landing:
	$(PY) scripts/landing.py

landing-upload:
	$(PY) scripts/landing.py --upload

stream:
	$(COMPOSE) --env-file .env exec airflow bash /opt/project/scripts/run_stream.sh

dq:
	$(PY) scripts/run_dq.py --rules 03-dq-framework/dq/rules.yml --dsn "$(DWH_DSN_LOCAL)"

parity:
	$(PY) scripts/run_parity.py --spec 05-migration-parity/specs/orders.json \
		--source-dialect postgres --source-dsn "$(SOURCE_DSN)" \
		--target-dialect clickhouse --target-dsn "$(TARGET_DSN)" --buckets 8

bench:
	$(PY) 04-clickhouse-marts/bench/benchmark.py --runs 5 --rows $(BENCH_ROWS)

test:
	$(PY) -m pytest

lint:
	$(PY) scripts/check_style.py .
	$(PY) -m ruff check .

fmt:
	$(PY) -m ruff format .

site:
	$(PY) scripts/build_site.py --out site
	@echo "Сайт: http://localhost:$(PORT)/  (Ctrl+C — остановить)"
	$(PY) -m http.server $(PORT) --directory site

console:
	@echo "Консоль: http://localhost:$(CONSOLE_PORT)/  (Ctrl+C — остановить)"
	$(PY) scripts/run_console.py --port $(CONSOLE_PORT)

serve:
	@echo "Сайт: http://localhost:$(PORT)/  (Ctrl+C — остановить)"
	$(PY) -m http.server $(PORT) --directory site

tunnel:
	cloudflared tunnel --url http://localhost:$(PORT) --no-autoupdate

psql:
	$(COMPOSE) --env-file .env exec postgres psql -U dwh -d dwh

showcase:
	$(PY) scripts/open_page.py docs/index.html

game:
	$(PY) scripts/open_page.py 06-pipeline-game/index.html

clean:
	$(PY) scripts/clean.py
