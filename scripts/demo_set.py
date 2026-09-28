"""Набор для сдачи: колоды одного входа на нескольких шаблонах и сводка.

Живой прогон идёт в GitHub Actions (`llm-probe.yml`, `new_inputs`), его
артефакты из среды разработки недоступны. Поэтому набор собирается из двух
половин:

* из лога — время и находки аудита живого прогона (`probe_harvest.py`
  разложил их по `ПАРЫ/<вход>/<шаблон>/`: `run-summary.json`, `audit.json`);
* локально — сами колоды, пересобранные по живому плану тем же кодом
  (`probe_rebuild.sh` → `КОЛОДЫ/<вход>/<шаблон>/<вариант>/`): вёрстка
  детерминирована, план и контент-пакет — те, что вернула модель.

Итог в `ВЫХОД/`: `<шаблон>_<вариант>.{pptx,pdf,html}`, `README.md` с таблицей
«шаблон, вариант, время, находки аудита» и `sheet.png` — сводный лист.

Запуск:
    python scripts/demo_set.py ВХОД ПАРЫ КОЛОДЫ ВЫХОД "заголовок" шаблон[,шаблон…] [прогон]
"""

from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from contact_sheet import sheet, stack

VARIANTS = ("dense", "balanced", "airy")
SEVERITIES = ("error", "warning", "info")


def _counts(audit: dict, template: str, variant: str) -> Counter:
    entry = audit.get(f"{template}_{variant}.audit", {})
    return Counter(entry.get("counts", {}))


def build(
    source: str, pairs: Path, decks: Path, out: Path, title: str, templates: list[str], run: str
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows, sheets, top = [], [], Counter()
    for template in templates:
        pair = pairs / source / template
        summary = json.loads((pair / "run-summary.json").read_text("utf-8"))
        audit = json.loads((pair / "audit.json").read_text("utf-8"))
        for variant in VARIANTS:
            deck = decks / source / template / variant
            for ext in ("pptx", "pdf", "html"):
                name = f"{template}_{variant}.{ext}"
                shutil.copy(deck / name, out / name)
            counts = _counts(audit, template, variant)
            by_severity = Counter()
            for key, number in counts.items():
                severity, check = key.split(":", 1)
                by_severity[severity] += number
                if severity != "info":
                    top[check] += number
            slides = len(list((deck / "png").glob("*.png")))
            rows.append(
                f"| {template} | {variant} | {slides} "
                f"| {summary['variant_seconds'].get(variant, 0):.0f} "
                f"| {summary['total_seconds']:.0f} "
                + "".join(f"| {by_severity[s]} " for s in SEVERITIES)
                + "|"
            )
            part = out / f".sheet-{template}-{variant}.png"
            sheet(deck / "png", part, f"{template} · {variant} · {slides} сл.", 6, 260)
            sheets.append(part)
    stack(out / "sheet.png", sheets)
    for part in sheets:
        part.unlink()

    lines = [
        f"# {title}",
        "",
        f"Живой прогон `llm-probe.yml` {run}: модель, аудит по картинкам и по тексту, "
        "цикл исправления `auto`. Колоды пересобраны локально по живому плану тем же "
        "кодом — вёрстка детерминирована; время и находки — из лога живого прогона.",
        "",
        "Время — секунды: вариант (раскладка, сборка, аудит, исправление) и вся колода "
        "с разбором входа и планом — бюджет 300 с.",
        "",
        "| шаблон | вариант | слайдов | вариант, с | колода, с | error | warning | info |",
        "|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        "Частые находки (error и warning, все колоды набора):",
        "",
        *[f"* `{check}` — {number}" for check, number in top.most_common(8)],
        "",
        "Сводный лист всех колод — `sheet.png`.",
        "",
    ]
    (out / "README.md").write_text("\n".join(lines), "utf-8")


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 6:
        raise SystemExit(__doc__)
    build(
        args[0], Path(args[1]), Path(args[2]), Path(args[3]), args[4], args[5].split(","),
        args[6] if len(args) > 6 else "",
    )
