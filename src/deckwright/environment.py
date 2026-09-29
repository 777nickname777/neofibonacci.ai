"""Проверка системных зависимостей, без которых пайплайн не работает.

Три внешние зависимости не ставятся через pip и молча ломают разные участки
пайплайна, если их нет:

* LibreOffice Impress — без него ``soffice`` на ``.pptx`` отвечает
  ``source file could not be loaded``, и экспорт в PDF отваливается;
* poppler (``pdftoppm``) — без него нет PNG, а значит нет превью и нет
  контекстного аудита по картинке слайда;
* libeot — без него не распаковываются встроенные в шаблон шрифты, и рендер,
  PDF и измерение текста расходятся с тем, что задумал дизайнер;
* метрически совместимые клоны проприетарных шрифтов — без них шаблон на
  Calibri меряется по DejaVu, ширины расходятся, и бюджет длины уходит
  мимо.

Проверяются они здесь, одним вызовом, и на этапе сборки образа — чтобы
отсутствие вскрывалось до первого прогона, а не посреди него.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .parse.fonts import (
    LIBEOT_ENV_VAR,
    LIBEOT_SYMBOL,
    FontExtractionError,
    libeot_candidates,
    load_libeot,
)

# Имена библиотеки и функции libeot берутся из `parse.fonts`: там их
# единственное определение. Две копии списка имён однажды разъедутся, и
# проверка готовности начнёт расходиться с тем, что делает разбор.


@dataclass(frozen=True)
class Check:
    """Результат одной проверки готовности.

    `blocking` отличает «сервис не поднимется» от «одна функция недоступна».
    Разница практическая: без LibreOffice не будет PDF и превью, но `.pptx`
    соберётся и скачается — объявлять такой запуск несостоявшимся нельзя.
    """

    name: str
    ok: bool
    detail: str
    blocking: bool = True
    # Что именно не будет работать, если проверка не прошла.
    affects: str = ""


def _version_line(binary: str, args: list[str]) -> str:
    try:
        proc = subprocess.run(  # список аргументов фиксирован, не из пользовательского ввода
            [binary, *args], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"не запускается: {exc}"
    out = (proc.stdout or proc.stderr or "").strip().splitlines()
    return out[0] if out else "версия не определена"


def check_soffice(binary: str = "soffice") -> Check:
    path = shutil.which(binary)
    if path is None:
        return Check(
            "LibreOffice",
            False,
            f"{binary} не найден в PATH. На macOS он лежит внутри приложения: "
            "/Applications/LibreOffice.app/Contents/MacOS/soffice — укажите его "
            "в render.soffice_binary или поставьте симлинк",
            blocking=False,
            affects="экспорт в .pdf, превью слайдов и контекстный аудит; "
                    ".pptx и .html собираются",
        )
    return Check("LibreOffice", True, f"{path}: {_version_line(binary, ['--version'])}")


def check_pdftoppm() -> Check:
    path = shutil.which("pdftoppm")
    if path is None:
        return Check(
            "poppler",
            False,
            "pdftoppm не найден в PATH (brew install poppler)",
            blocking=False,
            affects="превью слайдов и контекстный аудит по картинкам",
        )
    return Check("poppler", True, f"{path}: {_version_line('pdftoppm', ['-v'])}")


def check_writable(paths: list[Path]) -> Check:
    """Можно ли вообще создавать файлы там, куда пишет прогон.

    Каталог без права записи вскрывается сейчас, а не на двадцатой минуте
    генерации, когда колода уже посчитана и её некуда положить.
    """
    bad: list[str] = []
    for path in paths:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".deckwright-write-test-{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            bad.append(f"{path}: {exc.strerror or exc}")
    if bad:
        return Check("рабочие каталоги", False, "; ".join(bad), blocking=True)
    return Check(
        "рабочие каталоги", True, ", ".join(str(p) for p in paths) + " — запись есть"
    )


def check_model(cfg) -> Check:
    """Настроена ли модель. Не блокирует: без неё идут записанные ответы."""
    missing = [
        name
        for name, value in (
            ("LLM_BASE_URL", cfg.llm.base_url),
            ("LLM_API_KEY", cfg.llm.api_key),
            ("LLM_MODEL", cfg.llm.model),
        )
        if not value
    ]
    if missing:
        return Check(
            "модель",
            False,
            # Имена переменных, никогда не значения: в файле лежат ключи.
            "не заданы: " + ", ".join(missing) + " (см. .env.example)",
            blocking=False,
            affects="разбор произвольного входа, планирование и контекстный "
                    "аудит; прогон по записанным ответам работает",
        )
    vlm = "и модель со зрением" if cfg.vlm.configured else "без модели со зрением"
    return Check("модель", True, f"endpoint задан, {vlm}")


def check_libeot() -> Check:
    try:
        lib, name = load_libeot()
    except FontExtractionError:
        tried = ", ".join(libeot_candidates())
        return Check(
            "libeot", False,
            f"библиотека не загружается; искали: {tried}. Явный путь задаётся "
            f"переменной {LIBEOT_ENV_VAR}",
            blocking=False,
            affects="распаковку встроенных в шаблон шрифтов; текст будет "
                    "мериться по подставленному шрифту, и это пишется в манифест",
        )
    if not hasattr(lib, LIBEOT_SYMBOL):
        return Check(
            "libeot", False, f"{name} без символа {LIBEOT_SYMBOL}",
            blocking=False, affects="распаковку встроенных шрифтов",
        )
    return Check("libeot", True, f"{name}, {LIBEOT_SYMBOL} на месте")


def check_fallback_fonts() -> Check:
    """Есть ли хоть один шрифт для подстановки, когда шрифт шаблона недоступен."""
    try:
        from fontTools.ttLib import TTFont  # noqa: F401
    except ImportError as exc:  # pragma: no cover - fontTools в зависимостях
        return Check("шрифты", False, f"fontTools недоступен: {exc}", blocking=True)

    # Тот же список, по которому ищет сам слой измерения. Раньше здесь
    # лежала своя копия путей, и две проверки противоречили друг другу:
    # «метрические клоны на месте» рядом с «ни одного .ttf».
    from deckwright.layout.text_metrics import FALLBACK_FONT_DIRS

    roots = [Path(d) for d in FALLBACK_FONT_DIRS]
    found = [p for root in roots if root.is_dir() for p in root.rglob("*.ttf")]
    if not found:
        return Check(
            "шрифты", False,
            "ни одного .ttf в " + ", ".join(str(r) for r in roots),
            blocking=True,
            affects="измерение текста: без шрифта бюджет длины обнуляется,\n"
                    "и вёрстка выбирает композицию вслепую",
        )
    return Check("шрифты", True, f"{len(found)} .ttf, например {found[0].name}")


def check_metric_clones() -> Check:
    """Есть ли в образе свободные клоны проприетарных шрифтов.

    Они не про внешний вид, а про ширины. Carlito повторяет метрики Calibri,
    Caladea — Cambria, Liberation — Arial, Times New Roman и Courier New:
    текст переносится на тех же местах, что и у человека с оригиналом, и
    бюджет длины считается по правильным ширинам. Без них подставляется
    DejaVu, и измерение врёт.

    Само имя шрифта в `.pptx` при этом не меняется никогда — клоны живут
    только внутри контейнера.
    """
    from deckwright.layout.text_metrics import METRIC_CLONES, _find_font_file

    missing = sorted(
        {
            files[0]
            for files in METRIC_CLONES.values()
            if _find_font_file(files) is None
        }
    )
    if missing:
        return Check(
            "метрические клоны", False,
            "нет: " + ", ".join(missing),
            blocking=False,
            affects="точность измерения текста для шаблонов на Calibri, Cambria, Arial и Times",
        )
    return Check(
        "метрические клоны",
        True,
        f"на месте, {len(METRIC_CLONES)} гарнитур покрыто",
    )


def run_checks(soffice_binary: str = "soffice", cfg=None) -> list[Check]:
    """Готовность окружения.

    `cfg` необязателен: без него проверяются только системные зависимости.
    С ним добавляются проверки, которые зависят от настроек, — право записи
    в рабочие каталоги и наличие настроек модели.
    """
    checks = [
        check_soffice(soffice_binary),
        check_pdftoppm(),
        check_libeot(),
        check_fallback_fonts(),
        check_metric_clones(),
    ]
    if cfg is not None:
        checks.append(
            check_writable([Path(cfg.run.output_dir), Path(cfg.template.cache_dir),
                            Path(cfg.fonts.extract_dir)])
        )
        checks.append(check_model(cfg))
    return checks
