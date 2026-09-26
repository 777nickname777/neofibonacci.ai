"""Лог живого прогона новых входов → каталоги пар с результатами.

Живая модель доступна только из GitHub Actions (`llm-probe.yml`, режим
`new_inputs`), а артефакты прогона из среды разработки недоступны. Поэтому
шаг печатает в лог по каждой паре «вход × шаблон» блоки
`----- НАЧАЛО имя -----` … `----- КОНЕЦ имя -----`: сводку аудита,
`run-summary.json`, контент-пакет и план. Этот скрипт раскладывает их по
каталогам `ВЫХОД/<вход>/<шаблон>/`:

    audit.json         сводка аудита по вариантам: счётчики и ошибки
    run-summary.json   время: разбор входа, генерация, всего, бюджет
    pack.json          контент-пакет, полученный из входа
    plan.json          план колоды (общий для вариантов)
    cli.txt            строки [вход] и [прогон] из вывода CLI

Лог — текст задачи из GitHub (кнопка «View raw logs» или `get_job_logs`),
с отметками времени в начале строк или без них.

Запуск: `python scripts/probe_harvest.py ЛОГ ВЫХОД`
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
_RUN = re.compile(r"===== ПРОГОН (\S+) × (\S+) =====")
_BEGIN = re.compile(r"----- НАЧАЛО (\S+) -----")
_END = re.compile(r"----- КОНЕЦ (\S+) -----")


def harvest(log_text: str, out: Path) -> list[Path]:
    """Раскладывает лог по парам; возвращает каталоги пар."""
    # Лог из API приходит и одной JSON-строкой с полем содержимого.
    if log_text.lstrip().startswith("{"):
        try:
            data = json.loads(log_text)
            log_text = data.get("logs_content") or data.get("content") or log_text
        except ValueError:
            pass
    pairs: list[Path] = []
    current: Path | None = None
    block: str | None = None
    buffer: list[str] = []
    for raw in log_text.splitlines():
        line = _STAMP.sub("", raw)
        if match := _RUN.search(line):
            current = out / match.group(1) / match.group(2)
            current.mkdir(parents=True, exist_ok=True)
            pairs.append(current)
            continue
        if current is None:
            continue
        if match := _BEGIN.match(line):
            block, buffer = match.group(1), []
            continue
        if (match := _END.match(line)) and block:
            name = block if block.endswith(".json") else f"{block}.json"
            (current / name).write_text("\n".join(buffer), encoding="utf-8")
            block = None
            continue
        if block:
            buffer.append(line)
        elif line.startswith(("[вход]", "[прогон]")):
            with (current / "cli.txt").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    return pairs


def summary(pairs: list[Path]) -> str:
    """Таблица по парам: время против бюджета и ошибки аудита."""
    rows = ["вход × шаблон | разбор, с | всего, с | бюджет | ошибок аудита"]
    for pair in pairs:
        name = f"{pair.parent.name} × {pair.name}"
        try:
            run = json.loads((pair / "run-summary.json").read_text(encoding="utf-8"))
            audit = json.loads((pair / "audit.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows.append(f"{name} | прогон не завершился: см. лог")
            continue
        errors = sum(len(variant["errors"]) for variant in audit.values())
        verdict = "в бюджете" if run["total_seconds"] <= run["budget_seconds"] else "ВНЕ"
        rows.append(
            f"{name} | {run['parse_seconds']} | {run['total_seconds']} | {verdict} | {errors}"
        )
    return "\n".join(rows)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    found = harvest(text, Path(sys.argv[2]))
    print(summary(found))
