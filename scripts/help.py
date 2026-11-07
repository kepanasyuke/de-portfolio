"""Print the Makefile help in clean UTF-8, without the quotes ``cmd echo`` adds."""

from __future__ import annotations

import sys

PORT_DEFAULT = "8000"
CONSOLE_PORT = "8088"

SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Стек (Docker):",
        (
            ("make doctor", "проверить, что установлено и чего не хватает"),
            ("make up", "поднять стек (создаст .env из .env.example)"),
            ("make up-core", "только postgres + minio + kafka (~1 ГБ образов)"),
            ("make down", "остановить и удалить тома"),
            ("make ps / logs", "состояние и логи Airflow"),
            ("make psql", "psql внутрь контейнера postgres"),
        ),
    ),
    (
        "Данные и код:",
        (
            ("make landing", "сгенерировать landing-слой за сегодня"),
            ("make landing-upload", "то же + загрузить в MinIO"),
            ("make stream", "запустить Kafka -> Spark (в контейнере)"),
            ("make dq", "прогнать data-quality проверки"),
            ("make parity", "сверка legacy DWH и нового"),
            ("make bench", "бенчмарк ClickHouse"),
            ("make test / lint", "тесты и контракт стиля + ruff"),
        ),
    ),
    (
        "Сайт и панель:",
        (
            ("make console", f"визуальная панель на http://localhost:{CONSOLE_PORT}/"),
            ("make site", f"собрать site/ и отдать на http://localhost:{PORT_DEFAULT}/"),
            ("make tunnel", "cloudflared: публичный https-URL на этот порт"),
            ("make showcase / game", "открыть витрину или игру локально"),
        ),
    ),
)


def render(port: str = PORT_DEFAULT) -> str:
    lines: list[str] = []
    for title, entries in SECTIONS:
        lines.append(title)
        for target, description in entries:
            lines.append(f"  {target:<22} {description}")
        lines.append("")
    return "\n".join(lines).replace(PORT_DEFAULT, port)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
