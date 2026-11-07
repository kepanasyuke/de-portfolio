# Docker на этой машине: что сделано и что осталось

Документ описывает два пути установки, бюджет диска и фактический статус
проверок. Всё, что помечено «проверено», запускалось здесь, а не переписано из
документации.

## Статус (проверено 2026-11-07)

| Проверка | Результат |
| --- | --- |
| `docker --version` | `29.9.2` (Docker Desktop, установлен через `winget`) |
| `docker compose version` | `v5.6.1` |
| `docker info` | демон отвечает, `os=linux`, 7.8 ГБ памяти |
| `make compose-check` | 11 сервисов, 0 проблем |
| `docker compose config` | OK (настоящая валидация схемы) |
| `make up-core` | postgres, minio, kafka — Up; postgres healthy |
| схемы в Postgres | `stg`, `dds`, `dm`, `meta` созданы |
| таблицы DDS | 6 таблиц на месте |
| MinIO | бакет `lake` создан (`minio-init` завершился) |
| `make dq` | гейт отработал: 11 passed, 2 failed на пустой витрине — корректно |
| `make site` | 200 на `/`, `/game/index.html`, `/assets/anim-hero.svg` |
| `make tunnel` | **не работает на этой сети** (см. ниже) |

## Путь 1: Docker Desktop (использован здесь)

```powershell
winget install --id Docker.DockerDesktop --exact
# установщик запросит UAC; после установки — вход в систему заново
```

Проверка:

```powershell
docker info --format "{{.ServerVersion}}"
docker compose version
cd data-engineering-portfolio
make doctor          # покажет, что готово
make up-core         # ядро: postgres + minio + kafka
```

Особенности:

- Съел **~5.8 ГБ** на диске C: (сам Docker Desktop плюс WSL-образ). Свободного
  места после установки осталось 3.1 ГБ — полному стеку этого мало.
- Демон поднимается через 10–20 секунд после запуска Docker Desktop.
- Без входа в систему Docker Desktop не стартует автоматически: `make doctor`
  покажет `docker daemon -> демон не отвечает`.

## Путь 2: WSL2 + Docker Engine (без Docker Desktop)

```powershell
wsl --install -d Ubuntu          # нужен администратор, затем перезагрузка
```

Внутри Ubuntu:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 make python3 python3-pip
sudo usermod -aG docker "$USER"   # затем выйти и войти в WSL заново
docker run --rm hello-world
```

Дальше проект запускается из WSL:

```bash
cd /mnt/c/Users/Admin/Documents/MultiTool/HomeChats/Chat-77/data-engineering-portfolio
make up-core
```

Этот путь не проверялся: `wsl --install` требует прав администратора и скачивания
образа Ubuntu (порядка 1 ГБ), а на диске оставалось 3.1 ГБ.

## Бюджет диска

| Набор | Образы | Команда |
| --- | --- | --- |
| Ядро: postgres, minio, mc, kafka | ~1 ГБ | `make up-core` |
| Аналитика: + clickhouse | ~2.5 ГБ | `make up-core` + `docker compose up -d clickhouse` |
| Полный стек: + spark, airflow, prometheus, grafana | ~6 ГБ | `make up` |

`make doctor` предупреждает, если свободно меньше 12 ГБ. Освободить место:

```powershell
docker system df            # что занимает
docker system prune -a      # удалит неиспользуемые образы и кэш
```

Другой вариант — перенести хранилище Docker на другой диск через
Settings → Resources → Disk image location в Docker Desktop.

## MinIO: официальные образы закрыты

Официальные `minio/minio` и `minio/mc` перестали тянуться анонимно:

```
denied: requested access to the resource is denied
unauthorized: authentication required
```

В compose используются публичные ребилды `pgsty/minio` и `pgsty/mc` — тот же S3
API и тот же `mc`, поэтому код пайплайна и документация не меняются. Если нужен
именно официальный образ, его надо тянуть из приватного registry с логином
(`docker login`) и заменить `image:` в compose.

## Туннель для сайта

```powershell
make site      # собирает site/ и отдаёт http://localhost:8000
make tunnel    # в другом терминале: cloudflared quick tunnel
```

На этой сети quick tunnel не создаётся:

```
failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel":
context deadline exceeded (Client.Timeout exceeded while awaiting headers)
```

При этом сам API доступен (`GET https://api.trycloudflare.com/` → 405, сертификат
подлинный, `1.1.1.1` → 200), то есть это не полная блокировка, а отказ именно на
создании безаккаунтного туннеля. Что остаётся:

1. **Именованный туннель с аккаунтом Cloudflare** — стабильный домен, но нужен
   `cloudflared tunnel login` и DNS-запись.
2. **ngrok** — `winget install ngrok`, затем `ngrok config add-authtoken <token>`
   и `ngrok http 8000`. Требует бесплатный аккаунт.
3. **Локально** — `make site` и `http://localhost:8000`; для показа рядом этого
   достаточно.

`make tunnel` оставлен как рабочая цель: там, где quick tunnel разрешён, он даст
`https://*.trycloudflare.com` без правок.

## Адреса после `make up`

| Что | Адрес | Логин |
| --- | --- | --- |
| Airflow | http://localhost:8080 | `admin` / `admin` |
| MinIO console | http://localhost:9001 | `minioadmin` / `minioadmin` |
| Grafana | http://localhost:3000 | `admin` / `admin` |
| Spark UI | http://localhost:8081 | — |
| Prometheus | http://localhost:9090 | — |
| ClickHouse HTTP | http://localhost:8123 | `default` |
| DQ exporter | http://localhost:9109/metrics | — |

Пароли задаются в `.env` (`AIRFLOW_ADMIN_*`, `GRAFANA_ADMIN_*`).
