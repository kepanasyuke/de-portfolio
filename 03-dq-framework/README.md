# 03 · Data Quality framework

Декларативные проверки качества данных: правила живут в YAML, компилируются в SQL,
результаты пишутся в `meta.dq_result` и отдаются в Prometheus. Один и тот же набор
правил работает в батче (гейт перед витринами) и в стриме (каждые 15 минут).

<p align="center">
  <img src="../docs/assets/anim-dq.svg" alt="Data quality gate animation" width="820">
</p>

## Зачем свой движок, а не готовый

Great Expectations и dbt tests закрывают 80% кейсов, но в промышленном DWH мешают
три вещи: тяжёлые зависимости, отсутствие единого контракта результата и сложность
встроить гейт в произвольный Airflow-граф. Здесь контракт максимально узкий:

> любое правило — это один `SELECT`, возвращающий ровно одну строку с двумя
> целочисленными колонками: `failed_rows` и `checked_rows`.

Из этого контракта бесплатно получаются: сравнимость результатов между Postgres,
ClickHouse и Spark SQL; единый отчёт; интеграция с Prometheus; юнит-тесты без БД.

## Типы проверок

| Тип | Смысл | Ключи правила |
| --- | --- | --- |
| `not_null` | обязательное поле заполнено | `column` |
| `unique` | уникальность по одной или нескольким колонкам | `column` / `columns` (+ `where`) |
| `accepted_values` | значение входит в справочник | `column`, `values` |
| `referential_integrity` | нет сиротских строк | `column`, `ref_table`, `ref_column` |
| `range` | числовое значение в границах | `column`, `min_value`, `max_value` |
| `row_count` | объём таблицы в коридоре | `min_value`, `max_value` |
| `freshness` | данные не старше лага | `column`, `max_lag` |
| `expression` | произвольный SQL на `src` | `sql` |

`severity: warn` не закрывает гейт, но красит метрику и триггерит алерт.

## Пример правила

```yaml
- name: orders_customer_fk
  type: referential_integrity
  table: dds.orders
  column: customer_id
  ref_table: dds.customers
  ref_column: customer_id
  description: сиротские заказы ловим до сборки витрин
```

Скомпилированный SQL (`python -m dq.run --rules dq/rules.yml --show-sql`):

```sql
WITH src AS (SELECT * FROM dds.orders)
SELECT count(*) FILTER (WHERE s.customer_id IS NOT NULL
                         AND NOT EXISTS (SELECT 1 FROM dds.customers r
                                          WHERE r.customer_id = s.customer_id)) AS failed_rows,
       count(*) FILTER (WHERE s.customer_id IS NOT NULL) AS checked_rows
FROM src AS s;
```

## Запуск

```bash
# посмотреть, во что компилируются правила
python -m dq.run --rules 03-dq-framework/dq/rules.yml --show-sql

# прогнать против DWH и закрыть гейт при failed
DWH_DSN=postgresql://dwh:dwh@localhost:5432/dwh \
  python -m dq.run --rules 03-dq-framework/dq/rules.yml --json-out dq-report.json
```

Коды возврата: `0` — гейт открыт, `1` — гейт закрыт, `2` — прогон не состоялся
(битый конфиг, недоступная база). Airflow использует именно это, чтобы не пускать
плохие данные в витрины.

## Метрики и дашборд

Экспортёр — read-only сайдкар над таблицей вердиктов:

```
dq_rule_status{rule,table,severity}        1 passed / 0.5 warned / 0 failed
dq_rule_failed_rows{rule,table,severity}
dq_rule_checked_rows{rule,table,severity}
dq_rules_total, dq_rules_failed
dq_seconds_since_last_run
```

`monitoring/grafana-dq-dashboard.json` импортируется в Grafana как есть: статус
правил, динамика нарушенных строк, свежесть последнего прогона. Алерт, который
реально стоит завести: `dq_seconds_since_last_run > 93600` (прогон не приходил
больше суток — сломан шедулер, а не данные).

## Что здесь отработано

- Компиляция декларативных правил в SQL с защитой от инъекций: имена таблиц и
  колонок проходят регулярку квалифицированного идентификатора, литералы
  экранируются, в `sql`/`where` запрещены `;` и комментарии.
- Вердикты с `run_id` и `ran_at`: история прогонов, а не только последний статус.
- Один упавший чек не скрывает остальные: ошибка выполнения превращается в
  `failed` с сообщением, прогон продолжается.
- Гейт настраивается по строгости: `--fail-on error|warn|none`.
